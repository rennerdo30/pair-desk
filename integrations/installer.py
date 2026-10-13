"""`pair-desk install | update | uninstall`: set Pair Desk up for supported coding agents.

    pair-desk install                      # every tool found on PATH, asking per tool
    pair-desk install claude codex --yes   # only these, without questions
    pair-desk update | uninstall [tools] [--yes] [--dry-run]

For each tool it shows exactly what it will run and asks before running it. It runs only:

- Claude Code: the official plugin commands (`claude plugin marketplace add|update`,
  `claude plugin install|update|uninstall`).
- Codex, opencode and Antigravity: this repo's `integrations/<tool>/install.py`, which edits only the
  `pair-desk` entries of the tool's config (after a timestamped backup) and writes the skills.
  Those entries name an absolute path, so they must point at a copy of Pair Desk that stays: a
  git clone is used where it is; any other source (a uvx or pipx run, a downloaded archive) is
  first copied to the app folder (`app_dir()`, or --app-dir) and the entries point there.

After an install it offers to link the current git repo to a desk project and says how to start
the desk. Nothing here touches the desk's data beyond that link (and creating the project it
names, when it does not exist yet).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from integrations.common import LAUNCHER, LAUNCHER_CMD, REPO_ROOT, ensure_executable, fwd

MARKETPLACE_REPO = "rennerdo30/pair-desk"
MARKETPLACE_NAME = "agent-pair-programming"
PLUGIN_ID = f"agent-pair-programming@{MARKETPLACE_NAME}"
TOOLS = ("claude", "codex", "opencode", "antigravity")
ACTIONS = ("install", "update", "uninstall")

# what a standalone copy of Pair Desk needs (relative to the app root)
APP_ITEMS = ("desk.py", "pair_desk", "web", "skills", "integrations", "bin", "LICENSE", "README.md")
APP_MARKER = ".pair-desk-app.json"


def app_dir() -> Path:
    """Where a copy of Pair Desk lives when it is not run from a git clone: $PAIR_DESK_APP, else
    %LOCALAPPDATA%\\Programs\\AgentPairProgramming on Windows, else
    ${XDG_DATA_HOME:-~/.local/share}/agent-pair-programming. (The desk's data folder is separate.)"""
    env = os.environ.get("PAIR_DESK_APP")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "AgentPairProgramming"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agent-pair-programming"


def is_clone(root: Path) -> bool:
    return (root / ".git").exists()


def launcher_for(root: Path) -> str:
    """The command that starts the desk from `root`, for messages."""
    if os.name == "nt":
        return f'"{Path(root) / LAUNCHER_CMD}"'
    return f'"{(Path(root) / LAUNCHER).as_posix()}"'


@dataclass
class Step:
    """One thing the installer does: an official CLI command, copying Pair Desk to the app
    folder, one of the integration installers, or removing the app folder."""
    kind: str                      # command | copy | integration | remove-app
    argv: list[str] = field(default_factory=list)
    allow_failure: bool = False
    tool: str = ""

    def describe(self, app: Path, source: Path) -> str:
        if self.kind == "command":
            return " ".join(self.argv)
        if self.kind == "copy":
            return f"copy Pair Desk from {fwd(source)} to {fwd(app)}"
        if self.kind == "remove-app":
            return f"remove {fwd(app)} (the Pair Desk copy) once no tool uses it"
        return "python " + " ".join(fwd(a) if i == 0 else a for i, a in enumerate(self.argv))


@dataclass
class Context:
    source: Path                   # where this code runs from
    app: Path                      # stable source used by the MCP integrations
    marketplace: str = MARKETPLACE_REPO
    python: str | None = None      # --python for the integration installers

    @property
    def copies(self) -> bool:
        return fwd(self.source) != fwd(self.app)


def make_context(app: Path | None = None, marketplace: str | None = None, python: str | None = None) -> Context:
    source = REPO_ROOT
    if app is None:
        app = source if is_clone(source) else app_dir()
    return Context(source=source, app=Path(os.path.abspath(app)), marketplace=marketplace or MARKETPLACE_REPO,
                   python=python)


def plan_for(tool: str, action: str, ctx: Context) -> list[Step]:
    """The exact steps for one tool and action. Nothing else is ever run."""
    if tool == "claude":
        if action == "install":
            return [
                # adding a marketplace that exists fails harmlessly; the update refreshes it
                Step("command", ["claude", "plugin", "marketplace", "add", ctx.marketplace], allow_failure=True, tool=tool),
                Step("command", ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME], tool=tool),
                Step("command", ["claude", "plugin", "install", PLUGIN_ID], tool=tool),
            ]
        if action == "update":
            return [
                Step("command", ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME], tool=tool),
                Step("command", ["claude", "plugin", "update", PLUGIN_ID], tool=tool),
            ]
        return [Step("command", ["claude", "plugin", "uninstall", PLUGIN_ID], tool=tool)]
    script = str(ctx.app / "integrations" / tool / "install.py")
    extra = ["--python", ctx.python] if ctx.python and action != "uninstall" else []
    if action == "uninstall":
        steps = [Step("integration", [script, "--uninstall"], tool=tool)]
        if ctx.copies:
            steps.append(Step("remove-app", tool=tool))
        return steps
    steps = [Step("copy", tool=tool)] if ctx.copies else []
    return steps + [Step("integration", [script, *extra], tool=tool)]


NOTES = {
    "claude": "  Restart Claude Code (or run /reload-plugins) afterwards.",
    "codex": "  Codex runs a new or changed hook only after you trust it: start codex, run /hooks and trust the "
             "Pair Desk SessionStart hook.",
    "opencode": "  Restart opencode afterwards.",
    "antigravity": "  Restart Google Antigravity and refresh its MCP servers afterwards. "
                   "Session context and owner activity use the installed global rules.",
}


# -- asking -------------------------------------------------------------------------------

def _terminal_input(prompt: str) -> str:
    """Ask on the terminal. Without one (CI, a pipe) the answer is "no". install.sh hands the
    terminal to Python even when the script itself arrives through `curl ... | sh`."""
    if sys.stdin is None or not sys.stdin.isatty():
        print(prompt + "(no terminal to ask on: no)")
        return ""
    try:
        return input(prompt)
    except EOFError:
        return ""


def confirm(prompt: str, ask=_terminal_input) -> bool:
    return ask(prompt).strip().lower() in ("y", "yes")


# -- doing ------------------------------------------------------------------------------------

def _which(name: str) -> str | None:
    return shutil.which(name)


def tool_available(tool: str, explicit: bool = False) -> bool:
    """Antigravity is a desktop app; its optional CLI need not be on PATH."""
    if _which(tool):
        return True
    if tool != "antigravity":
        return False
    from integrations.antigravity.install import config_dir
    candidates = (Path("/Applications/Antigravity.app"), Path.home() / "Applications" / "Antigravity.app",
                  Path("/usr/share/applications/antigravity.desktop"),
                  Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
                  / "Programs" / "Antigravity" / "Antigravity.exe")
    return explicit or config_dir().is_dir() or any(path.exists() for path in candidates)


def run_command(argv: list[str]) -> int:
    sys.stdout.flush()
    exe = _which(argv[0])
    if not exe:
        return 127
    try:
        return subprocess.run([exe, *argv[1:]]).returncode  # a .cmd shim runs by full path
    except OSError as e:
        print(f"  {e}")
        return 1


def _read_app_marker(app: Path) -> dict:
    try:
        data = json.loads((app / APP_MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_app_marker(app: Path, used_by: set[str]) -> None:
    from pair_desk import VERSION
    (app / APP_MARKER).write_text(json.dumps({"version": VERSION, "used_by": sorted(used_by)}, indent=2) + "\n",
                                  encoding="utf-8")


def copy_app(source: Path, app: Path, tool: str) -> None:
    """Copy (or refresh) Pair Desk into `app`. A folder that is not ours is never overwritten."""
    marker = _read_app_marker(app)
    if app.exists() and any(app.iterdir()) and not marker:
        raise RuntimeError(f"{app} exists and was not created by the Pair Desk installer; "
                           "pick another folder with --app-dir or PAIR_DESK_APP")
    app.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for item in APP_ITEMS:
        src, dst = source / item, app / item
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=ignore)
        elif src.is_file():
            shutil.copy2(src, dst)
    ensure_executable(app / LAUNCHER)
    _write_app_marker(app, set(marker.get("used_by", [])) | {tool})


def release_app(app: Path, tool: str, dry_run: bool) -> str:
    """Drop `tool` from the app folder's users; remove the folder when none is left."""
    marker = _read_app_marker(app)
    if not marker:
        return f"  {app} is not a Pair Desk copy made by this installer; left alone"
    users = set(marker.get("used_by", [])) - {tool}
    if users:
        if not dry_run:
            _write_app_marker(app, users)
        return f"  keep {app} (still used by {', '.join(sorted(users))})"
    if not dry_run:
        shutil.rmtree(app, ignore_errors=True)
    return f"  {'would remove' if dry_run else 'removed'} {app}"


def run_integration(step: Step, dry_run: bool) -> int:
    argv = [sys.executable, *step.argv] + (["--dry-run"] if dry_run else [])
    sys.stdout.flush()
    try:
        return subprocess.run(argv).returncode
    except OSError as e:
        print(f"  {e}")
        return 1


# -- linking the current repo --------------------------------------------------------------

def git_root(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return None


def _slug_for(folder: Path) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-")[:40]
    return slug if slug and slug[0].isalnum() else "game"


def offer_link(data_dir: Path, cwd: Path, link: str | None, no_link: bool, yes: bool, dry_run: bool,
               ask=_terminal_input) -> None:
    from pair_desk.context import resolve_project, save_link
    repo = git_root(cwd)
    if no_link or repo is None:
        return
    current = resolve_project(data_dir, repo) if (data_dir / "desk.sqlite").is_file() or (data_dir / "projects.json").is_file() else None
    if current and not link:
        print(f"{repo} is linked to the desk project {current}.")
        return
    slug = link
    if not slug:
        default = _slug_for(repo)
        if dry_run:
            print(f"Would offer to link {repo} to a desk project (default: {default}).")
            return
        if yes:
            print(f"Not linking {repo} (--yes asks nothing); link it with --link <slug>, or later with "
                  f"`pair-desk link --project <slug> --path {fwd(repo)}`.")
            return
        answer = ask(f"Link {repo} to a Pair Desk project? Project slug [{default}], or n to skip: ").strip()
        if answer.lower() in ("n", "no"):
            return
        slug = answer or default
    if dry_run:
        print(f"Would link {repo} to the desk project {slug} (creating it if needed).")
        return
    from pair_desk.store import DeskError, Store
    try:
        with Store(data_dir) as store:
            try:
                store.get_project(slug)
            except DeskError:
                p = store.create_project(slug, repo.name)
                print(f"Created the desk project {p['slug']} ({p['name']}); ids look like {p['prefix']}-1.")
        save_link(data_dir, repo, slug)
        print(f"Linked {repo} to {slug}.")
    except (DeskError, OSError) as e:
        print(f"Could not link {repo}: {e}")


# -- the command -------------------------------------------------------------------------------

def run(args, data_dir: Path, ask=_terminal_input) -> int:
    action: str = args.cmd
    unknown = [t for t in args.tools or [] if t not in TOOLS]
    if unknown:
        print(f"error: unknown tool {', '.join(unknown)} (choose from {', '.join(TOOLS)})", file=sys.stderr)
        return 2
    tools = [t for t in TOOLS if t in (args.tools or [])] or list(TOOLS)
    ctx = make_context(Path(args.app_dir) if args.app_dir else None, args.marketplace, args.python)
    dry = args.dry_run
    failures = 0
    ran_any = False
    if dry:
        print("Dry run: nothing is changed.")
    if action == "update" and not ctx.copies and is_clone(ctx.source):
        print(f"Codex, opencode and Antigravity run Pair Desk from the clone {fwd(ctx.source)}: "
              "update it with `git pull` there.")
    if action != "uninstall" and ctx.copies:
        print(f"Pair Desk runs from {fwd(ctx.source)}, which is not a git clone; Codex, opencode and Antigravity will use a "
              f"copy in {fwd(ctx.app)}.")
    for tool in tools:
        if not tool_available(tool, explicit=tool in (args.tools or [])):
            reason = "no desktop configuration or CLI found" if tool == "antigravity" else "not found on PATH"
            print(f"{tool} is not installed ({reason}); skipping it.")
            continue
        steps = plan_for(tool, action, ctx)
        print(f"{tool}: these steps will run:")
        for s in steps:
            print(f"  {s.describe(ctx.app, ctx.source)}")
        if action != "uninstall" and NOTES.get(tool):
            print(NOTES[tool])
        if not dry and not args.yes and not confirm(f"Run them for {tool}? [y/N] ", ask):
            print(f"Skipped {tool}.")
            continue
        ran_any = True
        for step in steps:
            if step.kind == "command":
                if dry:
                    print(f"  (dry run) {step.describe(ctx.app, ctx.source)}")
                    continue
                print(f"> {step.describe(ctx.app, ctx.source)}")
                code = run_command(step.argv)
                if code != 0 and not step.allow_failure:
                    print(f"  Command failed (exit code {code}); stopping for {tool}.")
                    failures += 1
                    break
            elif step.kind == "copy":
                if dry:
                    print(f"  (dry run) {step.describe(ctx.app, ctx.source)}")
                    continue
                try:
                    copy_app(ctx.source, ctx.app, tool)
                    print(f"  copied Pair Desk to {ctx.app}")
                except (OSError, RuntimeError) as e:
                    print(f"  {e}; stopping for {tool}.")
                    failures += 1
                    break
            elif step.kind == "integration":
                script = Path(step.argv[0])
                if dry and not script.is_file():
                    # the copy has not been made yet: run the source's installer to show the changes
                    script = ctx.source / script.relative_to(ctx.app)
                    step = Step("integration", [str(script), *step.argv[1:]], tool=tool)
                code = run_integration(step, dry)
                if code != 0:
                    print(f"  The {tool} integration failed (exit code {code}); stopping for {tool}.")
                    failures += 1
                    break
            elif step.kind == "remove-app":
                print(release_app(ctx.app, tool, dry))
    if action == "install" and not failures:
        offer_link(data_dir, Path.cwd(), args.link, args.no_link, args.yes, dry, ask)
    if failures:
        print(f"Finished with {failures} error(s); see above.")
        return 1
    if dry:
        print("Dry run done: nothing was changed.")
    elif ran_any and action == "uninstall":
        print("Done. Restart your agent sessions. The desk's data folder was left as it is.")
    elif ran_any:
        print("Done. Restart your agent sessions to load Pair Desk.")
    if action != "uninstall":
        print(f"Start the desk: {launcher_for(ctx.app)} serve --open   "
              "(in Claude Code: /agent-pair-programming:serve)")
    return 0


def add_arguments(sub, common) -> None:
    """The install, update and uninstall subcommands of desk.py."""
    for action, text in (("install", "set Pair Desk up for Claude Code, Codex, opencode and Google Antigravity"),
                         ("update", "update the Claude Code plugin and refresh the other agent integrations"),
                         ("uninstall", "remove what install added (the desk's data stays)")):
        s = sub.add_parser(action, help=text, parents=[common], description=text)
        s.add_argument("tools", nargs="*", metavar="TOOL", help="claude, codex, opencode and/or antigravity "
                       "(default: all detected tools; explicit antigravity also works without its optional CLI)")
        s.add_argument("-y", "--yes", action="store_true", help="do not ask before each tool")
        s.add_argument("--dry-run", action="store_true", help="show the plan and the config changes, change nothing")
        s.add_argument("--app-dir", help="where MCP integrations run Pair Desk from when this is not a git clone "
                                         "(default: $PAIR_DESK_APP or a per-user app folder)")
        s.add_argument("--marketplace", help=f"Claude Code marketplace source (default: {MARKETPLACE_REPO}; "
                                             "a local clone's path works too)")
        s.add_argument("--python", help="Python command for the MCP integrations (default: per OS, see "
                                        "integrations/common.py desk_command)")
        s.add_argument("--link", metavar="SLUG", help="link the current git repo to this desk project")
        s.add_argument("--no-link", action="store_true", help="do not offer to link the current git repo")
