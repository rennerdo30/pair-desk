"""Shared pieces of the Codex, opencode and Antigravity installers.

- Timestamped backups of config files before they change.
- Writing a file only when its content changes (so re-running an installer is a no-op).
- The Pair Desk skills, generated from `skills/*/SKILL.md` of this repo for tools that read the
  cross-tool skill folder `~/.agents/skills` (Codex, opencode and Antigravity). The Claude Code
  wording (plugin root, `${CLAUDE_PLUGIN_ROOT}`, `/agent-pair-programming:serve`) is rewritten to
  the absolute `desk.py` path and the installed skill names. Each installed skill folder carries
  a marker listing which installers use it, so uninstalling Codex leaves the copy opencode still
  needs, and a skill folder without the marker (someone else's) is never touched.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# abspath, not resolve(): keep the drive the user runs from (E: may be a substitute for D:)
REPO_ROOT = Path(os.path.abspath(__file__)).parent.parent
DESK_PY = REPO_ROOT / "desk.py"
LAUNCHER = "bin/pair-desk"          # POSIX sh launcher, relative to the app root
LAUNCHER_CMD = "bin/pair-desk.cmd"  # Windows launcher
SKILLS_SRC = REPO_ROOT / "skills"
SERVER_NAME = "pair-desk"

# source skill folder -> installed skill name (global names must not collide with other skills)
SKILL_NAMES = {"pair-desk": "pair-desk", "serve": "pair-desk-serve", "triage": "pair-desk-triage"}
MARKER = ".pair-desk-install.json"


@dataclass
class Report:
    """What an install or uninstall did (or would do with --dry-run)."""
    dry_run: bool = False
    lines: list[str] = field(default_factory=list)
    changed: bool = False

    def note(self, text: str) -> None:
        self.lines.append(text)

    def change(self, text: str) -> None:
        self.changed = True
        self.lines.append(("would " if self.dry_run else "") + text)

    def print(self) -> None:
        for line in self.lines:
            print(line)
        if not self.changed:
            print("Nothing to change.")


def fwd(path: str | os.PathLike) -> str:
    """Absolute path with forward slashes: works in TOML, JSON, shells and Python on Windows."""
    return Path(os.path.abspath(os.fspath(path))).as_posix()


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path: Path) -> Path | None:
    """Copy `path` to `<name>.pair-desk-backup-<timestamp>` next to it. Never overwrites a backup."""
    if not path.is_file():
        return None
    stamp = timestamp()
    target = path.with_name(f"{path.name}.pair-desk-backup-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.pair-desk-backup-{stamp}-{n}")
    shutil.copy2(path, target)
    return target


def read_text(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def update_config_file(path: Path, new_text: str | None, report: Report, what: str) -> None:
    """Write `new_text` to a user config file with a backup first. `None` means unchanged."""
    old = read_text(path)
    if new_text is None or new_text == old:
        report.note(f"{what}: already up to date ({path})")
        return
    if report.dry_run:
        report.change(f"update {what} in {path} (after a timestamped backup)")
        return
    saved = backup(path)
    write_text(path, new_text)
    report.change(f"updated {what} in {path}" + (f" (backup: {saved.name})" if saved else " (new file)"))


# -- how integrations start the desk ---------------------------------------------------------

_VERSION_PROBE = "import sys; sys.exit(sys.version_info < (3, 11))"


def python_ok(argv: list[str]) -> bool:
    """Whether `argv` (e.g. ["py", "-3"]) is a Python 3.11+ that actually runs (the Windows Store
    `python3` stub does not)."""
    try:
        res = subprocess.run([*argv, "-I", "-S", "-c", _VERSION_PROBE], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def windows_python() -> list[str]:
    """The interpreter a Windows integration runs desk.py with: `python` or `py -3` when that is a
    working Python 3.11+ (both stay valid across Python upgrades), else this interpreter's own
    base executable (outside any virtual environment, so not a throwaway uvx or pipx one)."""
    for argv in (["python"], ["py", "-3"]):
        if shutil.which(argv[0]) and python_ok(argv):
            return argv
    return [fwd(getattr(sys, "_base_executable", None) or sys.executable)]


def desk_command(desk: str | os.PathLike, python: str | None = None, windows: bool | None = None) -> list[str]:
    """The argv prefix that runs `desk.py` for MCP integrations (add "mcp" or "hook ...").

    - `python` given (the installers' --python): `[python, desk.py]`, exactly as asked.
    - macOS and Linux: the `bin/pair-desk` launcher next to desk.py, which picks python3 or
      python (3.11+) each time it starts, so neither name has to exist.
    - Windows: `python`, `py -3` or an absolute interpreter, found now, plus desk.py. Codex and
      opencode cannot start a `.cmd` launcher directly, and a bare `python` is shell-agnostic in
      hook commands (cmd, PowerShell and Git Bash)."""
    desk = fwd(desk)
    if python:
        return [python, desk]
    windows = (os.name == "nt") if windows is None else windows
    if not windows:
        launcher = Path(desk).parent / LAUNCHER
        if launcher.is_file():
            ensure_executable(launcher)
            return [launcher.as_posix()]
        return ["python3", desk]
    return [*windows_python(), desk]


def ensure_executable(path: Path) -> None:
    """chmod +x (a zip download or a wheel may drop the bit); no-op on Windows."""
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
        if mode & 0o111 != 0o111:
            path.chmod(mode | 0o755)
    except OSError:
        pass


# characters that make a hook command word need double quotes (paths always get them)
_NEEDS_QUOTES = frozenset(" \t\"'/\\$&;|<>()")


def shell_line(argv: list[str]) -> str:
    """A command line for a hook: paths in double quotes, plain words as they are. Works in sh,
    cmd and PowerShell as long as the first word is a plain command name (python, py) or, on
    macOS and Linux, a quoted path."""
    def quote(a: str) -> str:
        if not a or any(c in a for c in _NEEDS_QUOTES):
            return '"' + a.replace('"', '\\"') + '"'
        return a
    return " ".join(quote(a) for a in argv)


# -- skills ---------------------------------------------------------------------------------

_CLI_COMMANDS = ("serve|stop|projects|new-project|project-set|link|list|show|add|edit|comment|status|attach|"
                 "send|import-json|export|plan|step|parent|merge|unmerge|suggest-groups|handoff")


def _rewrite_skill(text: str, source_name: str, desk_cmd: str) -> str:
    """Claude Code plugin wording -> an installed skill that runs the desk by absolute path.

    `desk_cmd` is the command line prefix that runs desk.py on this machine (see desk_command),
    e.g. `python "C:/apps/pair-desk/desk.py"` or `"/home/me/pair-desk/bin/pair-desk"`."""
    name = SKILL_NAMES[source_name]
    out = re.sub(r"(?m)^name:\s*\S+\s*$", f"name: {name}", text, count=1)
    # the explanations of where the plugin's bin/pair-desk lives
    out = re.sub(r"(?s)The desk command is `pair-desk`:.*?in PowerShell\.", f"The desk command is `{desk_cmd}`.", out)
    out = re.sub(r"(?s)\s*\(`pair-desk` is the plugin's `bin/pair-desk`.*?this skill\)", "", out)
    out = re.sub(r"(?s) ?`pair-desk` is the plugin's `bin/pair-desk`.*?this skill\.", "", out)
    # every CLI call: `pair-desk list ...` -> `<desk_cmd> list ...`
    out = re.sub(rf"(?<![\w/.\-])pair-desk (?=(?:{_CLI_COMMANDS})\b)", desk_cmd.replace("\\", "\\\\") + " ", out)
    for src, dst in SKILL_NAMES.items():
        out = out.replace(f"`/agent-pair-programming:{src}`", f"the `{dst}` skill")
        out = out.replace(f"/agent-pair-programming:{src}", f"the `{dst}` skill")
    out = out.replace("Claude Code plugin", "Pair Desk integration")
    header = (f"<!-- Generated from {fwd(SKILLS_SRC / source_name / 'SKILL.md')} by the Pair Desk installer. "
              "Re-run the installer to update; edits here are overwritten. -->\n")
    m = re.match(r"(?s)(---\n.*?\n---\n)", out)
    return (m.group(1) + header + out[m.end():]) if m else header + out


def render_skills(desk_cmd: str | None = None) -> dict[str, str]:
    """Installed skill name -> SKILL.md text, from the skills in this repo. `desk_cmd` defaults to
    how this machine runs this repo's desk.py."""
    desk_cmd = desk_cmd or shell_line(desk_command(DESK_PY))
    rendered = {}
    for src, name in SKILL_NAMES.items():
        f = SKILLS_SRC / src / "SKILL.md"
        if f.is_file():
            rendered[name] = _rewrite_skill(f.read_text(encoding="utf-8"), src, desk_cmd)
    return rendered


def _read_marker(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def install_skills(skills_dir: Path, owner: str, report: Report, desk_cmd: str | None = None,
                   force: bool = False) -> None:
    for name, text in render_skills(desk_cmd).items():
        folder = skills_dir / name
        marker = _read_marker(folder)
        if folder.exists() and marker is None and not force:
            report.note(f"skill {name}: {folder} exists and was not installed by Pair Desk; left alone "
                        "(--force replaces it)")
            continue
        owners = sorted(set((marker or {}).get("installed_by", [])) | {owner})
        new_marker = json.dumps({"source": fwd(REPO_ROOT), "installed_by": owners}, indent=2) + "\n"
        same = read_text(folder / "SKILL.md") == text and read_text(folder / MARKER) == new_marker
        if same:
            report.note(f"skill {name}: up to date ({folder})")
            continue
        report.change(f"install skill {name} -> {folder}")
        if not report.dry_run:
            write_text(folder / "SKILL.md", text)
            write_text(folder / MARKER, new_marker)


def uninstall_skills(skills_dir: Path, owner: str, report: Report) -> None:
    for name in SKILL_NAMES.values():
        folder = skills_dir / name
        marker = _read_marker(folder)
        if marker is None:
            if folder.exists():
                report.note(f"skill {name}: {folder} was not installed by Pair Desk; left alone")
            continue
        owners = sorted(set(marker.get("installed_by", [])) - {owner})
        if owners:
            if owners == sorted(marker.get("installed_by", [])):
                continue
            report.change(f"keep skill {name} (still used by {', '.join(owners)}), drop {owner} from its marker")
            if not report.dry_run:
                marker["installed_by"] = owners
                write_text(folder / MARKER, json.dumps(marker, indent=2) + "\n")
        else:
            report.change(f"remove skill {name} ({folder})")
            if not report.dry_run:
                shutil.rmtree(folder)


def agents_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


# -- JSON with comments ---------------------------------------------------------------------

def strip_jsonc(text: str) -> tuple[str, bool]:
    """Remove // and /* */ comments and trailing commas outside strings.
    Returns (json_text, had_comments)."""
    out: list[str] = []
    i, n, had = 0, len(text), False
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            had = True
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            had = True
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    joined = "".join(out)
    # trailing commas: a comma followed only by whitespace before } or ] (strings were kept intact,
    # so rescan while skipping them)
    res: list[str] = []
    i, n = 0, len(joined)
    while i < n:
        c = joined[i]
        if c == '"':
            j = i + 1
            while j < n and joined[j] != '"':
                j += 2 if joined[j] == "\\" else 1
            res.append(joined[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            k = i + 1
            while k < n and joined[k] in " \t\r\n":
                k += 1
            if k < n and joined[k] in "}]":
                i += 1
                continue
        res.append(c)
        i += 1
    return "".join(res), had
