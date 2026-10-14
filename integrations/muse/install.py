"""Install Pair Desk MCP, user hooks and shared skills in Meta Muse Code.

Settings live at $XDG_CONFIG_HOME/muse/settings.json (default ~/.config/muse/settings.json).
Back up before changes, preserve unrelated servers/hooks, and remove only our own entries.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.common import (DESK_PY, MARKER, SKILL_NAMES, Report, agents_skills_dir,
                                 desk_command, install_skills, shell_line,
                                 uninstall_skills, update_config_file)
from integrations.native import check_unlinked, dump, hook_command, load_object, remove_managed_groups

TOOL = "muse"
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "user-prompt-submit"}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "muse"


def mcp_entry(prefix: list[str]) -> dict:
    return {"transport": "stdio", "command": prefix[0], "args": [*prefix[1:], "mcp"],
            "env": {"PAIR_DESK_AUTHOR": TOOL}, "enabled": True, "mode": "optional"}


def merged_settings(data: dict, prefix: list[str] | None, uninstall: bool = False) -> dict:
    # Muse 1.4.7 accepts both spellings; follow an existing map and refuse ambiguous duplicates.
    if "mcp_servers" in data and "mcpServers" in data:
        raise ValueError("both mcp_servers and mcpServers exist; resolve the duplicate maps first")
    key = "mcpServers" if "mcpServers" in data else "mcp_servers"
    servers = data.get(key, {})
    hooks = data.get("hooks", {})
    if not isinstance(servers, dict) or not isinstance(hooks, dict):
        raise ValueError("MCP servers and hooks must be JSON objects")
    if "schema_version" in data and data["schema_version"] != 1:
        raise ValueError("unsupported Muse schema_version (expected 1)")
    for event in EVENTS:
        if event in hooks and not isinstance(hooks[event], list):
            raise ValueError(f"hooks.{event} must be an array")
    existing = servers.get("pair-desk")
    if existing is not None and (not isinstance(existing, dict)
                                or not isinstance(existing.get("env"), dict)
                                or existing["env"].get("PAIR_DESK_AUTHOR") != TOOL):
        raise ValueError("the pair-desk MCP entry was not installed by Pair Desk; left alone")
    # Copy rather than mutate the caller's config, including nested maps/lists.
    data = copy.deepcopy(data)
    servers = data.setdefault(key, {})
    hooks = data.setdefault("hooks", {})
    if uninstall:
        servers.pop("pair-desk", None)
    else:
        data.setdefault("schema_version", 1)
        servers["pair-desk"] = mcp_entry(prefix)
    for event, action in EVENTS.items():
        groups = remove_managed_groups(hooks.get(event, []), TOOL)
        if not uninstall:
            groups.append({"hooks": [{"type": "command", "command": hook_command(prefix, action, TOOL),
                                      "timeout": 10}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not servers:
        data.pop(key, None)
    if not hooks:
        data.pop("hooks", None)
    return data


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install Pair Desk into Meta Muse Code (MCP, hooks, skills).")
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--skills-dir", type=Path, default=None)
    ap.add_argument("--desk", type=Path, default=DESK_PY)
    ap.add_argument("--python")
    ap.add_argument("--no-skills", action="store_true")
    ap.add_argument("--force", action="store_true", help="replace foreign same-named skills")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)
    config = (args.config_dir or config_dir()) / "settings.json"
    skills = args.skills_dir or agents_skills_dir()
    report = Report(dry_run=args.dry_run)
    try:
        check_unlinked(config)
        if not args.no_skills:
            for name in SKILL_NAMES.values():
                for leaf in ("SKILL.md", MARKER):
                    check_unlinked(skills / name / leaf)
        old, comments = load_object(config)
        prefix = None
        if not args.uninstall:
            if not args.desk.is_file():
                raise ValueError(f"{args.desk} does not exist")
            prefix = desk_command(args.desk, args.python, dry_run=args.dry_run)
        new = merged_settings(old, prefix, args.uninstall)
        if old != new and comments:
            report.note("note: settings.json comments are dropped on rewrite (the backup keeps them)")
        update_config_file(config, dump(new) if old != new else None, report, "Muse MCP server and hooks")
        if not args.no_skills:
            if args.uninstall:
                uninstall_skills(skills, TOOL, report)
            else:
                install_skills(skills, TOOL, report, shell_line(prefix), force=args.force)
        report.print()
        if not args.uninstall:
            print("Restart Muse Code; inspect `/mcp` and `muse skills list`. User hooks run from your "
                  "settings; MCP tools keep your existing approval policy.")
        return 0
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
