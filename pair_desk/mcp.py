"""A stdio MCP server over the Pair Desk store (JSON-RPC 2.0, one message per line).

Claude Code launches it through the plugin manifest. It reads and writes the same data folder
as the web server and the CLI, so no HTTP server has to run. Agents may not mark anything
passed: only the owner gives that verdict.

It is also a Claude Code channel (research preview): with the session started as
`claude --dangerously-load-development-channels plugin:agent-pair-programming@agent-pair-programming`
it pushes the owner's comments, verdicts, reports and status changes into the session as
`notifications/claude/channel` messages, even while the session is idle. See notify.py.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from . import APP_NAME, VERSION, notify
from .context import resolve_project
from .store import (HANDOFF_SECTIONS, KINDS, PLAN_STATES, PRIORITIES, SIZES, STATUSES, DeskError, Invalid,
                    Store, handover_problem, location_commands, merge_location)

CHANNEL_NOTIFICATION = "notifications/claude/channel"
# Without a desk server to stream from, the channel checks the database this often (seconds).
CHANNEL_POLL = 3.0
DESK_PORT_ENV = "PAIR_DESK_PORT"

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

AGENT_FORBIDDEN = "Only the owner marks an item passed. Use auto_check for agent verification, to_check for owner review, and record evidence in a comment."

_PROJECT = {"type": "string", "description": "Project slug, e.g. mygame. Optional when the repo is linked to a project."}
_FORMAT = ('Format it for a browser: a one-line bold outcome first, then short bullets or labelled lines (**Cause:**, **Fix:**, **Verified:**, **Not verified:**), one fact per bullet, code names in backticks; never one long paragraph.')
_ID = {"type": "string", "description": "Issue id, e.g. MG-12"}
_AUTHOR = {"type": "string", "description": "Who is writing, e.g. claude. Defaults to $PAIR_DESK_AUTHOR or 'claude'."}
_SEED = {"type": "integer", "description": "World seed the place is in. A /goto only lands right in the same "
                                           "generated world. Omit to use the project's default_seed."}
_COMMANDS = {
    "type": "array",
    "description": "The game console commands that take the owner to what to look at, in order: one PLACE per "
                   "command (one /goto, at most one /spawn or /battle, plus look settings such as /time, /weather, "
                   "/fly). When the check needs several places, list several commands, each with a short label; never "
                   "chain places into one line and never leave them in a comment. The owner pastes each one into the "
                   "game console or sends it with its own button.",
    "items": {"type": "object", "properties": {
        "command": {"type": "string", "description": "One console line for one place, e.g. /goto biome glacier; /time 12:00"},
        "label": {"type": "string", "description": "A few words naming the place, e.g. 'the capital', 'back at the camp'"},
    }, "required": ["command"]},
}
_LOCATION = {
    "type": "object",
    "description": "Where to reproduce it: {commands, seed, x, y, z, yaw, pitch, place, time, weather, extra}. "
                   "`commands` is the ordered list of {command, label} console lines, one place each; `command` is "
                   "the first of them (a single command sets or replaces the first). `seed` is the world seed they "
                   "are valid in (defaults to the project's default_seed for agent checks).",
    "properties": {
        "commands": _COMMANDS,
        "command": {"type": "string", "description": "The first command only; prefer `commands`."}, "seed": _SEED,
        "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
        "yaw": {"type": "number"}, "pitch": {"type": "number"}, "place": {"type": "string"},
        "time": {"type": "string"}, "weather": {"type": "string"},
        "action": {"type": "string", "description": "For a check that is an action, not a place: what the owner does "
                   "('Continue from the title', 'Quit the game'). Stands in for `commands`; checks about a place "
                   "still give the command that takes the owner there."},
        "extra": {"type": "object"},
    },
}
_ATTACHMENT_PATHS = {
    "type": "array", "items": {"type": "string"},
    "description": "Local screenshot or PDF files to attach (png, jpg, gif, webp, pdf; max 25 MB each). Absolute "
                   "paths, or relative to the repo. Attach every screenshot the text refers to.",
}
_CHECK_FIELDS = {
    "title": {"type": "string"},
    "body": {"type": "string", "description": "Markdown: what changed and what the owner should look at. " + _FORMAT},
    "area": {"type": "string"},
    "priority": {"type": "string", "enum": list(PRIORITIES)},
    "tags": {"type": "array", "items": {"type": "string"}},
    "commands": _COMMANDS,
    "command": {"type": "string", "description": "A single game console command for one place (the first of "
                                                 "`commands`); use `commands` when there are several places."},
    "location": _LOCATION,
    "attachment_paths": _ATTACHMENT_PATHS,
    "external_ref": {"type": "string", "description": "Stable reference, e.g. a TODO.md item title or commit hash. Used to skip duplicates."},
    "size": {"type": "string", "enum": list(SIZES), "description": "Effort estimate for the backlog: S, M or L."},
    "milestone": {"type": "string", "maxLength": 120, "description": "Milestone or group name the item belongs to in the backlog; empty clears it."},
}
_STEPS = {
    "type": "array",
    "description": "Ordered plan steps: text, or {text, state, commit, note}. A step whose text matches a current "
                   "step keeps its state, commit and note.",
    "items": {"anyOf": [{"type": "string"}, {"type": "object", "properties": {
        "text": {"type": "string"}, "state": {"type": "string", "enum": list(PLAN_STATES)},
        "commit": {"type": "string"}, "note": {"type": "string"}}, "required": ["text"]}]},
}
_VERIFICATION = {"type": "string", "description": "One line: what proves it works (stage shots, tests, the check the owner runs)."}
_STEP_INDEX = {"type": "integer", "minimum": 1, "description": "Step number, 1 = the first step."}

TOOLS: list[dict] = [
    {
        "name": "list_projects",
        "description": "List the games tracked in Pair Desk with issue counts per status, and which project the current repo is linked to.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_issues",
        "description": "List issues of a project. Filter by status/kind/area/priority (comma separated for several), "
                       "a text query, or `since` (ISO time, updated at or after). Default order puts to_check, "
                       "reported and failed first. Bodies are omitted unless full=true.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT,
            "status": {"type": "string", "description": "e.g. 'failed,reported'. One or more of: " + ", ".join(STATUSES)},
            "kind": {"type": "string", "description": "One or more of: " + ", ".join(KINDS)},
            "area": {"type": "string"},
            "priority": {"type": "string"},
            "q": {"type": "string", "description": "Text search over titles, bodies, comments, areas, tags, refs"},
            "seed": {"type": "integer", "description": "Only issues whose location is in this world seed"},
            "merged": {"type": "boolean", "description": "Only issues that were merged into another (closed, with merged_into)"},
            "since": {"type": "string"},
            "sort": {"type": "string", "enum": ["triage", "updated", "created", "oldest", "priority", "number"]},
            "limit": {"type": "integer", "default": 50},
            "full": {"type": "boolean", "default": False},
        }},
    },
    {
        "name": "get_issue",
        "description": "Get one issue with its body, location, comments (with the owner's verdicts), attachments and activity.",
        "inputSchema": {"type": "object", "properties": {"id": _ID}, "required": ["id"]},
    },
    {
        "name": "create_issue",
        "description": "File an issue. Use status=auto_check for screenshots, logs and tests the agent can verify. Defaults: kind=check, status=to_check for an explicitly manual owner check, source=agent. milestone is supported.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT, **_CHECK_FIELDS,
            "kind": {"type": "string", "enum": list(KINDS), "default": "check"},
            "status": {"type": "string", "enum": [s for s in STATUSES if s != "passed"]},
            "author": _AUTHOR,
        }, "required": ["title"]},
    },
    {
        "name": "update_issue",
        "description": "Rewrite issue metadata, including owner report titles and descriptions. Previous owner wording is preserved in activity as owner's original. Cannot change status; use set_status. Empty strings clear optional fields; commands replaces the command list.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            **{k: v for k, v in _CHECK_FIELDS.items() if k not in ("attachment_paths", "external_ref")},
            "size": {"type": "string", "enum": [*SIZES, ""]},
            "id": _ID, "kind": {"type": "string", "enum": list(KINDS)}, "author": _AUTHOR,
        }, "required": ["id"]},
    },
    {
        "name": "comment",
        "description": "Comment on an issue (markdown). verdict='failed' moves it to failed; agents cannot give 'passed'.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID, "text": {"type": "string", "description": "Markdown. " + _FORMAT}, "author": _AUTHOR,
            "verdict": {"type": "string", "enum": ["failed"]}, "attachment_paths": _ATTACHMENT_PATHS,
        }, "required": ["id", "text"]},
    },
    {
        "name": "set_status",
        "description": "Use auto_check for automatic agent verification (screenshots, logs, tests). Record evidence, then to_check only for remaining manual owner review, or closed per project rules. in_progress while working. Agents cannot set passed.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID, "status": {"type": "string", "enum": [s for s in STATUSES if s != "passed"]}, "author": _AUTHOR,
        }, "required": ["id", "status"]},
    },
    {
        "name": "set_build",
        "description": "Publish the project's current build: what the owner plays to check changes (the player exe or "
                       "folder path, or the version string of a release), with its commit. Call it every time a new build "
                       "is published. The desk stamps it on every to_check issue (one short activity entry each) and on "
                       "every issue that reaches to_check later, and shows it with a copy button, so never paste the "
                       "build path into comments by hand. Once a project has published a build, to_check needs a "
                       "current one.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT,
            "path": {"type": "string", "description": "The player executable or build folder, or a version string, e.g. "
                                                      "D:/builds/mygame/MyGame.exe or 0.9.3"},
            "commit": {"type": "string", "description": "The commit the build was made from"},
            "label": {"type": "string", "description": "A short name for the build (default: a version string itself, "
                                                       "else the short commit)"},
            "built_at": {"type": "string", "description": "When it was built, ISO time (default: now)"},
            "author": _AUTHOR,
        }, "required": ["path"]},
    },
    {
        "name": "set_location",
        "description": "Set where the owner checks an issue: the exact game commands that take them there (the /where "
                       "line), one place per command, and their world seed, merged into the issue's location. "
                       "`commands` replaces the whole list; a lone `command` replaces only the first. Put every place "
                       "here as its own command, never only in a comment: each gets its own Copy and Send to game "
                       "button on the desk, and to_check needs at least one.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID, "commands": _COMMANDS, "location": _LOCATION, "author": _AUTHOR,
        }, "required": ["id"]},
    },
    {
        "name": "queue_command",
        "description": "Queue a chat command for the running game (it polls the desk while its dev console is on). Expires after 10 minutes.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT, "command": {"type": "string"}, "issue": _ID, "author": _AUTHOR,
        }, "required": ["command"]},
    },
    {
        "name": "set_plan",
        "description": "Write or revise an issue's plan: ordered steps plus a verification line. Plan before you code; "
                       "the owner sees it as a checklist above the timeline. Each step is one short imperative line (under ~100 "
                       "characters, no semicolon chains). Returns the plan and progress.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID, "steps": _STEPS, "verification": _VERIFICATION, "author": _AUTHOR,
        }, "required": ["id", "steps"]},
    },
    {
        "name": "update_step",
        "description": "Change one plan step: state (todo, doing, done, dropped), the commit that did it, a note, or its text. "
                       "Tick steps as commits land; the owner watches this live. A step started moves the issue to in_progress, "
                       "the last step done (or dropped) moves it to auto_check for agent verification.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID, "index": _STEP_INDEX, "state": {"type": "string", "enum": list(PLAN_STATES)},
            "commit": {"type": "string"}, "note": {"type": "string"}, "text": {"type": "string"}, "author": _AUTHOR,
        }, "required": ["id", "index"]},
    },
    {
        "name": "progress",
        "description": "Cheap live progress on an issue in one call: optionally set a plan step's state/commit and/or post a "
                       "short progress comment. Use it as you work so the owner can follow along. A step started moves the issue to "
                       "in_progress, the last step done moves it to auto_check for agent verification. Returns one line.",
        "inputSchema": {"type": "object", "properties": {
            "id": _ID, "text": {"type": "string", "description": "Short progress note (markdown, 3-8 bullets). " + _FORMAT},
            "step": _STEP_INDEX, "state": {"type": "string", "enum": list(PLAN_STATES)},
            "commit": {"type": "string"}, "author": _AUTHOR,
        }, "required": ["id"]},
    },
    {
        "name": "link_parent",
        "description": "Make an issue part of another ('part of MG-4'), or pass parent=null to unlink. The parent shows its "
                       "children and their statuses.",
        "inputSchema": {"type": "object", "properties": {
            "child": _ID, "parent": {"type": ["string", "null"], "description": "Parent issue id, or null to unlink"},
            "author": _AUTHOR,
        }, "required": ["child", "parent"]},
    },
    {
        "name": "merge_issues",
        "description": "Merge duplicate reports into one: the sources' comments, attachments, activity, body and location "
                       "move into the target's timeline ('merged from MG-n'); sources close and redirect to the target. "
                       "Suggest merges to the owner first (suggest_groups) unless they asked for it. Undo with unmerge.",
        "inputSchema": {"type": "object", "properties": {
            "target": _ID, "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "author": _AUTHOR,
        }, "required": ["target", "sources"]},
    },
    {
        "name": "unmerge",
        "description": "Undo a merge: the source issue gets back its own comments, attachments, activity, children and status.",
        "inputSchema": {"type": "object", "properties": {"id": _ID, "author": _AUTHOR}, "required": ["id"]},
    },
    {
        "name": "suggest_groups",
        "description": "Propose groups of related open items (by area, title words, location/seed and time): 'merge' for "
                       "duplicates, 'group' for related work under a parent. Proposals only; show them to the owner.",
        "inputSchema": {"type": "object", "properties": {"project": _PROJECT, "limit": {"type": "integer", "default": 20}}},
    },
    {
        "name": "get_handoff",
        "description": "Read the project handoff (sections: " + ", ".join(HANDOFF_SECTIONS) + "). Read it at session start.",
        "inputSchema": {"type": "object", "properties": {"project": _PROJECT, "version": {"type": "integer"}}},
    },
    {
        "name": "set_handoff",
        "description": "Save the whole project handoff as markdown with `## ` sections (" + ", ".join(HANDOFF_SECTIONS)
                       + "). Every save is a new version. Update it before you stop and whenever a milestone lands.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT, "markdown": {"type": "string"}, "note": {"type": "string"}, "author": _AUTHOR,
        }, "required": ["markdown"]},
    },
    {
        "name": "update_handoff",
        "description": "Replace one section of the project handoff (" + ", ".join(HANDOFF_SECTIONS) + ") and save a new version.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT, "section": {"type": "string"}, "text": {"type": "string"}, "author": _AUTHOR,
        }, "required": ["section", "text"]},
    },
    {
        "name": "import_checks",
        "description": "Bulk-file player checks (kind=check, status=to_check, source=agent). Items whose external_ref already exists in the project are skipped.",
        "inputSchema": {"type": "object", "properties": {
            "project": _PROJECT,
            "checks": {"type": "array", "items": {"type": "object", "properties": _CHECK_FIELDS, "required": ["title"]}},
            "author": _AUTHOR,
        }, "required": ["checks"]},
    },
]


def _compact(issue: dict, full: bool) -> dict:
    keys = ("id", "title", "status", "kind", "priority", "area", "tags", "size", "milestone", "source", "external_ref",
            "parent", "merged_into", "redirected_from", "updated_at", "comment_count", "attachment_count", "last_verdict")
    out = {k: issue.get(k) for k in keys if issue.get(k) not in (None, "", [])}
    prog = issue.get("plan_progress") or {}
    if prog.get("total"):
        out["plan"] = f"{prog['done']}/{prog['total']}"
    if issue.get("child_count"):
        out["children"] = f"{issue['child_done']}/{issue['child_count']} done"
    if issue.get("build"):
        out["build"] = issue["build"]["label"]
    loc = issue.get("location", {})
    commands = location_commands(loc)
    if commands:
        out["command"] = commands[0]["command"]
    if len(commands) > 1:
        out["commands"] = commands
    if loc.get("seed") is not None:
        out["seed"] = loc["seed"]
    if full:
        out["body"] = issue.get("body", "")
        out["location"] = issue.get("location", {})
    return out


class McpServer:
    def __init__(self, data_dir: Path, cwd: str | None = None):
        self.data_dir = Path(data_dir)
        self.cwd = cwd
        self._store: Store | None = None
        self.handlers: dict[str, Callable[[dict], Any]] = {
            "list_projects": self.t_list_projects,
            "list_issues": self.t_list_issues,
            "get_issue": self.t_get_issue,
            "create_issue": self.t_create_issue,
            "update_issue": self.t_update_issue,
            "comment": self.t_comment,
            "set_status": self.t_set_status,
            "set_location": self.t_set_location,
            "set_build": self.t_set_build,
            "queue_command": self.t_queue_command,
            "import_checks": self.t_import_checks,
            "set_plan": self.t_set_plan,
            "update_step": self.t_update_step,
            "progress": self.t_progress,
            "link_parent": self.t_link_parent,
            "merge_issues": self.t_merge_issues,
            "unmerge": self.t_unmerge,
            "suggest_groups": self.t_suggest_groups,
            "get_handoff": self.t_get_handoff,
            "set_handoff": self.t_set_handoff,
            "update_handoff": self.t_update_handoff,
        }
        self._out = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._channel: threading.Thread | None = None
        self.channel_state: dict[str, Any] = {"enabled": False}

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.data_dir)
        return self._store

    # -- helpers ----------------------------------------------------------------------------

    def _path_base(self) -> list[str]:
        """Where relative attachment paths resolve: the repo the agent works in."""
        return [self.cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()]

    def _author(self, args: dict) -> str:
        return str(args.get("author") or os.environ.get("PAIR_DESK_AUTHOR") or "claude")

    def _project(self, args: dict) -> str:
        if args.get("project"):
            return str(args["project"])
        linked = resolve_project(self.data_dir, self.cwd)
        if linked:
            return linked
        projects = self.store.list_projects()
        if len(projects) == 1:
            return projects[0]["slug"]
        names = ", ".join(p["slug"] for p in projects) or "none yet"
        raise Invalid(f"no project given and this repo is not linked to one (projects: {names}). "
                      "Pass `project`, or link the repo with `python desk.py link --project <slug>`.")

    # -- tools ------------------------------------------------------------------------------

    def t_list_projects(self, args):
        return {"projects": self.store.list_projects(), "linked_project": resolve_project(self.data_dir, self.cwd)}

    def t_list_issues(self, args):
        filters = {k: args[k] for k in ("status", "kind", "area", "priority", "q", "since", "sort") if args.get(k)}
        if args.get("seed") is not None:
            filters["seed"] = args["seed"]
        if args.get("merged"):
            filters["merged"] = "1"
        filters["limit"] = args.get("limit") or 50
        if not args.get("full"):
            filters["summary"] = "1"
        res = self.store.list_issues(self._project(args), filters)
        full = bool(args.get("full"))
        return {"project": res["project"], "total": res["total"], "status_counts": res["counts"]["status"],
                "issues": [_compact(i, full) for i in res["issues"]]}

    def t_get_issue(self, args):
        return self.store.get_issue(args["id"])

    def t_create_issue(self, args):
        if args.get("status") == "passed":
            raise Invalid(AGENT_FORBIDDEN)
        data = {k: v for k, v in args.items() if k not in ("project", "author")}
        data.setdefault("kind", "check")
        data.setdefault("status", "to_check" if data["kind"] == "check" else "open")
        data.setdefault("source", "agent")
        issue = self.store.create_issue(self._project(args), data, actor=self._author(args), path_base=self._path_base())
        return _compact(issue, True)

    def t_update_issue(self, args):
        allowed = {"title", "body", "kind", "priority", "area", "tags", "size", "milestone", "location", "commands", "command"}
        unknown = set(args) - allowed - {"id", "author"}
        if unknown:
            raise Invalid("update_issue cannot change: " + ", ".join(sorted(unknown)) + "; use set_status for status")
        changes = {k: v for k, v in args.items() if k in allowed}
        if not changes:
            raise Invalid("give at least one field to update")
        author = self._author(args)
        if author.lower() == "owner":
            author = "agent"
        return _compact(self.store.update_issue(args["id"], changes, actor=author), True)

    def t_comment(self, args):
        verdict = args.get("verdict") or None
        if verdict == "passed":
            raise Invalid(AGENT_FORBIDDEN)
        res = self.store.add_comment(args["id"], self._author(args), args.get("text"), verdict,
                                     attachment_paths=args.get("attachment_paths"), path_base=self._path_base(), full=False)
        return {"comment": res["comment"], "issue": _compact(res["issue"], False)}

    def t_set_location(self, args):
        new = args.get("location") or {}
        if not isinstance(new, dict):
            raise Invalid("location must be an object, e.g. {\"commands\": [{\"command\": \"/goto 12 -40\"}], \"seed\": 1234}")
        if args.get("commands") is not None:
            new = {**new, "commands": args["commands"]}
        if not new:
            raise Invalid("give `commands` (a list of {command, label}, one place each) or a `location` object")
        current = self.store.get_issue(args["id"], full=False).get("location") or {}
        issue = self.store.update_issue(args["id"], {"location": merge_location(current, new)}, actor=self._author(args))
        return {"id": issue["id"], "location": issue["location"]}

    def t_set_status(self, args):
        status = str(args.get("status", "")).lower()
        if status == "passed":
            raise Invalid(AGENT_FORBIDDEN)
        if status == "to_check":
            issue = self.store.get_issue(args["id"], full=False)
            problem = handover_problem(issue, self.store.get_project(issue["project"]))
            if problem:
                raise Invalid(problem)
        return _compact(self.store.set_status(args["id"], args["status"], self._author(args)), False)

    def t_set_build(self, args):
        return self.store.set_build(self._project(args), args.get("path"), args.get("commit"), args.get("label"),
                                    args.get("built_at"), self._author(args))

    @staticmethod
    def _plan_result(issue: dict) -> dict:
        prog = issue["plan_progress"]
        return {"id": issue["id"], "status": issue["status"], "progress": f"{prog['done']}/{prog['total']}",
                "verification": issue["plan"]["verification"],
                "steps": [{"n": n, **st} for n, st in enumerate(issue["plan"]["steps"], 1)]}

    def t_set_plan(self, args):
        return self._plan_result(self.store.set_plan(args["id"], args.get("steps"), args.get("verification"),
                                                     self._author(args)))

    def t_update_step(self, args):
        issue = self.store.update_step(args["id"], args.get("index"), args.get("state"), args.get("commit"),
                                       args.get("note"), args.get("text"), self._author(args))
        return self._plan_result(issue)

    def t_progress(self, args):
        """One call for live progress: a step change and/or a short comment; returns one line."""
        if args.get("step") is None and not str(args.get("text") or "").strip():
            raise Invalid("give text (a progress note) and/or step with a state or commit")
        author = self._author(args)
        issue = None
        if args.get("step") is not None:
            issue = self.store.update_step(args["id"], args["step"], args.get("state"), args.get("commit"),
                                           actor=author)
        if str(args.get("text") or "").strip():
            issue = self.store.add_comment(args["id"], author, args["text"])["issue"]
        prog = issue["plan_progress"]
        plan = f", plan {prog['done']}/{prog['total']}" if prog["total"] else ""
        return {"ok": f"{issue['id']} updated ({issue['status']}{plan})"}

    def t_link_parent(self, args):
        return _compact(self.store.link_parent(args["child"], args.get("parent"), self._author(args)), False)

    def t_merge_issues(self, args):
        res = self.store.merge_issues(args["target"], args.get("sources") or [], self._author(args))
        return {"target": res["target"], "merged": res["merged"], "issue": _compact(res["issue"], False)}

    def t_unmerge(self, args):
        res = self.store.unmerge(args["id"], self._author(args))
        return {"source": res["source"], "target": res["target"], "issue": _compact(res["issue"], False)}

    def t_suggest_groups(self, args):
        return self.store.suggest_groups(self._project(args), int(args.get("limit") or 20))

    def t_get_handoff(self, args):
        slug = self._project(args)
        h = self.store.get_handoff(slug, args.get("version"))
        out = {k: h[k] for k in ("project", "version", "versions", "author", "created_at", "markdown")}
        build = self.store.get_build(slug)
        if build["builds_enabled"]:
            out["build"] = build["build"]
        return out

    def t_set_handoff(self, args):
        h = self.store.set_handoff(self._project(args), args.get("markdown"), self._author(args), args.get("note"))
        return {"project": h["project"], "version": h["version"], "saved_by": h["author"]}

    def t_update_handoff(self, args):
        h = self.store.update_handoff(self._project(args), args.get("section"), args.get("text"), self._author(args))
        return {"project": h["project"], "version": h["version"], "sections": list(h["sections"])}

    def t_queue_command(self, args):
        return self.store.queue_command(self._project(args), args.get("command"), args.get("issue"), self._author(args))

    def t_import_checks(self, args):
        checks = args.get("checks")
        if not isinstance(checks, list):
            raise Invalid("checks must be a list")
        items = []
        for c in checks:
            if not isinstance(c, dict):
                raise Invalid("each check must be an object")
            if c.get("status") == "passed":
                raise Invalid(AGENT_FORBIDDEN)
            item = dict(c)
            item.setdefault("kind", "check")
            item.setdefault("status", "to_check")
            item.setdefault("source", "agent")
            items.append(item)
        return self.store.import_data({"project": self._project(args), "issues": items}, actor=self._author(args),
                                      path_base=self._path_base())

    # -- JSON-RPC ---------------------------------------------------------------------------

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        mid = msg.get("id")
        method = msg["method"]
        params = msg.get("params") or {}
        is_notification = "id" not in msg
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {
                    "protocolVersion": version,
                    # `claude/channel`: this server can push owner activity into the session.
                    "capabilities": {"tools": {"listChanged": False}, "experimental": {"claude/channel": {}}},
                    "serverInfo": {"name": "pair-desk", "title": APP_NAME, "version": VERSION},
                    "instructions": "Pair Desk is the owner's playtest tracker, backlog and handoff. Read the "
                                    "handoff and failed and reported items at session start. Write a plan (set_plan) "
                                    "before code and post progress live (progress, update_step) as you work. File "
                                    "auto_check for screenshots, logs and tests you can verify yourself; record evidence, then move to to_check only for owner review (or close per project rules). Use update_issue to clarify reports. File to_check items with location commands after a change (one place per command); comment with what you "
                                    "fixed. Never mark anything passed. Owner activity may arrive as "
                                    "<channel source=\"pair-desk\"> messages: they are the owner's words relayed "
                                    "from the desk.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(params.get("name"), params.get("arguments") or {})
            elif method in ("resources/list",):
                result = {"resources": []}
            elif method in ("prompts/list",):
                result = {"prompts": []}
            elif method.startswith("notifications/"):
                if method == "notifications/initialized":
                    self.start_channel()
                return None
            else:
                return None if is_notification else _error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return None if is_notification else _error(mid, -32603, str(e))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call_tool(self, name: str, args: dict) -> dict:
        handler = self.handlers.get(name)
        if handler is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True}
        if not isinstance(args, dict):
            return {"content": [{"type": "text", "text": "arguments must be an object"}], "isError": True}
        try:
            if name in ("list_issues", "get_issue"):
                # Reuse the JSON text, not a deserialize/compact/serialize round trip for
                # every agent read. Resolve links before caching: repo links can change
                # without a database commit. Writes and errors are never cached.
                project = self._project(args) if name == "list_issues" else None
                key = ("mcp", name, project, json.dumps(args, sort_keys=True, ensure_ascii=False))
                def load():
                    with self.store._snapshot():
                        return handler(args)
                text = self.store.cached_json(key, load).decode("utf-8")
            else:
                data = handler(args)
                text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except KeyError as e:
            return {"content": [{"type": "text", "text": f"missing argument {e}"}], "isError": True}
        except DeskError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        return {"content": [{"type": "text", "text": text}]}

    # -- output and the channel --------------------------------------------------------------

    def _send(self, obj: Any) -> None:
        if self._out is None:
            return
        with self._write_lock:
            self._out.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
            self._out.flush()

    def start_channel(self) -> None:
        if self._channel is None:
            self._channel = threading.Thread(target=self._channel_loop, name="pair-desk-channel", daemon=True)
            self._channel.start()

    def _channel_loop(self) -> None:
        """Push owner activity for the linked project as channel messages while the session runs."""
        try:
            enabled, claude_pid = notify.detect_channel()
            slug = resolve_project(self.data_dir, self.cwd) if enabled else None
            self.channel_state = {"enabled": bool(enabled and slug), "project": slug, "claude_pid": claude_pid}
            if not enabled or not slug:
                return
            store = self.store
            cursor = store.owner_events(slug, None)["cursor"]
            notify.write_heartbeat(self.data_dir, claude_pid, slug)
            waiter = _ChangeWaiter(slug)
            while not self._stop.is_set():
                waiter.wait(self._stop)
                if self._stop.is_set():
                    break
                res = store.owner_events(slug, cursor)
                cursor = res["cursor"]
                for e in reversed(res["events"]):
                    self._send({"jsonrpc": "2.0", "method": CHANNEL_NOTIFICATION,
                                "params": notify.channel_message(e, slug)})
                notify.write_heartbeat(self.data_dir, claude_pid, slug)
            notify.remove_heartbeat(self.data_dir, claude_pid)
        except Exception:  # noqa: BLE001 - the channel is optional; tools keep working
            traceback.print_exc(file=sys.stderr)

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        self._out = stdout
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                reply: Any = _error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):
                    reply = [r for r in (self.handle(m) for m in msg) if r is not None] or None
                else:
                    reply = self.handle(msg)
            if reply is not None:
                self._send(reply)
        self._stop.set()
        if self._channel is not None:
            self._channel.join(timeout=5)
        if self._store is not None:
            self._store.close()


class _ChangeWaiter:
    """Blocks until the desk may have changed: on the desk server's event stream when one runs
    (the same stream the web UI uses, so a push is immediate), else a short poll interval."""

    def __init__(self, slug: str, port: int | None = None):
        self.slug = slug
        self.port = port or int(os.environ.get(DESK_PORT_ENV) or 8765)
        self.conn: http.client.HTTPConnection | None = None
        self.resp = None
        self.retry_at = 0.0

    def _connect(self) -> bool:
        import time
        if time.time() < self.retry_at:
            return False
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=25)
            conn.request("GET", f"/api/projects/{self.slug}/events", headers={"Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status != 200:
                conn.close()
                raise OSError(f"status {resp.status}")
            self.conn, self.resp = conn, resp
            return True
        except (OSError, http.client.HTTPException):
            self.conn = self.resp = None
            self.retry_at = time.time() + 30
            return False

    def wait(self, stop: threading.Event) -> None:
        if self.resp is None and not self._connect():
            stop.wait(CHANNEL_POLL)
            return
        try:
            while not stop.is_set():
                line = self.resp.readline()
                if not line:
                    raise OSError("stream closed")
                if line.startswith(b"event: change") or line.startswith(b"event: refresh"):
                    return
                if line.startswith(b": ping"):
                    return  # at least every 15 s: lets the loop refresh its heartbeat
        except (OSError, http.client.HTTPException, ValueError):
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = self.resp = None
            stop.wait(CHANNEL_POLL)


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
