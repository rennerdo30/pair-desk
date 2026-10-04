"""The Pair Desk store: projects, issues, comments, attachments, activity and the command queue.

One SQLite file (`desk.sqlite`) plus an `attachments/` folder inside the data folder. The web
server, the CLI and the MCP server all go through this module; there is no second
implementation of the rules.

Thread safety: a locked writer and a bounded pool of read connections. Multi-query reads use
one WAL snapshot; readers do not wait for the writer's lock. Read caches are bounded and
validated with a dedicated connection's data_version, including commits by CLI/MCP processes.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import mimetypes
import random
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable

KINDS = ("bug", "check", "idea", "task")
# `parked`: not dead, not now. Hidden from the default view, kept in the backlog.
STATUSES = ("reported", "open", "in_progress", "auto_check", "to_check", "passed", "failed", "parked", "closed")
PRIORITIES = ("p0", "p1", "p2", "p3")
SOURCES = ("owner", "agent", "game")
VERDICTS = ("passed", "failed")
# Statuses that stamp closed_at. Leaving them clears it again.
DONE_STATUSES = ("passed", "closed")
# Triage order: what the owner should look at first.
TRIAGE_ORDER = ("to_check", "auto_check", "reported", "failed", "open", "in_progress", "parked", "passed", "closed")
# Optional effort estimate for backlog ordering: small, medium, large ("" = not sized).
SIZES = ("S", "M", "L")

LOCATION_NUMBERS = ("x", "y", "z", "yaw", "pitch")
# `action`: what the owner does for a check that is an action, not a place ("Continue from the title", "Quit the
# game"); it stands in for a location command (PD-3).
LOCATION_STRINGS = ("command", "place", "time", "weather", "action")
# The world seed the location is in: a game command such as `/goto x z` only lands in the right
# spot in the same generated world. A signed 32-bit integer (the games' seed type).
LOCATION_SEED = "seed"
SEED_MIN, SEED_MAX = -(2 ** 31), 2 ** 31 - 1
# The command token that names the seed on a `/goto` statement, and the statement separator.
GOTO_COMMAND, SEED_TOKEN, COMMAND_SEPARATOR = "/goto", "seed", ";"
# An issue's location commands: an ordered list of {command, label?}, each taking the owner to ONE place. When
# a check needs several places the issue lists several commands, never one line chaining them. `location.command`
# mirrors the first entry for clients that know only one command.
LOCATION_COMMANDS = "commands"
MAX_LOCATION_COMMANDS = 20
MAX_COMMAND_CHARS, MAX_COMMAND_LABEL_CHARS = 2000, 80

COMMAND_TTL = _dt.timedelta(minutes=10)
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
# Files an agent names by local path (`attachment_paths`, MCP and CLI only, never over HTTP):
# screenshots and PDFs, checked by extension and by content, and capped in size.
MAX_PATH_ATTACHMENT_BYTES = 25 * 1024 * 1024
PATH_ATTACHMENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".pdf": "application/pdf",
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{0,7})-(\d+)\s*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL UNIQUE,
    next_number INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    location TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    external_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (project_id, number)
);
CREATE INDEX IF NOT EXISTS issues_project_status ON issues(project_id, status);
CREATE INDEX IF NOT EXISTS issues_project_ref ON issues(project_id, external_ref);
CREATE INDEX IF NOT EXISTS issues_updated ON issues(project_id, updated_at);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    verdict TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_issue ON comments(issue_id);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    comment_id INTEGER REFERENCES comments(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    stored_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attachments_issue ON attachments(issue_id);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_issue ON activity(issue_id);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
    command TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    delivered_at TEXT,
    client TEXT
);
CREATE INDEX IF NOT EXISTS commands_pending ON commands(project_id, delivered_at, created_at);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    version INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    author TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, version)
);
"""

SCHEMA_VERSION = "6"
# Columns added after version 1: (table, column, declaration). Added on open when missing, so an
# existing desk migrates in place; every new column is nullable or has a constant default, which
# older code ignores.
MIGRATIONS = (
    # version 2
    ("projects", "default_seed", "INTEGER"),
    # version 3: plans, parent/child groups, merges, notification settings
    ("projects", "notify", "TEXT NOT NULL DEFAULT '{}'"),
    ("issues", "plan", "TEXT NOT NULL DEFAULT '{}'"),
    ("issues", "parent_id", "INTEGER"),
    ("issues", "merged_into", "INTEGER"),
    ("comments", "merged_from", "INTEGER"),
    ("attachments", "merged_from", "INTEGER"),
    ("activity", "merged_from", "INTEGER"),
    # version 3: the backlog (size estimate and milestone / group)
    ("issues", "size", "TEXT NOT NULL DEFAULT ''"),
    ("issues", "milestone", "TEXT NOT NULL DEFAULT ''"),
    # version 5: the project's current build and the build stamped on each issue handed to the owner
    ("projects", "build", "TEXT NOT NULL DEFAULT '{}'"),
    ("issues", "build", "TEXT NOT NULL DEFAULT '{}'"),
)
# Indexes over migrated columns, created after the columns exist.
MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS issues_parent ON issues(parent_id)",
    "CREATE INDEX IF NOT EXISTS issues_merged ON issues(merged_into)",
)

# Additive indexes only: no schema version or stored data changes. Installed after migrations
# even on an already-current desk (older releases can still open the same database).
PERFORMANCE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS commands_issue ON commands(issue_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS attachments_comment ON attachments(comment_id)",
    "CREATE INDEX IF NOT EXISTS comments_verdict ON comments(issue_id, id DESC) WHERE verdict IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS issues_children ON issues(parent_id, status) WHERE merged_into IS NULL",
    # Cover the facet inputs instead of re-reading the large issue body/plan for every group.
    "CREATE INDEX IF NOT EXISTS issues_facets ON issues(project_id, status, kind, priority, area, size, milestone, merged_into, location)",
)

# -- plans ------------------------------------------------------------------------------------
PLAN_STATES = ("todo", "doing", "done", "dropped")
MAX_PLAN_STEPS = 100
# -- handoff ----------------------------------------------------------------------------------
# The project handoff: one markdown document per project, every save a new version. These are
# its sections, in order, as `## <name>` headings.
HANDOFF_SECTIONS = ("State", "Where work stopped", "Verified", "Next step", "Traps")
MAX_HANDOFF_CHARS = 200_000
# -- notifications ----------------------------------------------------------------------------
# Owner activity a session hears about, each switchable per project (all on by default).
NOTIFY_EVENTS = ("comments", "verdicts", "reports", "status")
# Actors whose activity counts as the owner's. The web UI writes "owner".
OWNER_ACTORS = ("owner",)
# -- builds -----------------------------------------------------------------------------------
# A project's current build: what the owner plays to check a change (the player exe or folder, or a version
# string for a game that ships releases), with its commit. Publishing one stamps it on every issue waiting in
# to_check; an issue that reaches to_check later gets the build current then. The project keeps
# {current, count} (`{}` = builds never used: nothing changes for that project), an issue its stamp.
MAX_BUILD_PATH_CHARS, MAX_BUILD_LABEL_CHARS, MAX_BUILD_COMMIT_CHARS = 1000, 80, 80
BUILD_STAMP_FIELDS = ("number", "label", "path", "commit", "built_at")


class DeskError(Exception):
    status = 400


class NotFound(DeskError):
    status = 404


class Invalid(DeskError):
    status = 400


class Conflict(DeskError):
    status = 409


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def fmt_time(t: _dt.datetime) -> str:
    """Fixed-width UTC ISO time, so string comparison in SQL orders correctly."""
    t = t.astimezone(_dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_key(key: str) -> tuple[str, int]:
    m = KEY_RE.match(str(key))
    if not m:
        raise Invalid(f"not an issue id: {key!r} (expected PREFIX-NUMBER, e.g. MG-12)")
    return m.group(1).upper(), int(m.group(2))


# ---------------------------------------------------------------------------------------------
# normalisation helpers

def _req_choice(value: Any, choices: tuple[str, ...], field: str) -> str:
    v = str(value).strip().lower() if value is not None else ""
    if v not in choices:
        raise Invalid(f"{field} must be one of {', '.join(choices)} (got {value!r})")
    return v


def _text(value: Any, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)):
        raise Invalid(f"{field} must be text")
    s = str(value)
    if field in ("title", "area", "external_ref", "author"):
        s = " ".join(s.split())
    if required and not s.strip():
        raise Invalid(f"{field} is required")
    if len(s) > limit:
        raise Invalid(f"{field} is longer than {limit} characters")
    return s


def normalize_tags(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise Invalid("tags must be a list of strings or a comma separated string")
    out: list[str] = []
    seen = set()
    for item in items:
        t = " ".join(str(item).split()).lstrip("#")
        if not t:
            continue
        if len(t) > 60:
            raise Invalid("a tag is longer than 60 characters")
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def normalize_commands(value: Any, field: str = "location.commands") -> list[dict]:
    """An ordered list of location commands, each `{command, label?}`. An item may be a bare command string;
    entries without a command are dropped, and so is a repeat of an earlier command (keeping the first)."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise Invalid(f"{field} must be a list of {{command, label}} objects")
    out: list[dict] = []
    seen: set[str] = set()
    for n, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"command": item}
        if not isinstance(item, dict):
            raise Invalid(f"{field}[{n}] must be an object {{command, label}} or a command string")
        unknown = set(item) - {"command", "label"}
        if unknown:
            raise Invalid(f"{field}[{n}] has unknown keys: {', '.join(sorted(unknown))} (only command and label)")
        command = str(item.get("command") or "").strip()
        if not command or command in seen:
            continue
        if len(command) > MAX_COMMAND_CHARS:
            raise Invalid(f"{field}[{n}].command is too long")
        label = " ".join(str(item.get("label") or "").split())
        if len(label) > MAX_COMMAND_LABEL_CHARS:
            raise Invalid(f"{field}[{n}].label is too long (at most {MAX_COMMAND_LABEL_CHARS} characters: a few words)")
        seen.add(command)
        out.append({"command": command, **({"label": label} if label else {})})
    if len(out) > MAX_LOCATION_COMMANDS:
        raise Invalid(f"{field} holds at most {MAX_LOCATION_COMMANDS} commands")
    return out


def sync_commands(commands: list[dict] | None, command: str | None) -> list[dict]:
    """The command list with the single `command` reconciled into it. Without a list, the command is the one
    entry. A command the list already holds leaves the list as it is (a client that reordered it and sent the
    old first command along); a command it does not hold is an edit by a client that knows only `command`, so it
    replaces the first entry and keeps that entry's label."""
    command = (command or "").strip()
    if commands is None:
        return [{"command": command}] if command else []
    if not command or any(c["command"] == command for c in commands):
        return commands
    if not commands:
        return [{"command": command}]
    return [{**commands[0], "command": command}, *commands[1:]]


def merge_location(current: dict | None, new: dict) -> dict:
    """`new` merged over the stored location `current`, key by key. A new command list replaces the stored one
    and the stored first command with it; a new single command (without a list) edits the stored first entry."""
    base = dict(current or {})
    if LOCATION_COMMANDS in new and "command" not in new:
        base.pop("command", None)
    return {**base, **new}


def location_commands(location: dict | None, seeded: bool = True) -> list[dict]:
    """The location's commands in order, each {command, label?}, with the world seed on each one's first
    `/goto` when `seeded` (command_with_seed); the single `command` for a location stored before the list."""
    location = location or {}
    commands = location.get(LOCATION_COMMANDS)
    if not isinstance(commands, list):
        commands = sync_commands(None, location.get("command"))
    seed = location.get(LOCATION_SEED)
    return [{**c, "command": command_with_seed(c["command"], seed) if seeded else c["command"]}
            for c in commands if isinstance(c, dict) and c.get("command")]


def normalize_location(value: Any) -> dict:
    """{command, commands, x, y, z, yaw, pitch, place, time, weather, extra} - all optional.
    A bare string is taken as the command; unknown keys move into `extra`. `commands` is the ordered list of
    {command, label?} (normalize_commands) and `command` always mirrors its first entry (sync_commands)."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        value = {"command": value}
    if not isinstance(value, dict):
        raise Invalid("location must be an object")
    out: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    commands = normalize_commands(value[LOCATION_COMMANDS]) if value.get(LOCATION_COMMANDS) is not None else None
    for k, v in value.items():
        if v is None or v == "" or k == LOCATION_COMMANDS:
            continue
        if k in LOCATION_STRINGS:
            s = str(v).strip()
            if s:
                if len(s) > MAX_COMMAND_CHARS:
                    raise Invalid(f"location.{k} is too long")
                out[k] = s
        elif k in LOCATION_NUMBERS:
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise Invalid(f"location.{k} must be a number") from None
            if f != f or f in (float("inf"), float("-inf")):
                raise Invalid(f"location.{k} must be finite")
            out[k] = int(f) if f.is_integer() else f
        elif k == LOCATION_SEED:
            out[k] = normalize_seed(v, "location.seed")
        elif k == "extra":
            if not isinstance(v, dict):
                raise Invalid("location.extra must be an object")
            extra.update(v)
            if isinstance(extra.get(LOCATION_COMMANDS), list):
                # A desk before the list moved `commands` into extra on its edits: take it back.
                moved = extra.pop(LOCATION_COMMANDS)
                if commands is None:
                    commands = normalize_commands(moved)
        else:
            extra[k] = v
    if extra:
        out["extra"] = extra
    commands = sync_commands(commands, out.pop("command", None))
    if commands:
        out["command"] = commands[0]["command"]
        out[LOCATION_COMMANDS] = commands
    return out


def stored_location(text: str | None) -> dict:
    """A stored location as clients read it: normalised, so it always carries the command list in sync with
    `command` (a row written by an older desk may lack the list or hold it in `extra`). A row that does not
    normalise is returned as stored."""
    raw = json.loads(text or "{}")
    try:
        return normalize_location(raw)
    except Invalid:
        return raw


def normalize_seed(value: Any, field: str = "seed") -> int:
    """A world seed: an integer, or text holding one (`"1234"`, `"-7"`). Anything else is refused."""
    if isinstance(value, bool):
        raise Invalid(f"{field} must be an integer")
    if isinstance(value, int):
        seed = value
    elif isinstance(value, float) and value.is_integer():
        seed = int(value)
    elif isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d{1,11}\s*", value):
        seed = int(value)
    else:
        raise Invalid(f"{field} must be an integer (got {value!r})")
    if not SEED_MIN <= seed <= SEED_MAX:
        raise Invalid(f"{field} must fit a signed 32-bit integer (got {seed})")
    return seed


def command_with_seed(command: str | None, seed: Any) -> str:
    """The game command with the world seed on its first `/goto` statement: `/goto 1 2; /time 6`
    with seed 1234 reads `/goto 1 2 seed 1234; /time 6`. Unchanged when there is no seed, no
    `/goto` statement, or the statement already names a seed. The JS twin is commandWithSeed in
    web/app.js."""
    if not command or seed is None or seed == "":
        return command or ""
    parts = command.split(COMMAND_SEPARATOR)
    for n, part in enumerate(parts):
        words = part.split()
        if not words or words[0].lower() != GOTO_COMMAND:
            continue
        if any(w.lower() == SEED_TOKEN for w in words[1:]):
            return command
        trailing = part[len(part.rstrip()):]
        parts[n] = part.rstrip() + f" {SEED_TOKEN} {seed}" + trailing
        return COMMAND_SEPARATOR.join(parts)
    return command


def normalize_notify(value: Any, base: dict | None = None) -> dict:
    """Which owner events notify agent sessions: {comments, verdicts, reports, status} booleans.
    Accepts an object (missing keys keep `base`, else default on), or a list / comma separated
    string naming the events that are on ("none" or [] turns all off)."""
    out = {k: True for k in NOTIFY_EVENTS}
    if base:
        out.update({k: bool(v) for k, v in base.items() if k in NOTIFY_EVENTS})
    if value is None:
        return out
    if isinstance(value, str):
        value = [] if value.strip().lower() in ("", "none", "off") else value.split(",")
    if isinstance(value, (list, tuple)):
        names = [str(v).strip().lower() for v in value if str(v).strip()]
        for n in names:
            if n not in NOTIFY_EVENTS:
                raise Invalid(f"notify events are {', '.join(NOTIFY_EVENTS)} (got {n!r})")
        return {k: k in names for k in NOTIFY_EVENTS}
    if not isinstance(value, dict):
        raise Invalid("notify must be an object like {\"comments\": true, \"verdicts\": false}")
    for k, v in value.items():
        if k not in NOTIFY_EVENTS:
            raise Invalid(f"notify events are {', '.join(NOTIFY_EVENTS)} (got {k!r})")
        if not isinstance(v, bool):
            raise Invalid(f"notify.{k} must be true or false")
        out[k] = v
    return out


def parse_plan(raw: Any) -> dict:
    """The stored plan: {steps: [{text, state, commit?, note?}], verification, updated_at?}.
    An issue without a plan has no steps and an empty verification line."""
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    steps = [st for st in data.get("steps") or [] if isinstance(st, dict) and st.get("text")]
    out: dict[str, Any] = {"steps": steps, "verification": str(data.get("verification") or "")}
    if data.get("updated_at"):
        out["updated_at"] = data["updated_at"]
    return out


def plan_progress(plan: dict) -> dict:
    """{done, total}: dropped steps do not count."""
    steps = [st for st in plan.get("steps", []) if st.get("state") != "dropped"]
    return {"done": sum(1 for st in steps if st.get("state") == "done"), "total": len(steps)}


# Statuses the plan moves on its own: work starting on an untriaged, open, failed or waiting issue makes it
# in_progress; the last step done queues agent verification. Parked, passed and closed issues are never moved.
PLAN_STARTS_FROM = ("reported", "open", "failed", "to_check", "auto_check")
PLAN_FINISHES_FROM = ("reported", "open", "failed", "in_progress", "auto_check", "to_check")


def plan_status(status: str, before: str | None, after: str, plan: dict) -> tuple[str, str] | None:
    """The status a step change moves its issue to, with the reason, or None: a step started (`doing`, or
    `done` straight from `todo`) on a waiting issue makes it in_progress; the last open step done makes it
    auto_check (a dropped last step finishes it too), before any manual owner handover."""
    if after == before:
        return None
    if after in ("done", "dropped") and not plan_open_steps(plan) and plan_progress(plan)["done"]:
        return ("auto_check", "every plan step is done; agent verification next") if status in PLAN_FINISHES_FROM else None
    if after in ("doing", "done") and status in PLAN_STARTS_FROM:
        return "in_progress", "a plan step started"
    return None


def parse_build_state(raw: Any) -> dict:
    """A project's stored build state: {} when the project never used builds, else {current, count}
    (`current` None after `build clear`)."""
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data:
        return {}
    current = data.get("current") if isinstance(data.get("current"), dict) and data["current"].get("path") else None
    return {"current": current, "count": int(data.get("count") or 0)}


def parse_issue_build(raw: Any) -> dict | None:
    """The build stamped on an issue, or None."""
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("path") else None


def build_stamp(build: dict) -> dict:
    """What an issue keeps of a build: its number, label, path, commit and build time."""
    return {k: build[k] for k in BUILD_STAMP_FIELDS if build.get(k) not in (None, "")}


def normalize_build_time(value: Any) -> str:
    """An ISO 8601 time (`2026-09-30T14:02`, with `Z` or an offset; without one it is local time) as the
    desk's fixed-width UTC time."""
    try:
        t = _dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise Invalid(f"built_at must be an ISO time such as 2026-09-30T14:02:00Z (got {value!r})") from None
    return fmt_time(t)


def default_build_label(path: str, commit: str, number: int) -> str:
    """A build's name when none is given: a version string names itself, else the short commit, else its number."""
    if not re.search(r"[\\/]", path) and len(path) <= MAX_BUILD_LABEL_CHARS:
        return path
    return commit[:12] if commit else f"build {number}"


def handover_problem(issue: dict, project: dict | None = None) -> str | None:
    """Why an agent may not hand `issue` (an issue dict or row with `plan` and `location`) to the owner as
    to_check yet, or None when it is ready: every plan step done or dropped, and a location command that takes
    the owner to the spot (a check the owner cannot find is not a check). With `project` (a project dict), a
    project that uses builds also needs a current build: the owner can only check what a build contains. A check
    that is an action rather than a place (pressing Continue, quitting) carries `location.action` instead of a
    command (PD-3)."""
    plan = issue["plan"] if isinstance(issue["plan"], dict) else parse_plan(issue["plan"])
    location = issue["location"] if isinstance(issue["location"], dict) else json.loads(issue["location"] or "{}")
    key = issue.get("id") if isinstance(issue, dict) else None
    open_steps = plan_open_steps(plan)
    if open_steps:
        return (f"{key or 'The issue'} still has open plan steps ({', '.join(map(str, open_steps))}). Finish them "
                "(state done with the commit) or drop them (state dropped with a note) before moving it to to_check.")
    if not location_commands(location, seeded=False) and not str(location.get("action") or "").strip():
        return (f"{key or 'The issue'} has no location command. Give it the exact game command that takes the owner "
                "to what to look at (the /where line), or one that sets the check up, before moving it to to_check. "
                "One place per command: list further places as further commands. A check that is an action, not a "
                "place (Continue from the title, quit the game), gives location.action instead (--action).")
    if project is not None and project.get("builds_enabled") and not project.get("build"):
        return (f"{key or 'The issue'} cannot go to to_check: {project.get('slug', 'the project')} has no current "
                "build. Publish the build that contains the fix first (set_build, or `pair-desk build set --path ...`); "
                "the desk stamps it on the issue.")
    return None


def plan_open_steps(plan: dict) -> list[int]:
    """1-based numbers of the steps still todo or doing."""
    return [n for n, st in enumerate(plan.get("steps", []), 1) if st.get("state") in ("todo", "doing")]


def _step_text(value: Any, field: str, limit: int) -> str:
    s = _text(value, field, limit).strip()
    return " ".join(s.split()) if field == "commit" else s


def normalize_step(value: Any, previous: dict | None = None) -> dict:
    """One plan step from text or {text, state?, commit?, note?}. Unset fields keep `previous`."""
    if isinstance(value, str):
        value = {"text": value}
    if not isinstance(value, dict):
        raise Invalid("each plan step must be text or an object {text, state, commit, note}")
    prev = previous or {}
    text = _step_text(value.get("text"), "step text", 500)
    if not text:
        raise Invalid("a plan step needs text")
    step = {"text": text, "state": prev.get("state", "todo")}
    if value.get("state") not in (None, ""):
        step["state"] = _req_choice(value["state"], PLAN_STATES, "step state")
    for field, limit in (("commit", 80), ("note", 2000)):
        v = value[field] if field in value else prev.get(field)
        v = _step_text(v, field, limit) if v not in (None, "") else ""
        if v:
            step[field] = v
    return step


def handoff_template() -> str:
    return "\n\n".join(f"## {name}\n\n" for name in HANDOFF_SECTIONS).rstrip() + "\n"


def split_handoff(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    """(preamble, [(heading, body)]) for the `## ` sections of a handoff document."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in markdown.splitlines():
        m = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
        if m and not line.startswith("###"):
            sections.append((m.group(1).strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble).strip(), [(h, "\n".join(b).strip()) for h, b in sections]


def handoff_section_name(name: str) -> str:
    """The canonical section name for `name` (case and spacing insensitive, `next`/`stopped` ok)."""
    key = " ".join(str(name or "").replace("_", " ").replace("-", " ").split()).lower()
    for canon in HANDOFF_SECTIONS:
        c = canon.lower()
        if key == c or (key and (c.startswith(key) or key in c.split())):
            return canon
    if not key:
        raise Invalid("section is required: " + ", ".join(HANDOFF_SECTIONS))
    return " ".join(str(name).split())


def join_handoff(preamble: str, sections: list[tuple[str, str]]) -> str:
    parts = [preamble] if preamble else []
    for heading, body in sections:
        parts.append(f"## {heading}\n\n{body}".rstrip())
    return "\n\n".join(parts).rstrip() + "\n"


def read_attachment_path(path: str | Path, base_dirs: Iterable[str | Path] = ()) -> tuple[str, str, bytes]:
    """A local screenshot or PDF named by path: (filename, mime, bytes). Relative paths are tried
    against each of `base_dirs` in turn. Refuses what is missing, not a regular file, too large,
    or not an image/PDF by both extension and content."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise Invalid("each attachment path must be a non-empty string")
    raw = Path(str(path).strip()).expanduser()
    candidates = [raw] if raw.is_absolute() else ([Path(b) / raw for b in base_dirs] or [raw])
    target = next((c for c in candidates if c.exists()), None)
    if target is None:
        tried = "" if raw.is_absolute() else f" (tried {', '.join(str(c) for c in candidates)})"
        raise Invalid(f"attachment path {str(path)!r} does not exist{tried}")
    if not target.is_file():
        raise Invalid(f"attachment path {str(target)!r} is not a regular file")
    expected = PATH_ATTACHMENT_TYPES.get(target.suffix.lower())
    if expected is None:
        raise Invalid(f"attachment path {str(target)!r}: only images and PDFs can be attached "
                      f"({', '.join(sorted(PATH_ATTACHMENT_TYPES))})")
    if target.stat().st_size > MAX_PATH_ATTACHMENT_BYTES:
        raise Invalid(f"attachment path {str(target)!r} is larger than "
                      f"{MAX_PATH_ATTACHMENT_BYTES // (1024 * 1024)} MB")
    blob = target.read_bytes()
    if not blob:
        raise Invalid(f"attachment path {str(target)!r} is empty")
    sniffed = "application/pdf" if blob.startswith(b"%PDF-") else sniff_mime(blob, "", None)
    if sniffed != expected:
        raise Invalid(f"attachment path {str(target)!r}: the content is not {expected} (looks like {sniffed})")
    return _safe_filename(target.name), expected, blob


def _safe_filename(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name)
    name = name.strip(". ") or "file"
    return name[:120]


def sniff_mime(data: bytes, filename: str, declared: str | None) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and re.match(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", declared) and declared != "application/octet-stream":
        return declared
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def decode_base64(data: str) -> bytes:
    if not isinstance(data, str):
        raise Invalid("data_base64 must be a string")
    if data.startswith("data:"):
        comma = data.find(",")
        data = data[comma + 1:] if comma >= 0 else ""
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("data_base64 is not valid base64") from None


def feed_cursor(conn: sqlite3.Connection) -> dict:
    """The newest comment and activity ids: a cursor meaning 'everything so far has been seen'."""
    c = conn.execute("SELECT COALESCE(MAX(id), 0) FROM comments").fetchone()[0]
    a = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    h = conn.execute("SELECT COALESCE(MAX(id), 0) FROM handoffs").fetchone()[0]
    return {"comment": c, "activity": a, "handoff": h}


def read_changes(conn: sqlite3.Connection, cursor: dict, limit: int = 500) -> dict:
    """The desk's change feed: every comment and activity entry after `cursor`, oldest first, as
    {project, issue, type: comment|activity, id, action, actor, verdict, at}. The live stream of the
    web UI (server.py), the prompt hook and the session push (notify.py) all read this one feed.
    Returns {changes, cursor}."""
    after_c, after_a = int(cursor.get("comment") or 0), int(cursor.get("activity") or 0)
    after_h = int(cursor.get("handoff") or 0)
    changes = []
    for r in conn.execute(
            "SELECT h.id, h.author, h.version, h.created_at, p.slug FROM handoffs h JOIN projects p ON p.id=h.project_id "
            "WHERE h.id>? ORDER BY h.id LIMIT ?", (after_h, limit)).fetchall():
        changes.append({"project": r[4], "issue": None, "type": "handoff", "id": r[0], "action": "handoff",
                        "actor": r[1], "verdict": None, "at": r[3], "version": r[2]})
        after_h = max(after_h, r[0])
    for r in conn.execute(
            "SELECT c.id, c.author, c.verdict, c.created_at, i.number, p.prefix, p.slug FROM comments c "
            "JOIN issues i ON i.id=c.issue_id JOIN projects p ON p.id=i.project_id WHERE c.id>? ORDER BY c.id LIMIT ?",
            (after_c, limit)).fetchall():
        changes.append({"project": r[6], "issue": f"{r[5]}-{r[4]}", "type": "comment", "id": r[0],
                        "action": "verdict" if r[2] else "comment", "actor": r[1], "verdict": r[2], "at": r[3]})
        after_c = max(after_c, r[0])
    for r in conn.execute(
            "SELECT a.id, a.actor, a.action, a.created_at, i.number, p.prefix, p.slug FROM activity a "
            "JOIN issues i ON i.id=a.issue_id JOIN projects p ON p.id=i.project_id WHERE a.id>? ORDER BY a.id LIMIT ?",
            (after_a, limit)).fetchall():
        changes.append({"project": r[6], "issue": f"{r[5]}-{r[4]}", "type": "activity", "id": r[0],
                        "action": r[2], "actor": r[1], "verdict": None, "at": r[3]})
        after_a = max(after_a, r[0])
    # Rows can be deleted with their issue; never move the cursor backwards.
    cur = {"comment": after_c, "activity": after_a, "handoff": after_h}
    changes.sort(key=lambda ch: (ch["at"], ch["type"], ch["id"]))
    return {"changes": changes, "cursor": cur}


# ---------------------------------------------------------------------------------------------

def cached_read(fn):
    """Cache JSON-shaped read results without exposing mutable cached objects to callers."""
    @wraps(fn)
    def read(self, *args, **kwargs):
        # Nested reads must share the caller's snapshot, rather than borrow a result from
        # another snapshot (or wait for a cache flight while occupying a pooled reader).
        if getattr(self._local, "writer", False) or getattr(self._local, "reader", None) is not None:
            return fn(self, *args, **kwargs)
        key = (fn.__name__, json.dumps([args, kwargs], sort_keys=True, ensure_ascii=False))
        loaded = []
        def load():
            with self._snapshot():
                result = fn(self, *args, **kwargs)
                loaded.append(result)
                return result
        payload = self.cached_json(key, load)
        # A miss already has a fresh caller-owned object; do not deserialize it again.
        return loaded[0] if loaded else json.loads(payload)
    return read


class Store:
    MAX_READERS = 8
    WRITE_TIMEOUT = 10.0
    CACHE_BYTES = 16 * 1024 * 1024
    CACHE_ENTRIES = 64

    def __init__(self, data_dir: str | Path, clock: Callable[[], _dt.datetime] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir = self.data_dir / "attachments"
        self.attachments_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "desk.sqlite"
        self.clock = clock or utc_now
        self._lock = threading.RLock()
        self._local = threading.local()
        self._readers_condition = threading.Condition()
        self._readers: list[sqlite3.Connection] = []
        self._available: list[sqlite3.Connection] = []
        self._closed = False
        self._cache_lock = threading.RLock()
        self._cache: OrderedDict = OrderedDict()
        self._cache_bytes = 0
        self._flights: dict[tuple, threading.Event] = {}
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        with self._lock:
            # executescript commits on its own, so it runs outside an explicit transaction.
            self.conn.executescript(SCHEMA)
        self._migrate()
        for sql in PERFORMANCE_INDEXES:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError as e:
                # Optional optimization, not a data migration. A read-only agent can still
                # read a current schema before the write-capable server installs indexes.
                if (getattr(e, "sqlite_errorcode", 0) & 255) == sqlite3.SQLITE_READONLY:
                    break
                raise
        self._revision_conn = self._open_reader()
        self._data_version = None
        self._revision = 0
        self._revision_time = None
        self._cache_deadline = None

    def _open_reader(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True,
                               check_same_thread=False, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _migrate(self) -> None:
        """Add the columns later versions introduced. Runs in one write transaction and re-checks
        inside it, so two processes opening an old desk at once (the server and a hook) do not
        both try to add the same column."""
        def missing(conn):
            out = []
            columns = {}
            for table, column, decl in MIGRATIONS:
                if table not in columns:
                    columns[table] = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                have = columns[table]
                if column not in have:
                    out.append((table, column, decl))
            return out

        with self._lock:
            current = self.conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if current and current[0] == SCHEMA_VERSION and not missing(self.conn):
                return
        with self._tx() as c:
            for table, column, decl in missing(c):
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            for sql in MIGRATION_INDEXES:
                c.execute(sql)
            self._migrate_location_commands(c)
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))

    @staticmethod
    def _migrate_location_commands(c) -> None:
        """Version 4: every location with a command gets the command list (`commands`, its one entry the
        command), so clients can read the list alone. Nothing else in the location changes; a location that does
        not normalise (hand-edited rows) keeps its text, and one that already has the list is left alone."""
        for row in c.execute("SELECT id, location FROM issues").fetchall():
            try:
                loc = json.loads(row["location"] or "{}")
            except ValueError:
                continue
            if not isinstance(loc, dict) or LOCATION_COMMANDS in loc or not str(loc.get("command") or "").strip():
                continue
            loc[LOCATION_COMMANDS] = [{"command": str(loc["command"]).strip()}]
            loc["command"] = loc[LOCATION_COMMANDS][0]["command"]
            c.execute("UPDATE issues SET location=? WHERE id=?", (json.dumps(loc, ensure_ascii=False), row["id"]))

    def close(self) -> None:
        with self._readers_condition:
            self._closed = True
            self._readers_condition.notify_all()
            # Callers stop dispatching new work before closing; finish outstanding reads.
            self._readers_condition.wait_for(lambda: len(self._available) == len(self._readers))
            for conn in self._readers:
                conn.close()
            self._readers.clear()
            self._available.clear()
        with self._cache_lock:
            self._revision_conn.close()
            self._cache.clear()
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing ---------------------------------------------------------------------------

    def now(self) -> str:
        return fmt_time(self.clock())

    @contextmanager
    def _tx(self):
        with self._lock:
            # SQLite's default busy handler backs off to 100ms sleeps. With many CLI/MCP
            # writers that creates long retry tails even though each commit is very short.
            # Keep the same overall deadline, but retry reservation with short jittered
            # waits; after reservation the normal statement timeout is restored.
            timeout = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
            deadline = time.monotonic() + self.WRITE_TIMEOUT
            self.conn.execute("PRAGMA busy_timeout=5")
            try:
                while True:
                    try:
                        self.conn.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as e:
                        if (getattr(e, "sqlite_errorcode", 0) & 255) != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                            raise
                        time.sleep(random.uniform(.001, .004))
            finally:
                self.conn.execute(f"PRAGMA busy_timeout={timeout}")
            self._local.writer = True
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")
            finally:
                self._local.writer = False

    @contextmanager
    def _reader(self):
        if getattr(self._local, "writer", False):
            yield self.conn
            return
        current = getattr(self._local, "reader", None)
        if current is not None:
            yield current
            return
        with self._readers_condition:
            while not self._available and len(self._readers) >= self.MAX_READERS and not self._closed:
                self._readers_condition.wait()
            if self._closed:
                raise sqlite3.ProgrammingError("Store is closed")
            if self._available:
                conn = self._available.pop()
            else:
                conn = self._open_reader()
                self._readers.append(conn)
        try:
            yield conn
        finally:
            with self._readers_condition:
                self._available.append(conn)
                self._readers_condition.notify_all()

    @contextmanager
    def _snapshot(self):
        if getattr(self._local, "reader", None) is not None or getattr(self._local, "writer", False):
            yield
            return
        with self._reader() as conn:
            conn.execute("BEGIN")
            self._local.reader = conn
            try:
                yield
            finally:
                try:
                    conn.execute("ROLLBACK")  # read-only transaction, release the WAL snapshot
                finally:
                    self._local.reader = None

    def read_revision(self) -> int:
        """A cache token that changes for *every* committed write, even by another process.

        data_version values are connection-local: always read the same observer connection.
        Never hold the writer lock here (it may be waiting for another process to commit).
        """
        with self._cache_lock:
            data_version = self._revision_conn.execute("PRAGMA data_version").fetchone()[0]
            now = self.now()
            due = self._cache_deadline is not None and self._cache_deadline <= now
            backwards = self._revision_time is not None and now < self._revision_time
            if data_version != self._data_version or due or backwards:
                self._cache.clear()
                self._cache_bytes = 0
                self._cache_deadline = None
                self._data_version = data_version
                self._revision += 1
            self._revision_time = now
            return self._revision

    def _command_cache_deadline(self, commands: list[dict]) -> None:
        """Commands expire with the clock, even when nothing commits. Invalidate every layer
        of the cache at the earliest pending command's expiry (including HTTP/MCP JSON)."""
        pending = [c["expires_at"] for c in commands if c["state"] == "pending"]
        if pending:
            with self._cache_lock:
                deadline = min(pending)
                if self._cache_deadline is None or deadline < self._cache_deadline:
                    self._cache_deadline = deadline

    def cached_json(self, key: tuple, load: Callable[[], Any]) -> bytes:
        """Bounded LRU with single-flight misses; a concurrent commit prevents publication.

        This also caches the HTTP list's serialized response, while MCP/CLI use the same
        invalidation rules. No mtime/timestamp heuristic can miss same-second writes.
        """
        while True:
            revision = self.read_revision()
            flight_key = (revision, key)
            with self._cache_lock:
                if revision != self._revision:
                    continue
                payload = self._cache.get(key)
                if payload is not None:
                    self._cache.move_to_end(key)
                    return payload
                event = self._flights.get(flight_key)
                if event is None:
                    event = self._flights[flight_key] = threading.Event()
                    break
            event.wait()
        try:
            # The loader supplies its snapshot. HTTP loaders call cached store methods;
            # holding a pooled connection while waiting for their flight could deadlock.
            payload = json.dumps(load(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with self._cache_lock:
                if self.read_revision() == revision and len(payload) <= self.CACHE_BYTES:
                    self._cache[key] = payload
                    self._cache_bytes += len(payload)
                    while self._cache_bytes > self.CACHE_BYTES or len(self._cache) > self.CACHE_ENTRIES:
                        _, old = self._cache.popitem(last=False)
                        self._cache_bytes -= len(old)
            return payload
        finally:
            with self._cache_lock:
                self._flights.pop(flight_key, None)
                event.set()

    def _read(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._reader() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def _log(self, conn, issue_id: int, actor: str, action: str, detail: dict, at: str) -> None:
        conn.execute(
            "INSERT INTO activity(issue_id, actor, action, detail, created_at) VALUES (?,?,?,?,?)",
            (issue_id, actor or "owner", action, json.dumps(detail, ensure_ascii=False), at),
        )

    # -- projects ---------------------------------------------------------------------------

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict:
        return {
            "slug": row["slug"],
            "name": row["name"],
            "prefix": row["prefix"],
            "created_at": row["created_at"],
            "issue_count": row["next_number"] - 1,
            "default_seed": row["default_seed"],
            "notify": normalize_notify(None, json.loads(row["notify"] or "{}")),
            **Store._build_fields(row),
        }

    @staticmethod
    def _build_fields(row: sqlite3.Row) -> dict:
        """`build` (the current build or None) and `builds_enabled` (the project has published a build)."""
        state = parse_build_state(row["build"] if "build" in row.keys() else None)
        return {"build": state.get("current"), "builds_enabled": bool(state)}

    def create_project(self, slug: str, name: str | None = None, prefix: str | None = None) -> dict:
        slug = str(slug or "").strip().lower()
        if not SLUG_RE.match(slug):
            raise Invalid("slug must be lowercase letters, digits and dashes (max 40), e.g. mygame")
        name = _text(name or slug, "name", 80, required=True).strip()
        if not prefix:
            letters = re.sub(r"[^A-Za-z0-9]", "", name).upper()
            prefix = (letters[:2] or slug[:2].upper())
        prefix = str(prefix).strip().upper()
        if not PREFIX_RE.match(prefix):
            raise Invalid("prefix must be 1-8 capital letters or digits starting with a letter, e.g. MG")
        with self._tx() as c:
            if c.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                raise Conflict(f"project {slug!r} already exists")
            if c.execute("SELECT 1 FROM projects WHERE prefix=?", (prefix,)).fetchone():
                raise Conflict(f"prefix {prefix!r} is already used by another project")
            c.execute("INSERT INTO projects(slug, name, prefix, created_at) VALUES (?,?,?,?)",
                      (slug, name, prefix, self.now()))
        return self.get_project(slug)

    def update_project(self, slug: str, changes: dict) -> dict:
        """Project settings: `name`, `default_seed` (the world seed agent-filed checks inherit;
        null or "" clears it) and `notify` (which owner events reach agent sessions)."""
        if not isinstance(changes, dict):
            raise Invalid("changes must be an object")
        row = self._project_row(slug)
        sets, params = [], []
        if "name" in changes:
            sets.append("name=?")
            params.append(_text(changes["name"], "name", 80, required=True).strip())
        if "default_seed" in changes:
            seed = changes["default_seed"]
            sets.append("default_seed=?")
            params.append(None if seed is None or (isinstance(seed, str) and not seed.strip())
                          else normalize_seed(seed, "default_seed"))
        if "notify" in changes:
            sets.append("notify=?")
            params.append(json.dumps(normalize_notify(changes["notify"], json.loads(row["notify"] or "{}"))))
        if sets:
            with self._tx() as c:
                c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params + [row["id"]])
        return self.get_project(slug)

    @staticmethod
    def inherits_default_seed(v: dict) -> bool:
        """Whether a new issue takes the project's default seed when its location names none: work
        filed by agents and checks the owner verifies in game. Game reports keep what they sent."""
        if v.get("source") == "game":
            return False
        return v.get("source") == "agent" or v.get("kind") == "check" or v.get("status") == "to_check"

    def backfill_seed(self, slug: str, seed: Any, actor: str | None = None) -> dict:
        """Give `seed` to every issue that would inherit the default seed if filed today
        (inherits_default_seed) and whose location names none. Returns the ids changed."""
        seed = normalize_seed(seed)
        project = self._project_row(slug)
        changed = []
        for r in self._read("SELECT p.prefix, i.number, i.kind, i.status, i.source, i.location FROM issues i "
                            "JOIN projects p ON p.id=i.project_id WHERE i.project_id=? ORDER BY i.number",
                            (project["id"],)):
            loc = json.loads(r["location"])
            if LOCATION_SEED in loc or not self.inherits_default_seed(dict(r)):
                continue
            key = f"{r['prefix']}-{r['number']}"
            self.update_issue(key, {"location": {**loc, LOCATION_SEED: seed}}, actor=actor or "desk")
            changed.append(key)
        return {"project": project["slug"], "seed": seed, "changed": changed}

    def _project_row(self, slug: str) -> sqlite3.Row:
        rows = self._read("SELECT * FROM projects WHERE slug=?", (str(slug).strip().lower(),))
        if not rows:
            raise NotFound(f"no project {slug!r}")
        return rows[0]

    def get_project(self, slug: str) -> dict:
        return self._project_dict(self._project_row(slug))

    @cached_read
    def list_projects(self) -> list[dict]:
        out = []
        for row in self._read("SELECT * FROM projects ORDER BY name COLLATE NOCASE"):
            p = self._project_dict(row)
            p["counts"] = self._status_counts(row["id"])
            out.append(p)
        return out

    def _status_counts(self, project_id: int) -> dict:
        counts = {s: 0 for s in STATUSES}
        for r in self._read("SELECT status, COUNT(*) n FROM issues WHERE project_id=? GROUP BY status", (project_id,)):
            counts[r["status"]] = r["n"]
        return counts

    @cached_read
    def project_overview(self, slug: str) -> dict:
        row = self._project_row(slug)
        pid = row["id"]
        p = self._project_dict(row)
        p["counts"] = self._status_counts(pid)
        p["areas"] = [r["area"] for r in self._read(
            "SELECT area, COUNT(*) n FROM issues WHERE project_id=? AND area<>'' GROUP BY area ORDER BY n DESC, area",
            (pid,))]
        tag_counts: dict[str, int] = {}
        for r in self._read("SELECT tags FROM issues WHERE project_id=? AND tags<>'[]'", (pid,)):
            for t in json.loads(r["tags"]):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        p["tags"] = sorted(tag_counts, key=lambda t: (-tag_counts[t], t.lower()))
        last = self._read(
            "SELECT MAX(m) m FROM (SELECT MAX(updated_at) m FROM issues WHERE project_id=? "
            "UNION ALL SELECT MAX(COALESCE(delivered_at, created_at)) FROM commands WHERE project_id=?)",
            (pid, pid))
        p["last_change"] = last[0]["m"] if last else None
        return p

    # -- issues -----------------------------------------------------------------------------

    # Columns every issue query selects: the issue, its project, and the numbers of its parent
    # and merge target (same project, so the prefix is shared), plus child progress.
    ISSUE_COLUMNS = (
        "i.*, p.prefix, p.slug,"
        " (SELECT x.number FROM issues x WHERE x.id=i.parent_id) parent_number,"
        " (SELECT x.number FROM issues x WHERE x.id=i.merged_into) merged_number,"
        " (SELECT COUNT(*) FROM issues ch WHERE ch.parent_id=i.id AND ch.merged_into IS NULL) child_count,"
        " (SELECT COUNT(*) FROM issues ch WHERE ch.parent_id=i.id AND ch.merged_into IS NULL"
        "  AND ch.status IN (" + ",".join(f"'{s}'" for s in DONE_STATUSES) + ")) child_done")

    def _issue_row(self, key: str, follow: bool = False) -> sqlite3.Row:
        """The issue row for `key`. With `follow`, a merged issue resolves to the issue it was
        merged into (its id redirects there)."""
        prefix, number = parse_key(key)
        rows = self._read(
            f"SELECT {self.ISSUE_COLUMNS} FROM issues i JOIN projects p ON p.id=i.project_id "
            "WHERE p.prefix=? AND i.number=?", (prefix, number))
        if not rows:
            raise NotFound(f"no issue {prefix}-{number}")
        row = rows[0]
        seen = {row["id"]}
        while follow and row["merged_into"] is not None:
            nxt = self._read(f"SELECT {self.ISSUE_COLUMNS} FROM issues i JOIN projects p ON p.id=i.project_id "
                             "WHERE i.id=?", (row["merged_into"],))
            if not nxt or nxt[0]["id"] in seen:
                break
            row = nxt[0]
            seen.add(row["id"])
        return row

    def _row_by_id(self, issue_id: int) -> sqlite3.Row:
        rows = self._read(f"SELECT {self.ISSUE_COLUMNS} FROM issues i JOIN projects p ON p.id=i.project_id "
                          "WHERE i.id=?", (issue_id,))
        if not rows:
            raise NotFound(f"no issue with row id {issue_id}")
        return rows[0]

    @staticmethod
    def _key(row: sqlite3.Row) -> str:
        return f"{row['prefix']}-{row['number']}"

    @staticmethod
    def _issue_dict(row: sqlite3.Row, summary: bool = False) -> dict:
        d = {
            "id": f"{row['prefix']}-{row['number']}",
            "number": row["number"],
            "project": row["slug"],
            "title": row["title"],
            "body": "" if summary else row["body"],
            "kind": row["kind"],
            "status": row["status"],
            "priority": row["priority"],
            "area": row["area"],
            "tags": json.loads(row["tags"]),
            "location": stored_location(row["location"]),
            "source": row["source"],
            "external_ref": row["external_ref"],
            "size": row["size"] if "size" in row.keys() else "",
            "milestone": row["milestone"] if "milestone" in row.keys() else "",
            "build": parse_issue_build(row["build"] if "build" in row.keys() else None),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
        }
        keys = row.keys()
        plan = parse_plan(row["plan"] if "plan" in keys else None)
        d["plan"] = plan
        d["plan_progress"] = plan_progress(plan)
        if "parent_number" in keys:
            d["parent"] = f"{row['prefix']}-{row['parent_number']}" if row["parent_number"] is not None else None
            d["merged_into"] = f"{row['prefix']}-{row['merged_number']}" if row["merged_number"] is not None else None
            d["child_count"] = row["child_count"]
            d["child_done"] = row["child_done"]
        for extra in ("comment_count", "attachment_count", "last_verdict"):
            if extra in keys:
                d[extra] = row[extra]
        if summary:
            d.pop("body")
            d.pop("plan")
        return d

    def _validate_issue_fields(self, data: dict, creating: bool) -> dict:
        v: dict[str, Any] = {}
        if creating or "title" in data:
            v["title"] = _text(data.get("title"), "title", 300, required=True).strip()
        if creating or "body" in data:
            v["body"] = _text(data.get("body"), "body", 200_000)
        if creating or "kind" in data:
            v["kind"] = _req_choice(data.get("kind") or "bug", KINDS, "kind") if creating else _req_choice(data["kind"], KINDS, "kind")
        if creating or "status" in data:
            v["status"] = _req_choice(data.get("status") or "reported", STATUSES, "status") if creating else _req_choice(data["status"], STATUSES, "status")
        if creating or "priority" in data:
            v["priority"] = _req_choice(data.get("priority") or "p2", PRIORITIES, "priority") if creating else _req_choice(data["priority"], PRIORITIES, "priority")
        if creating or "area" in data:
            v["area"] = _text(data.get("area"), "area", 80).strip()
        if creating or "tags" in data:
            v["tags"] = normalize_tags(data.get("tags"))
        if creating or "location" in data or "command" in data or LOCATION_COMMANDS in data:
            loc = normalize_location(data.get("location")) if "location" in data else {}
            if LOCATION_COMMANDS in data:
                # A top-level list replaces the location's commands.
                loc = normalize_location({**{k: val for k, val in loc.items() if k not in ("command", LOCATION_COMMANDS)},
                                          LOCATION_COMMANDS: normalize_commands(data[LOCATION_COMMANDS], "commands")})
            if "command" in data:
                # The single command addresses the first entry: text replaces it, empty text removes it.
                cmd = str(data.get("command") or "").strip()
                rest = list(loc.get(LOCATION_COMMANDS) or [])
                if cmd and LOCATION_COMMANDS in data and any(c["command"] == cmd for c in rest):
                    pass  # a client sending both, in sync
                elif cmd:
                    rest = [{**(rest[0] if rest else {}), "command": cmd}, *rest[1:]]
                else:
                    rest = rest[1:]
                loc = normalize_location({**{k: val for k, val in loc.items() if k not in ("command", LOCATION_COMMANDS)},
                                          LOCATION_COMMANDS: rest})
            v["location"] = loc
        if creating or "source" in data:
            v["source"] = _req_choice(data.get("source") or "owner", SOURCES, "source") if creating else _req_choice(data["source"], SOURCES, "source")
        if creating or "external_ref" in data:
            v["external_ref"] = _text(data.get("external_ref"), "external_ref", 300).strip()
        if creating or "size" in data:
            size = str(data.get("size") or "").strip().upper()
            if size and size not in SIZES:
                raise Invalid(f"size must be one of {', '.join(SIZES)} or empty (got {data.get('size')!r})")
            v["size"] = size
        if creating or "milestone" in data:
            v["milestone"] = _text(data.get("milestone"), "milestone", 120).strip()
        return v

    def create_issue(self, slug: str, data: dict, actor: str | None = None,
                     path_base: Iterable[str | Path] | None = None) -> dict:
        """Create an issue. `attachment_paths` (local files) are read only when the caller passes
        `path_base` (the folders relative paths resolve against): the CLI and the MCP server do,
        the HTTP API does not, so a web page can never make the desk read a local file.
        Agent work and checks without a seed take the project's `default_seed` (inherits_default_seed);
        one with no location at all gets a location holding only the seed."""
        if not isinstance(data, dict):
            raise Invalid("issue must be an object")
        project = self._project_row(slug)
        v = self._validate_issue_fields(data, creating=True)
        attachments = data.get("attachments") or []
        if not isinstance(attachments, list):
            raise Invalid("attachments must be a list")
        decoded = [self._decode_attachment(a) for a in attachments]
        decoded += self._path_attachments(data.get("attachment_paths"), path_base)
        if (project["default_seed"] is not None and LOCATION_SEED not in v["location"]
                and self.inherits_default_seed(v)):
            v["location"] = {**v["location"], LOCATION_SEED: project["default_seed"]}
        actor = actor or data.get("author") or v["source"]
        now = self.now()
        with self._tx() as c:
            number = c.execute("SELECT next_number FROM projects WHERE id=?", (project["id"],)).fetchone()[0]
            c.execute("UPDATE projects SET next_number=? WHERE id=?", (number + 1, project["id"]))
            # Filed straight into to_check: it is in the current build.
            build = self._current_build(c, project["id"]) if v["status"] == "to_check" else None
            cur = c.execute(
                "INSERT INTO issues(project_id, number, title, body, kind, status, priority, area, tags, location,"
                " source, external_ref, size, milestone, build, created_at, updated_at, closed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project["id"], number, v["title"], v["body"], v["kind"], v["status"], v["priority"], v["area"],
                 json.dumps(v["tags"], ensure_ascii=False), json.dumps(v["location"], ensure_ascii=False),
                 v["source"], v["external_ref"], v["size"], v["milestone"],
                 json.dumps(build_stamp(build) if build else {}, ensure_ascii=False), now, now,
                 now if v["status"] in DONE_STATUSES else None))
            issue_id = cur.lastrowid
            self._log(c, issue_id, actor, "created",
                      {"status": v["status"], **({"build": build["label"]} if build else {})}, now)
            for filename, mime, blob in decoded:
                self._store_attachment(c, issue_id, project["slug"], None, filename, mime, blob, actor, now)
        return self.get_issue(f"{project['prefix']}-{number}")

    def _facet_where(self, project_id: int, filters: dict, skip: str | None = None) -> tuple[str, list]:
        where = ["i.project_id=?"]
        params: list[Any] = [project_id]

        def multi(field: str, choices: tuple[str, ...] | None):
            raw = filters.get(field)
            if raw in (None, "", []) or field == skip:
                return
            vals = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
            vals = [str(x).strip() for x in vals if str(x).strip()]
            if not vals:
                return
            if choices:
                vals = [x.lower() for x in vals]
                for x in vals:
                    if x not in choices:
                        raise Invalid(f"{field} must be one of {', '.join(choices)} (got {x!r})")
                where.append(f"i.{field} IN ({','.join('?' * len(vals))})")
                params.extend(vals)
            else:
                where.append(f"lower(i.{field}) IN ({','.join('?' * len(vals))})")
                params.extend(x.lower() for x in vals)

        multi("status", STATUSES)
        multi("kind", KINDS)
        multi("priority", PRIORITIES)
        multi("source", SOURCES)
        multi("area", None)
        multi("milestone", None)
        raw_size = filters.get("size")
        if raw_size not in (None, "", []) and skip != "size":
            vals = raw_size if isinstance(raw_size, (list, tuple)) else str(raw_size).split(",")
            vals = [str(x).strip().upper() for x in vals if str(x).strip()]
            for x in vals:
                if x not in SIZES + ("NONE",):
                    raise Invalid(f"size must be one of {', '.join(SIZES)} or none (got {x!r})")
            if vals:
                where.append(f"i.size IN ({','.join('?' * len(vals))})")
                params.extend("" if x == "NONE" else x for x in vals)
        tag = filters.get("tag")
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(i.tags) WHERE lower(json_each.value)=?)")
            params.append(str(tag).strip().lstrip("#").lower())
        seed = filters.get("seed")
        if seed not in (None, "") and skip != "seed":
            where.append("json_extract(i.location, '$.seed')=?")
            params.append(normalize_seed(seed, "seed filter"))
        ref = filters.get("external_ref")
        if ref:
            where.append("i.external_ref=?")
            params.append(str(ref))
        merged = str(filters.get("merged") or "").strip().lower()
        if merged in ("1", "true", "yes") and skip != "merged":
            where.append("i.merged_into IS NOT NULL")
        since = filters.get("since")
        if since:
            where.append("i.updated_at>=?")
            params.append(str(since))
        q = str(filters.get("q") or "").strip()
        for term in q.split():
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\' OR i.area LIKE ? ESCAPE '\\'"
                " OR i.tags LIKE ? ESCAPE '\\' OR i.external_ref LIKE ? ESCAPE '\\' OR i.location LIKE ? ESCAPE '\\'"
                " OR (p.prefix || '-' || i.number) LIKE ? ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM comments cm WHERE cm.issue_id=i.id AND cm.text LIKE ? ESCAPE '\\'))")
            params.extend([like] * 8)
        return " AND ".join(where), params

    SORTS = {
        "triage": "CASE i.status " + " ".join(f"WHEN '{s}' THEN {n}" for n, s in enumerate(TRIAGE_ORDER))
                  + " END, i.priority, i.updated_at DESC",
        "updated": "i.updated_at DESC",
        "created": "i.created_at DESC",
        "oldest": "i.created_at ASC",
        "priority": "i.priority, i.updated_at DESC",
        "number": "i.number DESC",
        # The backlog: most important first, then the smaller jobs, then grouped by area.
        "backlog": "i.priority, CASE i.size WHEN 'S' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END,"
                   " lower(i.area)='', lower(i.area), i.number",
    }

    @cached_read
    def list_issues(self, slug: str, filters: dict | None = None) -> dict:
        filters = dict(filters or {})
        project = self._project_row(slug)
        pid = project["id"]
        sort = str(filters.get("sort") or "triage")
        if sort not in self.SORTS:
            raise Invalid(f"sort must be one of {', '.join(self.SORTS)}")
        try:
            limit = int(filters.get("limit") or 500)
            offset = int(filters.get("offset") or 0)
        except (TypeError, ValueError):
            raise Invalid("limit and offset must be numbers") from None
        limit = max(1, min(limit, 5000))
        offset = max(0, offset)
        summary = str(filters.get("summary") or "").strip().lower() in ("1", "true", "yes")
        where, params = self._facet_where(pid, filters)
        base = "FROM issues i JOIN projects p ON p.id=i.project_id WHERE " + where
        # Sort/page narrow ids FIRST. SQLite otherwise evaluates correlated counts for many
        # rows that the LIMIT later discards, and sorts full bodies/plans in its temp b-tree.
        # Aggregate metadata once for just this page, with indexed joins into child tables.
        order = self.SORTS[sort] + ", i.number DESC"
        # Older system SQLite libraries support CTEs but not the explicit 3.35 hint.
        # Reused CTEs still share work there; do not raise the runtime requirement.
        materialized = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""
        columns = "i.*" if not summary else ", ".join("i." + name for name in (
            "id", "project_id", "number", "title", "kind", "status", "priority", "area", "tags", "location",
            "source", "external_ref", "size", "milestone", "build", "created_at", "updated_at", "closed_at", "plan"))
        rows = self._read(
            "WITH page AS " + materialized + "(SELECT i.id " + base + " ORDER BY " + order + " LIMIT ? OFFSET ?),"
            " cm AS (SELECT c.issue_id, COUNT(*) n, MAX(CASE WHEN c.verdict IS NOT NULL THEN c.id END) verdict_id"
            " FROM page CROSS JOIN comments c ON page.id=c.issue_id GROUP BY c.issue_id),"
            " att AS (SELECT a.issue_id, COUNT(*) n FROM page CROSS JOIN attachments a ON page.id=a.issue_id GROUP BY a.issue_id),"
            " ch AS (SELECT c.parent_id, COUNT(*) n, SUM(c.status IN ('passed','closed')) done"
            " FROM page CROSS JOIN issues c ON page.id=c.parent_id WHERE c.merged_into IS NULL GROUP BY c.parent_id)"
            " SELECT " + columns + ", p.prefix, p.slug, par.number parent_number, merged.number merged_number,"
            " COALESCE(ch.n,0) child_count, COALESCE(ch.done,0) child_done,"
            " COALESCE(cm.n,0) comment_count, COALESCE(att.n,0) attachment_count, verdict.verdict last_verdict"
            " FROM page JOIN issues i ON i.id=page.id JOIN projects p ON p.id=i.project_id"
            " LEFT JOIN issues par ON par.id=i.parent_id LEFT JOIN issues merged ON merged.id=i.merged_into"
            " LEFT JOIN ch ON ch.parent_id=i.id LEFT JOIN cm ON cm.issue_id=i.id LEFT JOIN att ON att.issue_id=i.id"
            " LEFT JOIN comments verdict ON verdict.id=cm.verdict_id ORDER BY " + order,
            params + [limit, offset])
        facets = ("status", "kind", "priority", "area", "seed", "size", "milestone")
        # Search/source/tag/ref/since apply to every facet. Evaluate them once instead of
        # rescanning issue bodies and comments eight times. Each facet still ignores ONLY
        # its own filter (and merged counts retain their special status/merged semantics).
        common = {k: v for k, v in filters.items() if k not in (*facets, "merged")}
        cw, cp = self._facet_where(pid, common)
        varying = {k: v for k, v in filters.items() if k in (*facets, "merged")}
        tw, tp = self._facet_where(pid, varying)
        parts = ["SELECT 'total' facet, NULL v, COUNT(*) n FROM candidates i WHERE " + tw]
        count_params = cp + tp
        for facet in facets:
            fw, fp = self._facet_where(pid, varying, skip=facet)
            column = "i.seed" if facet == "seed" else f"i.{facet}"
            # The CTE has extracted the seed already; predicates use that same value.
            fw = fw.replace("json_extract(i.location, '$.seed')", "i.seed")
            parts.append(f"SELECT '{facet}' facet, {column} v, COUNT(*) n FROM candidates i WHERE {fw} GROUP BY {column}")
            count_params.extend(fp)
        parts[0] = parts[0].replace("json_extract(i.location, '$.seed')", "i.seed")
        # Merged issues are always closed: the count ignores the status filter, or the default view reads 0.
        mw, mp = self._facet_where(pid, {k: v for k, v in varying.items() if k not in ("merged", "status")})
        mw = mw.replace("json_extract(i.location, '$.seed')", "i.seed")
        parts.append(f"SELECT 'merged' facet, NULL v, COUNT(*) n FROM candidates i WHERE {mw} AND i.merged_into IS NOT NULL")
        count_params.extend(mp)
        counted = self._read(
            "WITH candidates AS " + materialized + "(SELECT i.project_id, i.status, i.kind, i.priority, i.area,"
            " i.size, i.milestone, i.merged_into, json_extract(i.location, '$.seed') seed "
            "FROM issues i JOIN projects p ON p.id=i.project_id WHERE " + cw + ") " + " UNION ALL ".join(parts),
            count_params)
        counts = {facet: {} for facet in facets}
        total = 0
        for r in counted:
            facet, value = r["facet"], r["v"]
            if facet == "total":
                total = r["n"]
            elif facet == "merged":
                counts["merged"] = r["n"]
            elif facet == "seed":
                if value is not None:
                    counts[facet][str(value)] = r["n"]
            elif facet not in ("size", "milestone") or value:
                counts[facet][value] = r["n"]
        return {
            "project": project["slug"],
            "total": total,
            "limit": limit,
            "offset": offset,
            "issues": [self._issue_dict(r, summary=summary) for r in rows],
            "counts": counts,
        }

    @cached_read
    def get_issue(self, key: str, full: bool = True, follow: bool = True) -> dict:
        """One issue. A merged issue's id redirects to the issue it was merged into (the result
        then carries `redirected_from`); `follow=False` returns the merged issue itself."""
        row = self._issue_row(key, follow=follow)
        d = self._issue_dict(row)
        requested = "%s-%d" % parse_key(key)
        if d["id"] != requested:
            d["redirected_from"] = requested
        if not full:
            return d
        iid = row["id"]
        origin = "(SELECT x.number FROM issues x WHERE x.id=t.merged_from) merged_number"
        prefix = row["prefix"]

        def merged_from(r) -> str | None:
            return f"{prefix}-{r['merged_number']}" if r["merged_number"] is not None else None

        atts = []
        for a in self._read(f"SELECT t.*, {origin} FROM attachments t WHERE issue_id=? ORDER BY id", (iid,)):
            ad = self._attachment_dict(a)
            ad["merged_from"] = merged_from(a)
            atts.append(ad)
        by_comment: dict[int, list] = {}
        for a in atts:
            if a["comment_id"] is not None:
                by_comment.setdefault(a["comment_id"], []).append(a)
        d["comments"] = [
            {"id": c["id"], "author": c["author"], "text": c["text"], "verdict": c["verdict"],
             "created_at": c["created_at"], "attachments": by_comment.get(c["id"], []),
             "merged_from": merged_from(c)}
            for c in self._read(f"SELECT t.*, {origin} FROM comments t WHERE issue_id=? ORDER BY id", (iid,))]
        d["attachments"] = atts
        d["activity"] = [
            {"id": a["id"], "actor": a["actor"], "action": a["action"], "detail": json.loads(a["detail"]),
             "created_at": a["created_at"], "merged_from": merged_from(a)}
            for a in self._read(f"SELECT t.*, {origin} FROM activity t WHERE issue_id=? ORDER BY id", (iid,))]
        d["children"] = [
            {"id": self._key(r), "title": r["title"], "status": r["status"], "kind": r["kind"],
             "priority": r["priority"], "plan_progress": plan_progress(parse_plan(r["plan"]))}
            for r in self._read("SELECT i.number, p.prefix, i.title, i.status, i.kind, i.priority, i.plan "
                                "FROM issues i JOIN projects p ON p.id=i.project_id "
                                "WHERE i.parent_id=? AND i.merged_into IS NULL ORDER BY i.number", (iid,))]
        d["merged_sources"] = [
            {"id": self._key(r), "title": r["title"], "merged_at": r["updated_at"]}
            for r in self._read("SELECT i.number, p.prefix, i.title, i.updated_at "
                                "FROM issues i JOIN projects p ON p.id=i.project_id "
                                "WHERE i.merged_into=? ORDER BY i.number", (iid,))]
        if d.get("parent"):
            parent = self._read("SELECT i.number, p.prefix, i.title, i.status FROM issues i "
                                "JOIN projects p ON p.id=i.project_id WHERE i.id=?", (row["parent_id"],))[0]
            d["parent_info"] = {"id": self._key(parent), "title": parent["title"], "status": parent["status"]}
        d["commands"] = [self._command_dict(c) for c in self._read(
            "SELECT * FROM commands WHERE issue_id=? ORDER BY id DESC LIMIT 10", (iid,))]
        self._command_cache_deadline(d["commands"])
        d["comment_count"] = len(d["comments"])
        d["attachment_count"] = len(atts)
        return d

    def update_issue(self, key: str, changes: dict, actor: str | None = None) -> dict:
        if not isinstance(changes, dict):
            raise Invalid("changes must be an object")
        actor = actor or changes.get("actor") or "owner"
        if str(changes.get("status", "")).strip().lower() == "passed" and str(actor).lower() != "owner":
            raise Invalid("Only the owner marks an item passed.")
        row = self._issue_row(key, follow=True)
        key = self._key(row)
        current = self._issue_dict(row)
        if ("command" in changes or LOCATION_COMMANDS in changes) and "location" not in changes:
            # A bare command edit keeps the rest of the stored location.
            changes = {**changes, "location": current["location"]}
        v = self._validate_issue_fields(changes, creating=False)
        diff = {k: val for k, val in v.items() if current.get(k) != val}
        if diff:
            now = self.now()
            with self._tx() as c:
                self._apply_changes(c, row["id"], current, diff, actor, now)
        return self.get_issue(key)

    def _apply_changes(self, c, issue_id: int, current: dict, diff: dict, actor: str, now: str,
                       reason: str | None = None) -> None:
        sets, params = [], []
        for k, val in diff.items():
            sets.append(f"{k}=?")
            params.append(json.dumps(val, ensure_ascii=False) if k in ("tags", "location") else val)
        if "status" in diff:
            sets.append("closed_at=?")
            params.append(now if diff["status"] in DONE_STATUSES else None)
            detail = {"from": current["status"], "to": diff["status"]}
            if reason:
                detail["reason"] = reason
            build = self._current_build(c, issue_id=issue_id) if diff["status"] == "to_check" else None
            if build:
                # Handed to the owner: in the build current now (a later build restamps it, set_build).
                sets.append("build=?")
                params.append(json.dumps(build_stamp(build), ensure_ascii=False))
                detail["build"] = build["label"]
            self._log(c, issue_id, actor, "status", detail, now)
        edited = [k for k in diff if k != "status"]
        if edited:
            detail: dict[str, Any] = {"fields": edited}
            if (current.get("source") == "owner" and actor.lower() != "owner"
                    and any(k in diff for k in ("title", "body"))
                    and not any("owner_original" in json.loads(r[0]) for r in c.execute(
                        "SELECT detail FROM activity WHERE issue_id=? AND action='edited'", (issue_id,)))):
                detail["owner_original"] = {"title": current["title"], "body": current["body"]}
            changes = {}
            for k in edited:
                changes[k] = {"from": current.get(k), "to": diff[k]}
            if changes:
                detail["changes"] = changes
            self._log(c, issue_id, actor, "edited", detail, now)
        sets.append("updated_at=?")
        params.append(now)
        c.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id=?", params + [issue_id])

    def set_status(self, key: str, status: str, actor: str | None = None) -> dict:
        return self.update_issue(key, {"status": status}, actor=actor or "owner")

    # -- builds -----------------------------------------------------------------------------

    @staticmethod
    def _current_build(c, project_id: int | None = None, issue_id: int | None = None) -> dict | None:
        """The current build of a project (or of the project an issue belongs to), read inside transaction `c`."""
        if project_id is None:
            row = c.execute("SELECT p.build FROM projects p JOIN issues i ON i.project_id=p.id WHERE i.id=?",
                            (issue_id,)).fetchone()
        else:
            row = c.execute("SELECT build FROM projects WHERE id=?", (project_id,)).fetchone()
        return parse_build_state(row[0]).get("current") if row else None

    def get_build(self, slug: str) -> dict:
        """{project, build (current or None), builds_enabled}."""
        project = self._project_row(slug)
        return {"project": project["slug"], **self._build_fields(project)}

    def set_build(self, slug: str, path: Any, commit: Any = None, label: Any = None, built_at: Any = None,
                  actor: str | None = None) -> dict:
        """Publish the project's current build and stamp it on every issue waiting in to_check, with one `build`
        activity entry each (issues that already carry it are left alone). `path` is the player (exe or folder)
        or a version string; `label` defaults to a version string's own text, else the short commit, else
        `build N`; `built_at` (ISO time) defaults to now. Giving the current build again (same path, commit and
        label, no time) changes nothing but stamps any to_check issue that lacks it.
        Returns {project, build, stamped: [ids], unchanged}."""
        project = self._project_row(slug)
        path = _text(path, "path", MAX_BUILD_PATH_CHARS, required=True).strip()
        commit = " ".join(_text(commit, "commit", MAX_BUILD_COMMIT_CHARS).split())
        label = " ".join(_text(label, "label", MAX_BUILD_LABEL_CHARS).split())
        when = normalize_build_time(built_at) if built_at not in (None, "") else None
        actor = _text(actor or "agent", "author", 80, required=True).strip()
        now = self.now()
        with self._tx() as c:
            state = parse_build_state(c.execute("SELECT build FROM projects WHERE id=?", (project["id"],)).fetchone()[0])
            previous = state.get("current")
            unchanged = bool(previous and previous["path"] == path and previous.get("commit", "") == commit
                             and (not label or label == previous["label"]) and when is None)
            if unchanged:
                build = previous
            else:
                number = state.get("count", 0) + 1
                build = {"number": number, "label": label or default_build_label(path, commit, number), "path": path,
                         **({"commit": commit} if commit else {}), "built_at": when or now, "set_at": now, "set_by": actor}
                c.execute("UPDATE projects SET build=? WHERE id=?",
                          (json.dumps({"current": build, "count": number}, ensure_ascii=False), project["id"]))
            stamped = []
            for r in c.execute("SELECT id, number, build FROM issues WHERE project_id=? AND status='to_check'"
                               " AND merged_into IS NULL ORDER BY number", (project["id"],)).fetchall():
                old = parse_issue_build(r["build"])
                if old and old.get("number") == build["number"] and old.get("path") == build["path"]:
                    continue
                c.execute("UPDATE issues SET build=?, updated_at=? WHERE id=?",
                          (json.dumps(build_stamp(build), ensure_ascii=False), now, r["id"]))
                detail = {"label": build["label"], "number": build["number"], "path": build["path"],
                          **({"commit": build["commit"]} if build.get("commit") else {})}
                if old:
                    detail["previous"] = old.get("label")
                self._log(c, r["id"], actor, "build", detail, now)
                stamped.append(f"{project['prefix']}-{r['number']}")
        return {"project": project["slug"], "build": build, "stamped": stamped, "unchanged": unchanged}

    def clear_build(self, slug: str, off: bool = False) -> dict:
        """No current build (the last one is gone or broken): agents cannot hand issues over until the next one
        is published. With `off`, the project stops using builds and behaves as if it never had one. Stamps
        already on issues stay."""
        project = self._project_row(slug)
        with self._tx() as c:
            state = parse_build_state(c.execute("SELECT build FROM projects WHERE id=?", (project["id"],)).fetchone()[0])
            new = {} if off or not state else {"current": None, "count": state["count"]}
            c.execute("UPDATE projects SET build=? WHERE id=?", (json.dumps(new), project["id"]))
        return self.get_build(slug)

    def delete_issue(self, key: str, actor: str | None = None) -> dict:
        row = self._issue_row(key)
        merged = self._read("SELECT number FROM issues WHERE merged_into=? ORDER BY number", (row["id"],))
        if merged:
            ids = ", ".join(f"{row['prefix']}-{r['number']}" for r in merged)
            raise Conflict(f"{self._key(row)} holds merged issues ({ids}); unmerge them first")
        paths = [a["stored_path"] for a in self._read("SELECT stored_path FROM attachments WHERE issue_id=?", (row["id"],))]
        with self._tx() as c:
            c.execute("UPDATE issues SET parent_id=NULL WHERE parent_id=?", (row["id"],))
            for table in ("comments", "attachments", "activity"):
                c.execute(f"UPDATE {table} SET merged_from=NULL WHERE merged_from=?", (row["id"],))
            c.execute("DELETE FROM issues WHERE id=?", (row["id"],))
        for p in paths:
            try:
                (self.data_dir / p).unlink()
            except OSError:
                pass
        return {"deleted": f"{row['prefix']}-{row['number']}"}

    # -- plans ------------------------------------------------------------------------------

    def _write_plan(self, c, row: sqlite3.Row, plan: dict, now: str) -> None:
        plan = {**plan, "updated_at": now}
        c.execute("UPDATE issues SET plan=?, updated_at=? WHERE id=?",
                  (json.dumps(plan, ensure_ascii=False), now, row["id"]))

    def set_plan(self, key: str, steps: Any, verification: Any = None, actor: str | None = None) -> dict:
        """Replace an issue's plan. `steps` is a list of text or {text, state, commit, note}; a step
        whose text matches a current step keeps that step's state, commit and note unless given.
        `verification` (the line that says what proves it works) is kept when None."""
        row = self._issue_row(key, follow=True)
        if not isinstance(steps, list):
            raise Invalid("steps must be a list of step texts or {text, state, commit, note} objects")
        if len(steps) > MAX_PLAN_STEPS:
            raise Invalid(f"a plan has at most {MAX_PLAN_STEPS} steps")
        current = parse_plan(row["plan"])
        by_text: dict[str, list[dict]] = {}
        for st in current["steps"]:
            by_text.setdefault(st["text"], []).append(st)
        new_steps = []
        for item in steps:
            text = item.get("text") if isinstance(item, dict) else item
            prev_list = by_text.get(_step_text(text, "step text", 500)) if isinstance(text, str) else None
            new_steps.append(normalize_step(item, prev_list.pop(0) if prev_list else None))
        verif = current["verification"] if verification is None else _text(verification, "verification", 2000).strip()
        plan = {"steps": new_steps, "verification": verif}
        if plan["steps"] == current["steps"] and plan["verification"] == current["verification"]:
            return self.get_issue(self._key(row))
        actor = actor or "agent"
        now = self.now()
        with self._tx() as c:
            self._write_plan(c, row, plan, now)
            detail: dict[str, Any] = {"op": "set", "steps": [st["text"] for st in new_steps],
                                      **plan_progress(plan)}
            if verif != current["verification"]:
                detail["verification"] = verif
            if current["steps"]:
                detail["replaced"] = len(current["steps"])
            self._log(c, row["id"], actor, "plan", detail, now)
        return self.get_issue(self._key(row))

    def update_step(self, key: str, index: Any, state: Any = None, commit: Any = None, note: Any = None,
                    text: Any = None, actor: str | None = None) -> dict:
        """Change one step (1-based `index`): its state, the commit that did it, a note, or its text."""
        row = self._issue_row(key, follow=True)
        plan = parse_plan(row["plan"])
        try:
            n = int(index)
        except (TypeError, ValueError):
            raise Invalid("index must be a step number (1 = the first step)") from None
        if isinstance(index, bool) or not 1 <= n <= len(plan["steps"]):
            raise Invalid(f"{self._key(row)} has {len(plan['steps'])} plan steps; index {index!r} is not one of them"
                          + ("" if plan["steps"] else " (write the plan first with set_plan)"))
        old = plan["steps"][n - 1]
        change: dict[str, Any] = {}
        for field, value in (("text", text), ("state", state), ("commit", commit), ("note", note)):
            if value is not None:
                change[field] = value
        if not change:
            raise Invalid("give at least one of state, commit, note or text")
        # An explicit empty commit or note clears it.
        merged = {**old, **{k: v for k, v in change.items() if v != ""}}
        for field in ("commit", "note"):
            if change.get(field) == "":
                merged.pop(field, None)
        new = normalize_step(merged)
        if new == old:
            return self.get_issue(self._key(row))
        plan["steps"][n - 1] = new
        actor = actor or "agent"
        now = self.now()
        with self._tx() as c:
            self._write_plan(c, row, plan, now)
            detail: dict[str, Any] = {"op": "step", "index": n, "text": new["text"], **plan_progress(plan)}
            if new["state"] != old.get("state"):
                detail["from"], detail["to"] = old.get("state"), new["state"]
            for field in ("commit", "note"):
                if new.get(field) != old.get(field):
                    detail[field] = new.get(field, "")
            if new["text"] != old["text"]:
                detail["old_text"] = old["text"]
            self._log(c, row["id"], actor, "plan", detail, now)
            auto = plan_status(row["status"], old.get("state"), new["state"], plan)
            if auto:
                self._apply_changes(c, row["id"], dict(row), {"status": auto[0]}, actor, now, reason=auto[1])
        return self.get_issue(self._key(row))

    # -- parent / child groups ---------------------------------------------------------------

    def link_parent(self, child: str, parent: str | None, actor: str | None = None) -> dict:
        """Make `child` part of `parent` (same project, no cycles); `parent=None` unlinks it."""
        crow = self._issue_row(child, follow=True)
        actor = actor or "owner"
        prow = None
        if parent not in (None, ""):
            prow = self._issue_row(parent, follow=True)
            if prow["project_id"] != crow["project_id"]:
                raise Invalid(f"{self._key(prow)} belongs to another project")
            if prow["id"] == crow["id"]:
                raise Invalid("an issue cannot be part of itself")
            up, seen = prow, set()
            while up["parent_id"] is not None and up["id"] not in seen:
                seen.add(up["id"])
                if up["parent_id"] == crow["id"]:
                    raise Invalid(f"{self._key(prow)} is already part of {self._key(crow)}; that would make a loop")
                up = self._row_by_id(up["parent_id"])
        new_id = prow["id"] if prow is not None else None
        if crow["parent_id"] == new_id:
            return self.get_issue(self._key(crow))
        old_parent = self._row_by_id(crow["parent_id"]) if crow["parent_id"] is not None else None
        now = self.now()
        with self._tx() as c:
            c.execute("UPDATE issues SET parent_id=?, updated_at=? WHERE id=?", (new_id, now, crow["id"]))
            self._log(c, crow["id"], actor, "parent",
                      {"parent": self._key(prow) if prow is not None else None,
                       "previous": self._key(old_parent) if old_parent is not None else None}, now)
            if old_parent is not None:
                self._log(c, old_parent["id"], actor, "child", {"op": "removed", "child": self._key(crow)}, now)
                c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, old_parent["id"]))
            if prow is not None:
                self._log(c, prow["id"], actor, "child", {"op": "added", "child": self._key(crow)}, now)
                c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, prow["id"]))
        return self.get_issue(self._key(crow))

    # -- merging ----------------------------------------------------------------------------

    def merge_issues(self, target: str, sources: Iterable[str], actor: str | None = None) -> dict:
        """Fold `sources` into `target`: their comments, attachments and activity move into the
        target's timeline marked with where they came from, the target gets one 'merged' entry per
        source carrying its title, body and location, children move to the target, and each source
        closes with a link to the target (its id redirects there). `unmerge` undoes it."""
        trow = self._issue_row(target, follow=True)
        if isinstance(sources, str):
            sources = [x for x in re.split(r"[\s,]+", sources) if x]
        srows, seen = [], {trow["id"]}
        for key in sources or []:
            r = self._issue_row(key)
            if r["merged_into"] is not None:
                raise Conflict(f"{self._key(r)} is already merged into {self._key(self._row_by_id(r['merged_into']))}")
            if r["project_id"] != trow["project_id"]:
                raise Invalid(f"{self._key(r)} belongs to another project")
            if r["id"] in seen:
                if r["id"] == trow["id"]:
                    raise Invalid(f"cannot merge {self._key(r)} into itself")
                continue
            seen.add(r["id"])
            srows.append(r)
        if not srows:
            raise Invalid("give at least one issue to merge into " + self._key(trow))
        # A target that is (part of) a source's children would end up its own ancestor.
        up = trow
        while up["parent_id"] is not None:
            if any(up["parent_id"] == r["id"] for r in srows):
                up = None
                break
            up = self._row_by_id(up["parent_id"])
        reparent_target = up is None
        actor = actor or "owner"
        now = self.now()
        tkey = self._key(trow)
        merged = []
        with self._tx() as c:
            if reparent_target:
                c.execute("UPDATE issues SET parent_id=NULL WHERE id=?", (trow["id"],))
            for r in srows:
                skey = self._key(r)
                for table in ("comments", "attachments", "activity"):
                    c.execute(f"UPDATE {table} SET issue_id=?, merged_from=COALESCE(merged_from, ?) WHERE issue_id=?",
                              (trow["id"], r["id"], r["id"]))
                # Issues merged into the source earlier now redirect straight to the target.
                c.execute("UPDATE issues SET merged_into=? WHERE merged_into=?", (trow["id"], r["id"]))
                children = [ch["id"] for ch in c.execute(
                    "SELECT id FROM issues WHERE parent_id=? AND id<>?", (r["id"], trow["id"])).fetchall()]
                if children:
                    c.execute(f"UPDATE issues SET parent_id=? WHERE id IN ({','.join('?' * len(children))})",
                              [trow["id"], *children])
                c.execute("UPDATE issues SET merged_into=?, status='closed', closed_at=?, updated_at=? WHERE id=?",
                          (trow["id"], now, now, r["id"]))
                self._log(c, r["id"], actor, "merged_into",
                          {"into": tkey, "prev_status": r["status"], "children": children}, now)
                self._log(c, trow["id"], actor, "merged", {
                    "from": skey, "title": r["title"], "body": r["body"], "kind": r["kind"],
                    "status": r["status"], "source": r["source"], "area": r["area"],
                    "tags": json.loads(r["tags"]), "location": json.loads(r["location"]),
                    "created_at": r["created_at"]}, now)
                merged.append(skey)
            c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, trow["id"]))
        issue = self.get_issue(tkey)
        return {"target": tkey, "merged": merged, "issue": issue}

    def unmerge(self, source: str, actor: str | None = None) -> dict:
        """Undo a merge: the source gets back its own comments, attachments, activity, children and
        status, and its id stops redirecting. What was added to the target after the merge stays."""
        srow = self._issue_row(source)
        if srow["merged_into"] is None:
            raise Invalid(f"{self._key(srow)} is not merged into anything")
        trow = self._row_by_id(srow["merged_into"])
        record = self._read("SELECT detail FROM activity WHERE issue_id=? AND action='merged_into' "
                            "ORDER BY id DESC LIMIT 1", (srow["id"],))
        detail = json.loads(record[0]["detail"]) if record else {}
        prev = detail.get("prev_status") if detail.get("prev_status") in STATUSES else "open"
        actor = actor or "owner"
        now = self.now()
        with self._tx() as c:
            for table in ("comments", "attachments", "activity"):
                c.execute(f"UPDATE {table} SET issue_id=?, merged_from=NULL WHERE issue_id=? AND merged_from=?",
                          (srow["id"], trow["id"], srow["id"]))
            children = [int(x) for x in detail.get("children") or [] if isinstance(x, int)]
            if children:
                c.execute(f"UPDATE issues SET parent_id=? WHERE parent_id=? AND id IN ({','.join('?' * len(children))})",
                          [srow["id"], trow["id"], *children])
            c.execute("UPDATE issues SET merged_into=NULL, status=?, closed_at=?, updated_at=? WHERE id=?",
                      (prev, now if prev in DONE_STATUSES else None, now, srow["id"]))
            self._log(c, srow["id"], actor, "unmerged", {"from": self._key(trow), "status": prev}, now)
            self._log(c, trow["id"], actor, "unmerged", {"source": self._key(srow)}, now)
            c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, trow["id"]))
        return {"source": self._key(srow), "target": self._key(trow),
                "issue": self.get_issue(self._key(srow), follow=False)}

    def suggest_groups(self, slug: str, limit: int = 20) -> dict:
        """Proposals (never applied) for open items that look like one thing: see groups.py."""
        from .groups import suggest
        project = self._project_row(slug)
        rows = self._read(
            f"SELECT {self.ISSUE_COLUMNS} FROM issues i JOIN projects p ON p.id=i.project_id "
            f"WHERE i.project_id=? AND i.merged_into IS NULL AND i.status NOT IN "
            f"({','.join('?' * len(DONE_STATUSES))}) ORDER BY i.number",
            [project["id"], *DONE_STATUSES])
        issues = [self._issue_dict(r) for r in rows]
        return {"project": project["slug"], "groups": suggest(issues, limit=limit)}

    # -- project handoff ----------------------------------------------------------------------

    def _handoff_dict(self, project: sqlite3.Row, row: sqlite3.Row | None, versions: int) -> dict:
        markdown = row["markdown"] if row else ""
        preamble, sections = split_handoff(markdown)
        return {
            "project": project["slug"],
            "version": row["version"] if row else 0,
            "versions": versions,
            "markdown": markdown,
            "author": row["author"] if row else None,
            "note": row["note"] if row else "",
            "created_at": row["created_at"] if row else None,
            "sections": {h: b for h, b in sections},
            "section_order": list(HANDOFF_SECTIONS),
        }

    @cached_read
    def get_handoff(self, slug: str, version: Any = None) -> dict:
        """The project's handoff: the latest version, or `version`. Version 0 = none written yet."""
        project = self._project_row(slug)
        count = self._read("SELECT COUNT(*) n FROM handoffs WHERE project_id=?", (project["id"],))[0]["n"]
        if version in (None, "", "latest"):
            rows = self._read("SELECT * FROM handoffs WHERE project_id=? ORDER BY version DESC LIMIT 1",
                              (project["id"],))
        else:
            try:
                v = int(version)
            except (TypeError, ValueError):
                raise Invalid("version must be a number") from None
            rows = self._read("SELECT * FROM handoffs WHERE project_id=? AND version=?", (project["id"], v))
            if not rows:
                raise NotFound(f"{project['slug']} has no handoff version {v}")
        return self._handoff_dict(project, rows[0] if rows else None, count)

    def set_handoff(self, slug: str, markdown: Any, author: str | None = None, note: Any = None) -> dict:
        """Save the whole handoff document as a new version (unchanged text saves nothing)."""
        project = self._project_row(slug)
        markdown = _text(markdown, "handoff", MAX_HANDOFF_CHARS).replace("\r\n", "\n").strip()
        if not markdown:
            raise Invalid("the handoff is empty; write at least one section")
        markdown += "\n"
        author = _text(author or "agent", "author", 80, required=True).strip()
        note = _text(note, "note", 300).strip()
        now = self.now()
        with self._tx() as c:
            last = c.execute("SELECT version, markdown FROM handoffs WHERE project_id=? ORDER BY version DESC LIMIT 1",
                             (project["id"],)).fetchone()
            if last is None or last["markdown"] != markdown:
                c.execute("INSERT INTO handoffs(project_id, version, markdown, author, note, created_at) "
                          "VALUES (?,?,?,?,?,?)", (project["id"], (last["version"] if last else 0) + 1,
                                                  markdown, author, note, now))
        return self.get_handoff(slug)

    def update_handoff(self, slug: str, section: str, text: Any, author: str | None = None) -> dict:
        """Replace one section (State, Where work stopped, Verified, Next step, Traps; or a new
        `## ` heading) and save the document as a new version."""
        name = handoff_section_name(section)
        body = _text(text, "section text", MAX_HANDOFF_CHARS).replace("\r\n", "\n").strip()
        current = self.get_handoff(slug)["markdown"] or handoff_template()
        preamble, sections = split_handoff(current)
        for n, (heading, _) in enumerate(sections):
            if heading.lower() == name.lower():
                sections[n] = (heading, body)
                break
        else:
            order = {h: n for n, h in enumerate(HANDOFF_SECTIONS)}
            at = len(sections)
            if name in order:
                later = [n for n, (h, _) in enumerate(sections) if order.get(h, -1) > order[name]]
                at = later[0] if later else len(sections)
            sections.insert(at, (name, body))
        return self.set_handoff(slug, join_handoff(preamble, sections), author=author,
                                note=f"updated {name}")

    def handoff_history(self, slug: str, limit: int = 100) -> dict:
        project = self._project_row(slug)
        rows = self._read("SELECT version, author, note, created_at, length(markdown) size FROM handoffs "
                          "WHERE project_id=? ORDER BY version DESC LIMIT ?", (project["id"], max(1, min(int(limit), 1000))))
        return {"project": project["slug"], "versions": [dict(r) for r in rows]}

    def handoff_diff(self, slug: str, old: Any, new: Any = None) -> dict:
        """A unified diff between two versions (`new` defaults to the latest)."""
        import difflib
        a = self.get_handoff(slug, old)
        b = self.get_handoff(slug, new)
        lines = difflib.unified_diff(a["markdown"].splitlines(), b["markdown"].splitlines(),
                                     f"version {a['version']}", f"version {b['version']}", lineterm="")
        return {"project": a["project"], "from": a["version"], "to": b["version"], "diff": "\n".join(lines)}

    # -- owner activity feed (notifications) ---------------------------------------------------

    def event_cursor(self) -> dict:
        """The newest comment and activity ids: a cursor that means 'seen everything so far'."""
        with self._reader() as conn:
            return feed_cursor(conn)

    def change_feed(self, cursor: dict, limit: int = 500) -> dict:
        with self._reader() as conn:
            return read_changes(conn, cursor, limit)

    def owner_events(self, slug: str, cursor: dict | None, limit: int = 50,
                     kinds: Iterable[str] | None = None) -> dict:
        """Owner activity in a project after `cursor` ({comment, activity} ids), newest first:
        owner comments and plan notes (kind `comments`), verdicts (`verdicts`), new reports from the
        owner or the game (`reports`) and owner status changes and merges (`status`). Only kinds the
        project's notify setting enables (or `kinds`, when given). Returns {events, cursor, more}."""
        project = self._project_row(slug)
        notify = normalize_notify(None, json.loads(project["notify"] or "{}"))
        enabled = set(kinds) if kinds is not None else {k for k, on in notify.items() if on}
        latest = self.event_cursor()
        if not cursor:
            return {"events": [], "cursor": latest, "more": 0}
        after_c, after_a = int(cursor.get("comment") or 0), int(cursor.get("activity") or 0)
        owners = ",".join("?" * len(OWNER_ACTORS))
        events = []
        if enabled & {"comments", "verdicts"}:
            for r in self._read(
                    "SELECT c.id, c.author, c.text, c.verdict, c.created_at, i.number, i.title, p.prefix "
                    "FROM comments c JOIN issues i ON i.id=c.issue_id JOIN projects p ON p.id=i.project_id "
                    f"WHERE i.project_id=? AND c.id>? AND lower(c.author) IN ({owners}) ORDER BY c.id DESC LIMIT ?",
                    [project["id"], after_c, *OWNER_ACTORS, limit + 1]):
                kind = "verdicts" if r["verdict"] else "comments"
                if kind in enabled:
                    events.append({"kind": kind, "type": "verdict" if r["verdict"] else "comment",
                                   "issue": f"{r['prefix']}-{r['number']}", "title": r["title"],
                                   "actor": r["author"], "text": r["text"], "verdict": r["verdict"],
                                   "at": r["created_at"], "seq": ("c", r["id"])})
        for r in self._read(
                "SELECT a.id, a.actor, a.action, a.detail, a.created_at, i.number, i.title, i.source, p.prefix "
                "FROM activity a JOIN issues i ON i.id=a.issue_id JOIN projects p ON p.id=i.project_id "
                "WHERE i.project_id=? AND a.id>? AND ("
                " (a.action='created' AND i.source IN ('owner','game') AND a.merged_from IS NULL)"
                f" OR (a.action IN ('status','plan','merged','parent') AND lower(a.actor) IN ({owners}))"
                ") ORDER BY a.id DESC LIMIT ?",
                [project["id"], after_a, *OWNER_ACTORS, limit + 1]):
            d = json.loads(r["detail"])
            ev = {"issue": f"{r['prefix']}-{r['number']}", "title": r["title"], "actor": r["actor"],
                  "at": r["created_at"], "seq": ("a", r["id"])}
            if r["action"] == "created":
                ev.update(kind="reports", type="report", source=r["source"])
            elif r["action"] == "status":
                if d.get("reason") == "verdict":
                    continue  # the verdict comment already says it
                ev.update(kind="status", type="status", **{"from": d.get("from"), "to": d.get("to")})
            elif r["action"] == "plan":
                if d.get("op") != "step":
                    continue
                ev.update(kind="comments", type="plan", step=d.get("index"), step_text=d.get("text"),
                          to=d.get("to"), note=d.get("note"))
            elif r["action"] == "merged":
                ev.update(kind="status", type="merged", source_issue=d.get("from"))
            else:
                ev.update(kind="status", type="parent", parent=d.get("parent"))
            if ev["kind"] in enabled:
                events.append(ev)
        events.sort(key=lambda e: (e["at"], e["seq"][1]), reverse=True)
        more = max(0, len(events) - limit)
        events = events[:limit]
        for e in events:
            e.pop("seq", None)
        return {"events": events, "cursor": latest, "more": more}

    # -- comments ---------------------------------------------------------------------------

    def add_comment(self, key: str, author: str | None, text: str | None, verdict: str | None = None,
                    attachments: list | None = None, attachment_paths: list | None = None,
                    path_base: Iterable[str | Path] | None = None, *, full: bool = True) -> dict:
        row = self._issue_row(key, follow=True)
        key = self._key(row)
        author = _text(author or "owner", "author", 80, required=True).strip()
        text = _text(text, "text", 200_000)
        if verdict in ("", None):
            verdict = None
        else:
            verdict = _req_choice(verdict, VERDICTS, "verdict")
        attachments = attachments or []
        if not isinstance(attachments, list):
            raise Invalid("attachments must be a list")
        decoded = [self._decode_attachment(a) for a in attachments]
        decoded += self._path_attachments(attachment_paths, path_base)
        if not text.strip() and not verdict and not decoded:
            raise Invalid("a comment needs text, a verdict or an attachment")
        now = self.now()
        with self._tx() as c:
            cur = c.execute("INSERT INTO comments(issue_id, author, text, verdict, created_at) VALUES (?,?,?,?,?)",
                            (row["id"], author, text, verdict, now))
            comment_id = cur.lastrowid
            for filename, mime, blob in decoded:
                self._store_attachment(c, row["id"], row["slug"], comment_id, filename, mime, blob, author, now,
                                       log=False)
            current = self._issue_dict(row)
            diff = {}
            if verdict and current["status"] != verdict:
                diff["status"] = verdict
            self._apply_changes(c, row["id"], current, diff, author, now, reason="verdict" if diff else None)
        if full:
            issue = self.get_issue(key)
            comment = next(cm for cm in issue["comments"] if cm["id"] == comment_id)
        else:
            # MCP only returns a compact issue and the new comment. Do not load/parse the
            # entire historical timeline just to throw it away on every agent progress call.
            with self._snapshot():
                issue = self.get_issue(key, full=False)
                counts = self._read("SELECT (SELECT COUNT(*) FROM comments WHERE issue_id=i.id) comments,"
                                    " (SELECT COUNT(*) FROM attachments WHERE issue_id=i.id) attachments"
                                    " FROM issues i WHERE project_id=? AND number=?",
                                    (row["project_id"], issue["number"]))[0]
                issue.update(comment_count=counts["comments"], attachment_count=counts["attachments"])
                origin = "(SELECT x.number FROM issues x WHERE x.id=t.merged_from) merged_number"
                def merged_from(r):
                    return f"{row['prefix']}-{r['merged_number']}" if r["merged_number"] is not None else None
                atts = [{**self._attachment_dict(a), "merged_from": merged_from(a)} for a in self._read(
                    f"SELECT t.*, {origin} FROM attachments t WHERE comment_id=? ORDER BY id", (comment_id,))] if decoded else []
                cm = self._read(f"SELECT t.*, {origin} FROM comments t WHERE id=?", (comment_id,))[0]
                comment = {k: cm[k] for k in ("id", "author", "text", "verdict", "created_at")}
                comment.update(attachments=atts, merged_from=merged_from(cm))
        return {"comment": comment, "issue": issue}

    # -- attachments ------------------------------------------------------------------------

    @staticmethod
    def _attachment_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "comment_id": row["comment_id"],
            "filename": row["filename"],
            "mime": row["mime"],
            "size": row["size"],
            "url": f"/api/attachments/{row['id']}",
            "is_image": row["mime"].startswith("image/") and row["mime"] != "image/svg+xml",
            "created_at": row["created_at"],
        }

    def _path_attachments(self, paths: Any, path_base: Iterable[str | Path] | None) -> list[tuple[str, str, bytes]]:
        if paths in (None, "", []):
            return []
        if path_base is None:
            raise Invalid("attachment_paths are read only by the CLI and the MCP tools; "
                          "over HTTP send attachments as {filename, mime, data_base64}")
        if not isinstance(paths, list):
            raise Invalid("attachment_paths must be a list of file paths")
        base = list(path_base)
        return [self._check_blob(*read_attachment_path(p, base)) for p in paths]

    def _decode_attachment(self, a: Any) -> tuple[str, str, bytes]:
        if not isinstance(a, dict):
            raise Invalid("each attachment must be an object {filename, mime, data_base64}")
        blob = decode_base64(a.get("data_base64") or a.get("data") or "")
        filename = _safe_filename(a.get("filename") or "attachment")
        return self._check_blob(filename, a.get("mime"), blob)

    def _check_blob(self, filename: str, mime: str | None, blob: bytes) -> tuple[str, str, bytes]:
        if not blob:
            raise Invalid(f"attachment {filename!r} is empty")
        if len(blob) > MAX_ATTACHMENT_BYTES:
            raise Invalid(f"attachment {filename!r} is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        filename = _safe_filename(filename)
        mime = sniff_mime(blob, filename, mime)
        if "." not in filename:
            ext = mimetypes.guess_extension(mime) or ""
            filename += ext
        return filename, mime, blob

    def _store_attachment(self, c, issue_id: int, slug: str, comment_id: int | None, filename: str, mime: str,
                          blob: bytes, actor: str, now: str, log: bool = True) -> int:
        month = now[:7]
        rel_dir = Path("attachments") / slug / month
        (self.data_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        ext = Path(filename).suffix.lower()
        if not re.match(r"^\.[a-z0-9]{1,8}$", ext):
            ext = ""
        rel = rel_dir / f"{uuid.uuid4().hex}{ext}"
        target = self.data_dir / rel
        target.write_bytes(blob)
        try:
            cur = c.execute(
                "INSERT INTO attachments(issue_id, comment_id, filename, mime, size, stored_path, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (issue_id, comment_id, filename, mime, len(blob), rel.as_posix(), now))
            if log:
                self._log(c, issue_id, actor, "attached", {"filename": filename, "attachment": cur.lastrowid}, now)
                c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, issue_id))
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return cur.lastrowid

    def add_attachment(self, key: str, filename: str, mime: str | None, blob: bytes, comment_id: int | None = None,
                       actor: str | None = None) -> dict:
        row = self._issue_row(key, follow=True)
        if comment_id is not None:
            try:
                comment_id = int(comment_id)
            except (TypeError, ValueError):
                raise Invalid("comment_id must be a number") from None
            if not self._read("SELECT 1 FROM comments WHERE id=? AND issue_id=?", (comment_id, row["id"])):
                raise NotFound(f"no comment {comment_id} on this issue")
        filename, mime, blob = self._check_blob(filename or "attachment", mime, blob)
        now = self.now()
        with self._tx() as c:
            att_id = self._store_attachment(c, row["id"], row["slug"], comment_id, filename, mime, blob,
                                            actor or "owner", now)
        return self.get_attachment(att_id)[0]

    def get_attachment(self, att_id: int | str) -> tuple[dict, Path]:
        try:
            att_id = int(att_id)
        except (TypeError, ValueError):
            raise NotFound("no such attachment") from None
        rows = self._read("SELECT * FROM attachments WHERE id=?", (att_id,))
        if not rows:
            raise NotFound(f"no attachment {att_id}")
        path = self.data_dir / rows[0]["stored_path"]
        return self._attachment_dict(rows[0]), path

    def delete_attachment(self, att_id: int | str, actor: str | None = None) -> dict:
        meta, path = self.get_attachment(att_id)
        rows = self._read("SELECT issue_id FROM attachments WHERE id=?", (meta["id"],))
        now = self.now()
        with self._tx() as c:
            c.execute("DELETE FROM attachments WHERE id=?", (meta["id"],))
            self._log(c, rows[0]["issue_id"], actor or "owner", "detached", {"filename": meta["filename"]}, now)
            c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, rows[0]["issue_id"]))
        try:
            path.unlink()
        except OSError:
            pass
        return {"deleted": meta["id"]}

    # -- command queue ----------------------------------------------------------------------

    def _command_dict(self, row: sqlite3.Row) -> dict:
        issue_key = None
        if row["issue_id"] is not None:
            r = self._read("SELECT p.prefix, i.number FROM issues i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
                           (row["issue_id"],))
            if r:
                issue_key = f"{r[0]['prefix']}-{r[0]['number']}"
        if row["delivered_at"]:
            state = "delivered"
        elif row["expires_at"] <= self.now():
            state = "expired"
        else:
            state = "pending"
        return {
            "id": row["id"],
            "command": row["command"],
            "issue": issue_key,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "delivered_at": row["delivered_at"],
            "client": row["client"],
            "state": state,
        }

    def queue_command(self, slug: str, command: str, issue: str | None = None, actor: str | None = None) -> dict:
        project = self._project_row(slug)
        command = _text(command, "command", 2000, required=True).strip()
        issue_id = None
        if issue:
            irow = self._issue_row(issue, follow=True)
            if irow["project_id"] != project["id"]:
                raise Invalid(f"{issue} belongs to another project")
            issue_id = irow["id"]
        now_dt = self.clock()
        now = fmt_time(now_dt)
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO commands(project_id, issue_id, command, created_by, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (project["id"], issue_id, command, actor or "owner", now, fmt_time(now_dt + COMMAND_TTL)))
            if issue_id is not None:
                self._log(c, issue_id, actor or "owner", "command_sent", {"command": command, "command_id": cur.lastrowid}, now)
        return self.get_command(cur.lastrowid)

    def get_command(self, command_id: int | str) -> dict:
        try:
            command_id = int(command_id)
        except (TypeError, ValueError):
            raise NotFound("no such command") from None
        rows = self._read("SELECT * FROM commands WHERE id=?", (command_id,))
        if not rows:
            raise NotFound(f"no command {command_id}")
        return self._command_dict(rows[0])

    def list_commands(self, slug: str, limit: int = 20) -> list[dict]:
        project = self._project_row(slug)
        return [self._command_dict(r) for r in self._read(
            "SELECT * FROM commands WHERE project_id=? ORDER BY id DESC LIMIT ?", (project["id"], max(1, min(int(limit), 200))))]

    def next_command(self, slug: str, client: str | None = None) -> dict | None:
        """Hand the oldest undelivered, unexpired command to the game exactly once."""
        project = self._project_row(slug)
        client = _text(client or "game", "client", 80).strip() or "game"
        now = self.now()
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM commands WHERE project_id=? AND delivered_at IS NULL AND expires_at>? "
                "ORDER BY id LIMIT 1", (project["id"], now)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE commands SET delivered_at=?, client=? WHERE id=? AND delivered_at IS NULL",
                      (now, client, row["id"]))
            if row["issue_id"] is not None:
                self._log(c, row["issue_id"], client, "command_delivered",
                          {"command": row["command"], "command_id": row["id"], "client": client}, now)
        return self.get_command(row["id"])

    # -- bulk -------------------------------------------------------------------------------

    def import_data(self, payload: Any, default_project: str | None = None, actor: str | None = None,
                    path_base: Iterable[str | Path] | None = None) -> dict:
        """Bulk create. Accepts a list of issues, or {project?, projects?, issues}. Issues whose
        (project, external_ref) already exists are skipped."""
        if isinstance(payload, list):
            payload = {"issues": payload}
        if not isinstance(payload, dict):
            raise Invalid("import expects a list of issues or an object with an 'issues' list")
        summary = {"projects_created": [], "created": [], "skipped": [], "errors": []}
        for p in payload.get("projects") or []:
            try:
                self.get_project(p.get("slug", ""))
            except NotFound:
                self.create_project(p.get("slug"), p.get("name"), p.get("prefix"))
                summary["projects_created"].append(p.get("slug"))
        default_project = payload.get("project") or default_project
        issues = payload.get("issues")
        if not isinstance(issues, list):
            raise Invalid("'issues' must be a list")
        for n, item in enumerate(issues):
            if not isinstance(item, dict):
                summary["errors"].append({"index": n, "error": "not an object"})
                continue
            slug = item.get("project") or default_project
            if not slug:
                summary["errors"].append({"index": n, "error": "no project given"})
                continue
            try:
                project = self._project_row(slug)
                ref = str(item.get("external_ref") or "").strip()
                if ref:
                    dup = self._read("SELECT i.number FROM issues i WHERE i.project_id=? AND i.external_ref=?",
                                     (project["id"], ref))
                    if dup:
                        summary["skipped"].append({"index": n, "external_ref": ref,
                                                   "existing": f"{project['prefix']}-{dup[0]['number']}"})
                        continue
                data = {k: v for k, v in item.items() if k not in ("comments", "id", "number")}
                issue = self.create_issue(slug, data, actor=actor, path_base=path_base)
                for cm in item.get("comments") or []:
                    if isinstance(cm, dict):
                        self.add_comment(issue["id"], cm.get("author") or actor or "import", cm.get("text"),
                                         cm.get("verdict"))
                summary["created"].append(issue["id"])
            except DeskError as e:
                summary["errors"].append({"index": n, "error": str(e)})
        return summary

    def export_project(self, slug: str) -> dict:
        project = self.get_project(slug)
        issues = []
        rows = self._read("SELECT p.prefix, i.number FROM issues i JOIN projects p ON p.id=i.project_id "
                          "WHERE p.slug=? ORDER BY i.number", (project["slug"],))
        for r in rows:
            issues.append(self.get_issue(f"{r['prefix']}-{r['number']}"))
        return {"format": "pair-desk-export", "version": 1, "exported_at": self.now(), "project": project,
                "issues": issues}
