"""Install an owned native Pair Desk plugin in Antigravity plugin directories.

Discovers standalone ~/.gemini/config/plugins/pair-desk (when `agy` is on PATH or ~/.gemini/config
exists), $GEMINI_HOME/config/plugins/pair-desk, and T3 Code isolated Antigravity profiles under
~/.t3/userdata/providers/antigravity/*/config/plugins/pair-desk. Explicit --plugin-dir overrides
auto-discovery.

Includes MCP, SessionStart/PreInvocation hooks and three plugin skills. No permission grants
are added. --dry-run is read-only; uninstall removes only the files/skills managed by us.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.common import (DESK_PY, MARKER, SKILL_NAMES, Report, backup, desk_command, fwd,
                                 install_skills, shell_line, uninstall_skills, update_config_file)
from integrations.native import check_unlinked, dump, hook_command, load_object, managed_handler
from pair_desk import VERSION

TOOL = "antigravity"
FILES = ("plugin.json", "mcp_config.json", "hooks.json")


def plugin_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "plugins" / "pair-desk"


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = os.path.normcase(fwd(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def t3_plugin_dirs(home: Path | None = None) -> list[Path]:
    """Return plugin folders for initialized T3 Code Antigravity profiles."""
    root = (home or Path.home()) / ".t3" / "userdata" / "providers" / "antigravity"
    if not root.exists() or _is_reparse_or_symlink(root) or not root.is_dir():
        return []
    found: list[Path] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        if _is_reparse_or_symlink(entry) or not entry.is_dir():
            continue
        config_dir = entry / "config"
        if config_dir.is_dir():
            found.append(config_dir / "plugins" / "pair-desk")
    return found


def discover_plugin_dirs(home: Path | None = None, has_agy: bool | None = None,
                         env: dict[str, str] | None = None) -> list[Path]:
    """Discover standalone, GEMINI_HOME, and T3 Code Antigravity plugin directories."""
    base = home or Path.home()
    environ = os.environ if env is None else env
    agy_present = bool(shutil.which("agy")) if has_agy is None else has_agy
    found: list[Path] = []
    if agy_present or (base / ".gemini" / "config").is_dir():
        found.append(base / ".gemini" / "config" / "plugins" / "pair-desk")
    gemini_home = environ.get("GEMINI_HOME", "").strip()
    if gemini_home:
        gh = Path(gemini_home).expanduser()
        if agy_present or (gh / "config").is_dir():
            found.append(gh / "config" / "plugins" / "pair-desk")
    found.extend(t3_plugin_dirs(base))
    return _dedupe_paths(found)


def plugin_files(prefix: list[str]) -> dict[str, dict]:
    return {
        "plugin.json": {"$schema": "https://antigravity.google/schemas/v1/plugin.json",
                        "name": "pair-desk", "version": VERSION,
                        "description": "Pair Desk MCP, handoff and owner activity"},
        "mcp_config.json": {"mcpServers": {"pair-desk": {
            "command": prefix[0], "args": [*prefix[1:], "mcp"],
            "env": {"PAIR_DESK_AUTHOR": TOOL}}}},
        "hooks.json": {"pair-desk": {
            "SessionStart": [{"type": "command", "command": hook_command(prefix, "session-start", TOOL),
                              "timeout": 10}],
            "PreInvocation": [{"type": "command", "command": hook_command(prefix, "user-prompt-submit", TOOL),
                               "timeout": 10}]}},
    }


def _preflight_folder(folder: Path) -> None:
    for path in [folder, folder / MARKER, *(folder / name for name in FILES),
                 *(folder / "skills" / name / leaf for name in SKILL_NAMES.values()
                   for leaf in ("SKILL.md", MARKER))]:
        check_unlinked(path)
    marker, _ = load_object(folder / MARKER)
    if folder.exists() and marker.get("tool") != TOOL:
        raise ValueError(f"{folder} was not installed by Pair Desk; left alone")


def _plan_install(folder: Path, prefix: list[str]) -> dict[str, dict]:
    desired = plugin_files(prefix)
    previous = {}
    for name in FILES:
        # Invalid existing metadata is never silently replaced.
        previous[name], _ = load_object(folder / name)
    old_servers = previous["mcp_config.json"].get("mcpServers", {})
    old_hooks = previous["hooks.json"].get("pair-desk", {})
    if not isinstance(old_servers, dict) or not isinstance(old_hooks, dict):
        raise ValueError("invalid Antigravity MCP servers or Pair Desk hooks; left alone")
    existing = old_servers.get("pair-desk")
    if existing is not None and (not isinstance(existing, dict)
                                or not isinstance(existing.get("env"), dict)
                                or existing["env"].get("PAIR_DESK_AUTHOR") != TOOL):
        raise ValueError("the pair-desk MCP entry was not installed by Pair Desk; left alone")
    server = desired["mcp_config.json"]["mcpServers"]["pair-desk"]
    if existing:
        server = {**existing, **server, "env": {**existing["env"], **server["env"]}}
    merged_hooks = {**old_hooks}
    for event, handlers in desired["hooks.json"]["pair-desk"].items():
        previous_handlers = old_hooks.get(event, [])
        if not isinstance(previous_handlers, list):
            raise ValueError(f"Antigravity hooks.{event} must be an array; left alone")
        merged_hooks[event] = [h for h in previous_handlers if not managed_handler(h, TOOL)] + handlers
    desired["plugin.json"] = {**previous["plugin.json"], **desired["plugin.json"]}
    desired["mcp_config.json"] = {**previous["mcp_config.json"], "mcpServers": {
        **old_servers, "pair-desk": server}}
    desired["hooks.json"] = {**previous["hooks.json"], "pair-desk": merged_hooks}
    return desired


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install Pair Desk's native Antigravity plugin (agy / T3 Code).")
    ap.add_argument("--plugin-dir", type=Path, action="append", default=None,
                    help="target plugin folder (repeatable; overrides auto-discovery)")
    ap.add_argument("--desk", type=Path, default=DESK_PY)
    ap.add_argument("--python")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)
    folders = _dedupe_paths(args.plugin_dir) if args.plugin_dir else (discover_plugin_dirs() or [plugin_dir()])
    report = Report(dry_run=args.dry_run)
    try:
        # Preflight every managed path and target before any config or skill is written.
        for folder in folders:
            _preflight_folder(folder)
        if args.uninstall:
            kept_backups = False
            for folder in folders:
                if not folder.exists():
                    report.note(f"plugin: not installed ({folder})")
                    continue
                kept_backups = True
                uninstall_skills(folder / "skills", TOOL, report)
                for name in FILES:
                    target = folder / name
                    if target.is_file():
                        report.change(f"remove {target}")
                        if not args.dry_run:
                            # Keep any user additions in an inert backup, including edited metadata.
                            backup(target)
                            target.unlink()
                if not args.dry_run:
                    for directory in (folder / "skills", folder):
                        if directory.is_dir() and not any(directory.iterdir()):
                            directory.rmdir()
            if kept_backups:
                report.note("Inert metadata backups and the ownership marker are kept for reinstallation.")
        else:
            if not args.desk.is_file():
                raise ValueError(f"{args.desk} does not exist")
            prefix = desk_command(args.desk, args.python, dry_run=args.dry_run)
            plans = [(folder, _plan_install(folder, prefix)) for folder in folders]
            for folder, desired in plans:
                for name, data in desired.items():
                    update_config_file(folder / name, dump(data), report, f"Antigravity {name}")
                update_config_file(folder / MARKER, dump({"tool": TOOL, "source": fwd(DESK_PY.parent)}),
                                   report, "Antigravity ownership marker")
                install_skills(folder / "skills", TOOL, report, shell_line(prefix))
        report.print()
        if not args.uninstall:
            print("Restart agy (or start a new T3 Code thread). Review the plugin with `agy plugin list`, "
                  "`/mcp` and `/hooks`. Pair Desk tools use your existing permission policy.")
        return 0
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
