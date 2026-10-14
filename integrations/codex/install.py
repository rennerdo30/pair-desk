"""Install Pair Desk into the Codex CLI (user level).

    python integrations/codex/install.py              # install or update
    python integrations/codex/install.py --dry-run    # show what would change
    python integrations/codex/install.py --uninstall  # remove only what this installer added

What it sets up:
- `[mcp_servers.pair-desk]` in `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`):
  the stdio MCP server `<desk> mcp`.
- A `SessionStart` hook in the same file (`[[hooks.SessionStart]]`, matcher startup|resume|clear)
  running `<desk> hook session-start --client codex`, which adds the one-line desk summary for the
  repo's linked project to the session context, and a `UserPromptSubmit` hook running
  `<desk> hook user-prompt-submit --client codex`, which adds the owner's new activity. Codex asks you to review new hooks once: open
  `codex`, run `/hooks` and trust it.
- The skills `pair-desk`, `pair-desk-serve`, `pair-desk-triage` in `~/.agents/skills/` (the
  user skill folder Codex reads), generated from this repo's `skills/`.

`<desk>` is how this machine starts desk.py (integrations.common.desk_command): the
`bin/pair-desk` launcher on macOS and Linux, `python` or `py -3` plus desk.py on Windows, or
`--python X` plus desk.py when given.

config.toml gets a timestamped backup before every change; nothing outside the pair-desk
entries is edited. Re-running is a no-op when everything is current.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.common import (  # noqa: E402
    DESK_PY, SERVER_NAME, Report, agents_skills_dir, desk_command, fwd, install_skills, read_text,
    shell_line, uninstall_skills, update_config_file)

TOOL = "codex"
BEGIN = "# >>> pair-desk: managed by agent-pair-programming/integrations/codex/install.py >>>"
END = "# <<< pair-desk <<<"
HOOK_EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "user-prompt-submit"}

_HEADER = re.compile(r"^\s*(\[\[?)\s*(.+?)\s*(\]\]?)\s*(#.*)?$")


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def managed_block(prefix: list[str]) -> str:
    """The pair-desk block; `prefix` runs desk.py (see desk_command), e.g. ["python", "C:/x/desk.py"]."""
    args = ", ".join(_toml_str(a) for a in [*prefix[1:], "mcp"])
    return "\n".join([
        BEGIN,
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_str(prefix[0])}",
        f"args = [{args}]",
        "startup_timeout_sec = 20",
        *(line for event, action in HOOK_EVENTS.items() for line in (
            "",
            f"[[hooks.{event}]]",
            *(['matcher = "startup|resume|clear"', ""] if event == "SessionStart" else []),
            f"[[hooks.{event}.hooks]]",
            'type = "command"',
            f"command = {_toml_str(shell_line([*prefix, 'hook', action, '--client', 'codex']))}",
            "timeout = 10",
            *(['statusMessage = "Pair Desk: reading the desk"'] if event == "SessionStart" else []),
        )),
        END,
        "",
    ])


def _norm_key(raw: str) -> str:
    return re.sub(r"\s*\.\s*", ".", raw).replace('"', "").replace("'", "")


def _sections(text: str) -> list[tuple[str | None, bool, list[str]]]:
    """Split TOML text into (normalized header, is_array, lines) sections; the first has no header."""
    out: list[tuple[str | None, bool, list[str]]] = [(None, False, [])]
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line)
        if m and len(m.group(1)) == len(m.group(3)):
            out.append((_norm_key(m.group(2)), m.group(1) == "[[", [line]))
        else:
            out[-1][2].append(line)
    return out


def _is_ours_hook(lines: list[str]) -> bool:
    body = "".join(lines)
    return ("desk.py" in body or "pair-desk" in body) and any(f"hook {a}" in body for a in HOOK_EVENTS.values())


def strip_pair_desk(text: str) -> tuple[str, bool]:
    """Remove every pair-desk entry: `[mcp_servers.pair-desk]` (and its sub-tables, whoever wrote
    it), SessionStart/UserPromptSubmit hook entries that run `desk.py hook ...`, and the marker comments.
    Everything else is kept byte for byte. Returns (text, removed_anything)."""
    mcp = f"mcp_servers.{SERVER_NAME}"
    hooks = {f"hooks.{event}" for event in HOOK_EVENTS}
    secs = _sections(text)
    kept: list[str] = []
    removed = False
    i = 0
    while i < len(secs):
        key, is_array, lines = secs[i]
        if key is not None and (key == mcp or key.startswith(mcp + ".")):
            removed = True
            i += 1
            continue
        if key in hooks and is_array:
            group = list(lines)
            j = i + 1
            while j < len(secs) and secs[j][0] is not None and secs[j][0].startswith(key + "."):
                group.extend(secs[j][2])
                j += 1
            if _is_ours_hook(group):
                removed = True
            else:
                kept.extend(group)
            i = j
            continue
        kept.extend(lines)
        i += 1
    result = []
    for line in kept:
        if line.strip() in (BEGIN, END):
            removed = True
            continue
        result.append(line)
    if not removed:
        return text, False
    out = "".join(result).rstrip()
    return (out + "\n") if out else "", True


def merged_config(text: str | None, prefix: list[str]) -> str:
    """config.toml text with the current pair-desk block (replacing any earlier pair-desk entries)."""
    text = text or ""
    tomllib.loads(text)  # refuse to edit a config Codex itself could not read
    base, _ = strip_pair_desk(text)
    base = base.rstrip()
    new = (base + "\n\n" if base else "") + managed_block(prefix)
    server = tomllib.loads(new)["mcp_servers"][SERVER_NAME]
    assert [server["command"], *server["args"]] == [*prefix, "mcp"]
    return new


def removed_config(text: str | None) -> str | None:
    if not text:
        return None
    tomllib.loads(text)
    new, removed = strip_pair_desk(text)
    if not removed:
        return None
    tomllib.loads(new)
    return new


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install Pair Desk (MCP server, SessionStart and UserPromptSubmit hooks, skills) into Codex.")
    ap.add_argument("--uninstall", action="store_true", help="remove what this installer added")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--codex-home", type=Path, default=None, help="Codex home (default: $CODEX_HOME or ~/.codex)")
    ap.add_argument("--skills-dir", type=Path, default=None, help="skill folder (default: ~/.agents/skills)")
    ap.add_argument("--python", default=None,
                    help="Python command Codex runs desk.py with (default: the bin/pair-desk launcher on macOS "
                         "and Linux; python or py -3, whichever is a Python 3.11+, on Windows)")
    ap.add_argument("--desk", type=Path, default=DESK_PY, help="path to desk.py (default: this repo)")
    ap.add_argument("--no-skills", action="store_true", help="leave the skills alone")
    ap.add_argument("--force", action="store_true", help="replace same-named skills not installed by Pair Desk")
    args = ap.parse_args(argv)

    home = args.codex_home or codex_home()
    config = home / "config.toml"
    skills_dir = args.skills_dir or agents_skills_dir()
    desk = fwd(args.desk)
    report = Report(dry_run=args.dry_run)
    try:
        text = read_text(config)
        if args.uninstall:
            update_config_file(config, removed_config(text), report, "Codex MCP server and Pair Desk hooks")
            if not args.no_skills:
                uninstall_skills(skills_dir, TOOL, report)
        else:
            if not Path(desk).is_file():
                print(f"error: {desk} does not exist", file=sys.stderr)
                return 1
            prefix = desk_command(desk, args.python)
            update_config_file(config, merged_config(text, prefix), report,
                               "Codex MCP server and Pair Desk hooks")
            if not args.no_skills:
                install_skills(skills_dir, TOOL, report, shell_line(prefix), force=args.force)
    except tomllib.TOMLDecodeError as e:
        print(f"error: {config} is not valid TOML ({e}); fix it first, nothing was changed", file=sys.stderr)
        return 1
    report.print()
    if not args.uninstall and report.changed and not args.dry_run:
        print("Codex asks you to review new or changed hooks: start `codex`, run /hooks and trust the "
              "Pair Desk hooks. Verify with: codex mcp list")
    return 0


if __name__ == "__main__":
    sys.exit(run())
