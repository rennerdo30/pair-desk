"""Install Pair Desk's local MCP server, native hooks, skills and rules in Antigravity.

Only the pair-desk MCP entry and a marked section of GEMINI.md are managed.
Existing config/rules are backed up before changes; repeated installs are no-ops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.common import (  # noqa: E402
    DESK_PY, SERVER_NAME, Report, desk_command, install_skills,
    read_text, shell_line, uninstall_skills, update_config_file)

TOOL = "antigravity"
RULE_START = "\n\n<!-- >>> pair-desk antigravity >>> -->\n"
RULE_END = "<!-- <<< pair-desk antigravity <<< -->\n"


def config_dir() -> Path:
    return Path.home() / ".gemini" / "config"


def mcp_entry(prefix: list[str]) -> dict:
    return {"command": prefix[0], "args": [*prefix[1:], "mcp"],
            "env": {"PAIR_DESK_AUTHOR": TOOL}}


def merged_hooks(text: str | None, prefix: list[str] | None) -> str | None:
    data = json.loads(text) if text and text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("the hooks config must be a JSON object")
    if prefix is None:
        if SERVER_NAME not in data:
            return None
        del data[SERVER_NAME]
    else:
        entry = {
            event: [{"type": "command", "command": shell_line([*prefix, "hook", cli_event]), "timeout": 10}]
            for event, cli_event in (("SessionStart", "antigravity-session-start"),
                                     ("PreInvocation", "antigravity-pre-invocation"))
        }
        old = data.get(SERVER_NAME)
        if isinstance(old, dict) and "enabled" in old:
            entry["enabled"] = old["enabled"]
        if data.get(SERVER_NAME) == entry:
            return None
        data[SERVER_NAME] = entry
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def merged_config(text: str | None, prefix: list[str] | None) -> str | None:
    """Merge an MCP entry, or remove it when prefix is None, preserving other settings."""
    if prefix is None and not text:
        return None
    data = json.loads(text) if text and text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("the MCP config must be a JSON object")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError('"mcpServers" must be a JSON object')
    if prefix is None:
        if SERVER_NAME not in servers:
            return None
        del servers[SERVER_NAME]
        if not servers:
            del data["mcpServers"]
    else:
        want = mcp_entry(prefix)
        existing = servers.get(SERVER_NAME)
        if isinstance(existing, dict):
            for key in ("cwd", "disabled", "disabledTools"):
                if key in existing:
                    want[key] = existing[key]
            if isinstance(existing.get("env"), dict):
                want["env"] = {**want["env"], **existing["env"]}
        if servers.get(SERVER_NAME) == want:
            return None
        data.setdefault("mcpServers", {})[SERVER_NAME] = want
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def rules_text(prefix: list[str]) -> str:
    command = shell_line(prefix)
    return f"""## Pair Desk

At the beginning of work in a workspace, use the pair-desk skill. Resolve the desk project
from that workspace's .pair-desk.json or the desk's external repo link; never assume the MCP
process was started in the workspace. Pass the resolved project explicitly to MCP tools.
If this workspace is not linked to a desk project, leave Pair Desk alone.

For a linked workspace, read the handoff, failed checks, new reports and auto_check queue
before coding. Write a plan, record progress and verification evidence, and update the handoff
before ending the session. Use the pair-desk MCP server; all desk tools are available locally.
Only the owner may mark an issue passed. The pair-desk-serve and pair-desk-triage skills start
the UI and triage the desk. If MCP is unavailable, use the CLI: {command}.

The installed SessionStart and PreInvocation hooks supply session context and fresh owner
activity for each linked workspace. Read that context before continuing work. If hooks are
disabled, run {command} hook session-start and hook user-prompt-submit with a JSON object on
stdin containing the workspace's absolute cwd and a stable session_id. Never invent a project
link. Idle push channels remain a Claude Code feature.
"""


def merged_rules(text: str | None, prefix: list[str] | None) -> str | None:
    old = text or ""
    if old.count(RULE_START) != old.count(RULE_END) or old.count(RULE_START) > 1:
        raise ValueError("the Pair Desk rules markers are incomplete or duplicated")
    if RULE_START in old:
        start = old.index(RULE_START)
        end = old.index(RULE_END)
        if end < start:
            raise ValueError("the Pair Desk rules markers are out of order")
        end += len(RULE_END)
        managed = RULE_START + rules_text(prefix) + RULE_END if prefix else ""
        new = old[:start] + managed + old[end:]
    else:
        new = old + RULE_START + rules_text(prefix) + RULE_END if prefix else old
    return new if new != old else None


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install Pair Desk (MCP server, hooks, skills, rules) into Google Antigravity.")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config-dir", type=Path, help="MCP/hooks directory (default: ~/.gemini/config)")
    ap.add_argument("--rules-file", type=Path, help="default: ~/.gemini/GEMINI.md")
    ap.add_argument("--skills-dir", type=Path, help="app/IDE skills (default: ~/.gemini/config/skills)")
    ap.add_argument("--cli-skills-dir", type=Path, help="CLI skills (default: ~/.gemini/antigravity-cli/skills)")
    ap.add_argument("--desk", type=Path, default=DESK_PY)
    ap.add_argument("--python", help="Python command to run desk.py (default: platform launcher)")
    ap.add_argument("--no-skills", action="store_true")
    ap.add_argument("--no-rules", action="store_true")
    ap.add_argument("--no-hooks", action="store_true")
    ap.add_argument("--force", action="store_true", help="replace foreign skills with the same names")
    args = ap.parse_args(argv)
    folder = args.config_dir or config_dir()
    config = folder / "mcp_config.json"
    hooks = folder / "hooks.json"
    rules = args.rules_file or Path.home() / ".gemini" / "GEMINI.md"
    skills_dirs = {args.skills_dir or folder / "skills",
                   args.cli_skills_dir or Path.home() / ".gemini" / "antigravity-cli" / "skills"}
    report = Report(dry_run=args.dry_run)
    try:
        if not args.uninstall and not args.desk.is_file():
            raise ValueError(f"{args.desk} does not exist")
        prefix = None if args.uninstall else desk_command(args.desk, args.python)
        # Validate all configuration files before writing any of them.
        new_config = merged_config(read_text(config), prefix)
        new_rules = None if args.no_rules else merged_rules(read_text(rules), prefix)
        new_hooks = None if args.no_hooks else merged_hooks(read_text(hooks), prefix)
        update_config_file(config, new_config, report, "Antigravity MCP server")
        if not args.no_rules:
            update_config_file(rules, new_rules, report, "Antigravity Pair Desk rules")
        if not args.no_hooks:
            update_config_file(hooks, new_hooks, report, "Antigravity Pair Desk hooks")
        if not args.no_skills:
            for skills in sorted(skills_dirs):
                if args.uninstall:
                    uninstall_skills(skills, TOOL, report)
                else:
                    install_skills(skills, TOOL, report, shell_line(prefix), force=args.force)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report.print()
    if not args.uninstall and report.changed and not args.dry_run:
        print("Restart Antigravity, then refresh its MCP servers. Verify by calling pair-desk list_projects.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
