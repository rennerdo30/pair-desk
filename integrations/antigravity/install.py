"""Install an owned native Pair Desk plugin in ~/.gemini/config/plugins/pair-desk.

Includes MCP, SessionStart/PreInvocation hooks and three plugin skills. No permission grants
are added. --dry-run is read-only; uninstall removes only the files/skills managed by us.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.common import (DESK_PY, MARKER, SKILL_NAMES, Report, backup, desk_command, fwd,
                                 install_skills, shell_line, uninstall_skills, update_config_file)
from integrations.native import check_unlinked, dump, hook_command, load_object, managed_handler

TOOL = "antigravity"
FILES = ("plugin.json", "mcp_config.json", "hooks.json")


def plugin_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "plugins" / "pair-desk"


def plugin_files(prefix: list[str]) -> dict[str, dict]:
    return {
        "plugin.json": {"$schema": "https://antigravity.google/schemas/v1/plugin.json",
                        "name": "pair-desk", "description": "Pair Desk MCP, handoff and owner activity"},
        "mcp_config.json": {"mcpServers": {"pair-desk": {
            "command": prefix[0], "args": [*prefix[1:], "mcp"],
            "env": {"PAIR_DESK_AUTHOR": TOOL}}}},
        "hooks.json": {"pair-desk": {
            "SessionStart": [{"type": "command", "command": hook_command(prefix, "session-start", TOOL),
                              "timeout": 10}],
            "PreInvocation": [{"type": "command", "command": hook_command(prefix, "user-prompt-submit", TOOL),
                               "timeout": 10}]}},
    }


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install Pair Desk's native Antigravity plugin (agy).")
    ap.add_argument("--plugin-dir", type=Path, default=None)
    ap.add_argument("--desk", type=Path, default=DESK_PY)
    ap.add_argument("--python")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)
    folder = args.plugin_dir or plugin_dir()
    report = Report(dry_run=args.dry_run)
    try:
        # Preflight every managed path before any config or skill is written.
        for path in [folder, folder / MARKER, *(folder / name for name in FILES),
                     *(folder / "skills" / name / leaf for name in SKILL_NAMES.values()
                       for leaf in ("SKILL.md", MARKER))]:
            check_unlinked(path)
        marker, _ = load_object(folder / MARKER)
        if folder.exists() and marker.get("tool") != TOOL:
            raise ValueError(f"{folder} was not installed by Pair Desk; left alone")
        if args.uninstall:
            if not folder.exists():
                report.note(f"plugin: not installed ({folder})")
            else:
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
                report.note("Inert metadata backups and the ownership marker are kept for reinstallation.")
        else:
            if not args.desk.is_file():
                raise ValueError(f"{args.desk} does not exist")
            prefix = desk_command(args.desk, args.python, dry_run=args.dry_run)
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
            for name, data in desired.items():
                update_config_file(folder / name, dump(data), report, f"Antigravity {name}")
            update_config_file(folder / MARKER, dump({"tool": TOOL, "source": fwd(DESK_PY.parent)}),
                               report, "Antigravity ownership marker")
            install_skills(folder / "skills", TOOL, report, shell_line(prefix))
        report.print()
        if not args.uninstall:
            print("Restart agy. Review the plugin with `agy plugin list`, `/mcp` and `/hooks`. "
                  "Pair Desk tools use your existing permission policy.")
        return 0
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
