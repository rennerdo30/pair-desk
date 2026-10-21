"""Portable, complete project snapshots for a trusted repository writer.

This is deliberately not an agent apply/resolve API. Repository writers can restore
historical owner verdicts as raw records; they cannot replay commands or operations.
Only validated complete manifests may be imported. Network/Git trust and conflict
resolution authorization belong to the coordinator, not this data layer.
"""
from __future__ import annotations

import base64
import binascii
import copy
import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import uuid
from typing import Callable

from .store import (Conflict, DONE_STATUSES, Invalid, KINDS, MAX_ATTACHMENT_BYTES,
                    MAX_HANDOFF_CHARS, MAX_PLAN_STEPS, PLAN_STATES, PREFIX_RE,
                    PRIORITIES, SEED_MAX, SEED_MIN, SIZES, SLUG_RE, SOURCES,
                    STATUSES, Store, VERDICTS, _safe_filename, normalize_location,
                    normalize_tags, sniff_mime)

FORMAT = "pair-desk-sync"
VERSION = 1
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
KINDS_OF_RECORD = ("issues", "comments", "attachments", "activity", "handoffs")
MISSING = {"$missing": True}
TOMBSTONE = {"deleted": True}
ISSUE_FIELDS = {"number", "title", "body", "kind", "status", "priority", "area", "tags", "location",
                "source", "external_ref", "created_at", "updated_at", "closed_at", "plan", "parent_id",
                "merged_into", "size", "milestone", "build"}
FIELDS = {
    "issues": ISSUE_FIELDS,
    "comments": {"issue_id", "author", "text", "verdict", "created_at", "merged_from"},
    "attachments": {"issue_id", "comment_id", "filename", "mime", "size", "created_at", "merged_from",
                    "sha256", "data_base64"},
    "activity": {"issue_id", "actor", "action", "detail", "created_at", "merged_from"},
    "handoffs": {"source_version", "markdown", "author", "note", "created_at", "parents"},
}
for _history_kind in ("comments", "attachments", "activity"):
    FIELDS[_history_kind].add("source_order")
JSON_FIELDS = {"tags", "location", "plan", "build", "detail"}
REF_FIELDS = {"issue_id": "issues", "parent_id": "issues", "merged_into": "issues",
              "merged_from": "issues", "comment_id": "comments"}
ACTIVITY_REFS = {"merged_into": ("into",), "merged": ("from",), "unmerged": ("from", "source"),
                 "parent": ("from", "to"), "child": ("child",)}


def _uuid(value):
    if not isinstance(value, str):
        raise Invalid("expected a canonical UUID")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise Invalid("expected a canonical UUID") from None
    return value


def _integer(value, low=0, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise Invalid("integer outside permitted range")


def _string(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (required and not value.strip()):
        raise Invalid("invalid text field")


def _time(value, nullable=False):
    if nullable and value is None:
        return
    _string(value, 40, True)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise Invalid("timestamp must be ISO 8601 with timezone") from None


def _plain_json(value, depth=0):
    if depth > 64:
        raise Invalid("JSON nesting too deep")
    if value is None or type(value) in (str, bool, int):
        if isinstance(value, str) and "\x00" in value:
            raise Invalid("NUL in JSON text")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise Invalid("invalid Unicode text") from None
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for v in value:
            _plain_json(v, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for v in value.values():
            _plain_json(v, depth + 1)
        return
    raise Invalid("document must contain finite JSON values only")


def _build(value, state=False):
    if not isinstance(value, dict):
        raise Invalid("build must be an object")
    if not value:
        return
    if state:
        if set(value) != {"current", "count"}:
            raise Invalid("invalid build state")
        _integer(value["count"])
        if value["current"] is not None:
            _build(value["current"])
            if value["current"]["number"] > value["count"]:
                raise Invalid("build number exceeds count")
        return
    required = {"number", "label", "path", "built_at"}
    if not required <= set(value) or set(value) - required - {"commit", "set_at", "set_by"}:
        raise Invalid("invalid build evidence")
    _integer(value["number"], 1)
    for k, limit in (("label", 80), ("path", 1000), ("commit", 80), ("set_by", 80)):
        if k in value:
            _string(value[k], limit, k in ("label", "path"))
    for k in ("built_at", "set_at"):
        if k in value:
            _time(value[k])


def _plan(plan):
    if not isinstance(plan, dict) or set(plan) - {"steps", "verification", "updated_at"}:
        raise Invalid("invalid plan")
    if not plan:
        return
    if not {"steps", "verification"} <= set(plan):
        raise Invalid("incomplete plan")
    _string(plan["verification"], 4000)
    if not isinstance(plan["steps"], list) or len(plan["steps"]) > MAX_PLAN_STEPS:
        raise Invalid("invalid plan steps")
    for step in plan["steps"]:
        if not isinstance(step, dict) or not {"text", "state"} <= set(step) or set(step) - {"text", "state", "commit", "note"}:
            raise Invalid("invalid plan step")
        _string(step["text"], 500, True)
        if step["state"] not in PLAN_STATES:
            raise Invalid("invalid plan step state")
        for k, limit in (("commit", 80), ("note", 2000)):
            if k in step:
                _string(step[k], limit)
    if "updated_at" in plan:
        _time(plan["updated_at"])


def _acyclic(records, edges, label):
    # Iterative walk: malformed long graphs must not escape as RecursionError.
    done = set()
    for start in records:
        if start in done:
            continue
        active = set()
        stack = [(start, False)]
        while stack:
            node, finish = stack.pop()
            if finish:
                active.remove(node)
                done.add(node)
            elif node in active:
                raise Invalid(f"{label} cycle")
            elif node not in done:
                active.add(node)
                stack.append((node, True))
                stack.extend((n, False) for n in edges(records[node]) if n is not None)


def validate(document: dict) -> None:
    """Reject incomplete, malformed or unsafe documents without touching a Store."""
    _plain_json(document)
    if not isinstance(document, dict) or set(document) != {"format", "version", "complete", "project", "records", "handoff_head"}:
        raise Invalid("invalid snapshot envelope")
    if document["format"] != FORMAT or type(document["version"]) is not int or document["version"] != VERSION or document["complete"] is not True:
        raise Invalid("unsupported or incomplete snapshot")
    if len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise Invalid("snapshot exceeds 128 MiB JSON limit")
    project = document["project"]
    if not isinstance(project, dict) or set(project) != {"uuid", "slug", "name", "prefix", "default_seed", "created_at", "build"}:
        raise Invalid("invalid project metadata")
    _uuid(project["uuid"])
    _string(project["slug"], 40, True)
    _string(project["prefix"], 8, True)
    if not SLUG_RE.fullmatch(project["slug"]) or not PREFIX_RE.fullmatch(project["prefix"]):
        raise Invalid("invalid project slug or prefix")
    _string(project["name"], 80, True)
    if project["default_seed"] is not None:
        _integer(project["default_seed"], SEED_MIN, SEED_MAX)
    _time(project["created_at"])
    _build(project["build"], state=True)
    records = document["records"]
    if not isinstance(records, dict) or set(records) != set(KINDS_OF_RECORD):
        raise Invalid("incomplete entity manifest")
    seen = {project["uuid"]}
    live = {}
    for kind, rows in records.items():
        if not isinstance(rows, dict):
            raise Invalid("record collection must be UUID-keyed object")
        live[kind] = {}
        for key, row in rows.items():
            _uuid(key)
            if key in seen:
                raise Invalid("UUID reused across entity types")
            seen.add(key)
            if row == TOMBSTONE and type(row.get("deleted")) is bool:
                continue
            if not isinstance(row, dict) or set(row) != FIELDS[kind]:
                raise Invalid(f"invalid {kind} record fields")
            live[kind][key] = row
    def ref(kind, key, nullable=True, historical=False):
        if key is None and nullable:
            return
        _uuid(key)
        if key not in (records[kind] if historical else live[kind]):
            raise Invalid(f"dangling {kind} UUID reference")
    numbers = set()
    for key, row in live["issues"].items():
        _integer(row["number"], 1)
        if row["number"] in numbers:
            raise Invalid("duplicate issue number")
        numbers.add(row["number"])
        for field, choices in (("kind", KINDS), ("status", STATUSES), ("priority", PRIORITIES), ("source", SOURCES), ("size", (*SIZES, ""))):
            if type(row[field]) is not str or row[field] not in choices:
                raise Invalid(f"invalid issue {field}")
        for field, limit in (("title", 300), ("body", 200000), ("area", 80), ("external_ref", 300), ("milestone", 120)):
            _string(row[field], limit, field == "title")
        if not isinstance(row["tags"], list) or any(not isinstance(t, str) for t in row["tags"]) or normalize_tags(row["tags"]) != row["tags"]:
            raise Invalid("invalid tags")
        if not isinstance(row["location"], dict) or normalize_location(row["location"]) != row["location"]:
            raise Invalid("invalid location")
        _plan(row["plan"])
        _build(row["build"])
        _time(row["created_at"])
        _time(row["updated_at"])
        _time(row["closed_at"], nullable=True)
        if (row["status"] in DONE_STATUSES) != (row["closed_at"] is not None):
            raise Invalid("closed_at/status invariant")
        for field in ("parent_id", "merged_into"):
            ref("issues", row[field])
            if row[field] == key:
                raise Invalid("self relationship")
        if row["merged_into"]:
            if row["status"] != "closed" or live["issues"][row["merged_into"]]["merged_into"] is not None:
                raise Invalid("invalid merged source/target")
        if row["parent_id"] and live["issues"][row["parent_id"]]["merged_into"] is not None:
            raise Invalid("parent is merged")
    _acyclic(live["issues"], lambda r: [r["parent_id"], r["merged_into"]], "issue relationship")
    for kind in ("comments", "attachments", "activity"):
        for row in live[kind].values():
            _integer(row["source_order"], 1)
            ref("issues", row["issue_id"], nullable=False)
            ref("issues", row["merged_from"])
            if row["merged_from"] and live["issues"][row["merged_from"]]["merged_into"] != row["issue_id"]:
                raise Invalid("invalid merged_from relationship")
            _time(row["created_at"])
            if kind == "comments":
                _string(row["author"], 80, True)
                _string(row["text"], 200000)
                if row["verdict"] is not None and row["verdict"] not in VERDICTS:
                    raise Invalid("invalid historical verdict")
            elif kind == "attachments":
                ref("comments", row["comment_id"])
                if row["comment_id"] and live["comments"][row["comment_id"]]["issue_id"] != row["issue_id"]:
                    raise Invalid("attachment comment belongs to another issue")
                _string(row["filename"], 120, True)
                if _safe_filename(row["filename"]) != row["filename"]:
                    raise Invalid("unsafe attachment filename")
                _string(row["mime"], 120, True)
                if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", row["mime"]):
                    raise Invalid("invalid MIME")
                _integer(row["size"], 1, MAX_ATTACHMENT_BYTES)
                _string(row["data_base64"], (MAX_ATTACHMENT_BYTES + 2) // 3 * 4)
                try:
                    blob = base64.b64decode(row["data_base64"], validate=True)
                except (ValueError, binascii.Error):
                    raise Invalid("invalid attachment base64") from None
                if len(blob) != row["size"] or hashlib.sha256(blob).hexdigest() != row["sha256"]:
                    raise Invalid("attachment size or digest mismatch")
                if sniff_mime(blob, row["filename"], row["mime"]) != row["mime"]:
                    raise Invalid("attachment MIME/content mismatch")
            else:
                _string(row["actor"], 80, True)
                _string(row["action"], 80, True)
                detail = row["detail"]
                if not isinstance(detail, dict):
                    raise Invalid("activity detail must be an object")
                for field in ACTIVITY_REFS.get(row["action"], ()):
                    if field in detail:
                        ref("issues", detail[field], historical=True)
                if row["action"] == "merged_into":
                    if not isinstance(detail.get("children"), list) or detail.get("prev_status") not in STATUSES:
                        raise Invalid("invalid merge history")
                    for child in detail["children"]:
                        ref("issues", child, nullable=False, historical=True)
                if row["action"] == "attached":
                    ref("attachments", detail.get("attachment"), nullable=False, historical=True)
    for key, row in live["handoffs"].items():
        _integer(row["source_version"], 1)
        _string(row["markdown"], MAX_HANDOFF_CHARS, True)
        _string(row["author"], 80, True)
        _string(row["note"], 300)
        _time(row["created_at"])
        if not isinstance(row["parents"], list) or any(not isinstance(p, str) for p in row["parents"]) or len(set(row["parents"])) != len(row["parents"]):
            raise Invalid("invalid handoff parents")
        for parent in row["parents"]:
            ref("handoffs", parent, nullable=False)
    _acyclic(live["handoffs"], lambda r: r["parents"], "handoff")
    ref("handoffs", document["handoff_head"])
    if bool(live["handoffs"]) != bool(document["handoff_head"]):
        raise Invalid("handoff history requires current head")
    if document["handoff_head"] and any(document["handoff_head"] in row["parents"] for row in live["handoffs"].values()):
        raise Invalid("current handoff is not a causal head")


def _project(c, slug):
    row = c.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    if row is None:
        from .store import NotFound
        raise NotFound(f"no project {slug}")
    return row


def _substantive(c, pid):
    return (c.execute("SELECT 1 FROM issues WHERE project_id=? LIMIT 1", (pid,)).fetchone()
            or c.execute("SELECT 1 FROM handoffs WHERE project_id=? LIMIT 1", (pid,)).fetchone()
            or json.loads(c.execute("SELECT build FROM projects WHERE id=?", (pid,)).fetchone()[0])
            or c.execute("SELECT 1 FROM sync_entities WHERE project_id=? LIMIT 1", (pid,)).fetchone())


def configure_identity(store: Store, slug: str, project_uuid: str) -> None:
    """Enable shared creation guard, preserving seeded keys and local allocation.

    An empty desk may adopt a remote UUID, including after a lazy snapshot. A
    populated desk must first snapshot to establish its local UUID, and cannot
    then join a differently identified project.
    """
    _uuid(project_uuid)
    with store._tx() as c:
        project = _project(c, slug)
        old = c.execute("SELECT * FROM sync_projects WHERE project_id=?", (project["id"],)).fetchone()
        if old is None and _substantive(c, project["id"]):
            raise Conflict("populated untracked project must snapshot to establish its local identity first")
        if old and old["uuid"] != project_uuid and _substantive(c, project["id"]):
            raise Conflict("populated local project has a different sync identity")
        if old and old["uuid"] != project_uuid:
            # An unused allocation belongs to the old identity, never the new
            # repository. Empty adoption must install its own pushed block.
            c.execute("UPDATE sync_projects SET range_start=NULL,range_end=NULL WHERE project_id=?", (project["id"],))
            c.execute("UPDATE projects SET next_number=1 WHERE id=?", (project["id"],))
        try:
            c.execute("INSERT INTO sync_projects(project_id,uuid,shared) VALUES (?,?,1) "
                      "ON CONFLICT(project_id) DO UPDATE SET uuid=excluded.uuid,shared=1", (project["id"], project_uuid))
        except sqlite3.IntegrityError:
            raise Conflict("project UUID already belongs to another local project") from None


def reserve_numbers(store: Store, slug: str, start: int, end: int) -> None:
    """Install an already-pushed device block. Does not contact or allocate a remote.

    sync_projects stores shared/range_start/range_end; projects.next_number is the
    local cursor. The coordinator atomically pushes allocation before calling this
    helper (separate transaction, never inside apply's on_commit). Reinstall is
    idempotent; replenishment cannot overlap/rewind a previous block.
    """
    _integer(start, 1)
    _integer(end, 1, 2**63 - 2)
    if end - start != 999:
        raise Invalid("shared issue number blocks must contain exactly 1000 numbers")
    with store._tx() as c:
        p = _project(c, slug)
        shared = c.execute("SELECT * FROM sync_projects WHERE project_id=? AND shared=1", (p["id"],)).fetchone()
        if not shared:
            raise Conflict("configure shared identity before installing a number block")
        if (shared["range_start"], shared["range_end"]) == (start, end):
            return
        if shared["range_end"] is not None and start <= shared["range_end"]:
            raise Conflict("number block overlaps a previous reservation")
        if start < p["next_number"] or c.execute("SELECT 1 FROM issues WHERE project_id=? AND number BETWEEN ? AND ? LIMIT 1", (p["id"], start, end)).fetchone():
            raise Conflict("number block overlaps local issue history")
        c.execute("UPDATE sync_projects SET range_start=?,range_end=? WHERE project_id=?", (start, end, p["id"]))
        c.execute("UPDATE projects SET next_number=? WHERE id=?", (start, p["id"]))


def clear_number_reservation(store: Store, slug: str) -> None:
    """Disable shared creation guard without deleting identities or reusing keys."""
    with store._tx() as c:
        p = _project(c, slug)
        maximum = c.execute("SELECT COALESCE(MAX(number),0)+1 FROM issues WHERE project_id=?", (p["id"],)).fetchone()[0]
        c.execute("UPDATE projects SET next_number=? WHERE id=?", (max(maximum, p["next_number"]), p["id"]))
        c.execute("UPDATE sync_projects SET shared=0,range_start=NULL,range_end=NULL WHERE project_id=?", (p["id"],))


def _strip_provenance(value):
    if not isinstance(value, dict):
        return value
    return {k: _strip_provenance(v) for k, v in value.items() if k != "sync_remote"}


def _remember_activity(c, issue_id, activity_id, action, detail):
    """Freeze tracked activity references before numeric row IDs can be reused.

    Called only by Store._log inside its existing transaction. Raw Store history
    retains its local numeric IDs for unmerge, while map metadata stores UUID refs.
    """
    project = c.execute("SELECT i.project_id,p.prefix FROM issues i JOIN projects p ON p.id=i.project_id "
                        "JOIN sync_projects s ON s.project_id=p.id WHERE i.id=?", (issue_id,)).fetchone()
    if project is None:
        return
    detail = copy.deepcopy(detail)
    def ref(kind, local_id):
        row = c.execute("SELECT uuid FROM sync_entities WHERE project_id=? AND kind=? AND local_id=?",
                        (project["project_id"], kind, local_id)).fetchone()
        if row is None:
            # Existing untracked seed rows are backfilled by the first snapshot.
            key = str(uuid.uuid4())
            c.execute("INSERT INTO sync_entities(uuid,project_id,kind,local_id) VALUES (?,?,?,?)",
                      (key, project["project_id"], kind, local_id))
            return key
        return row[0]
    for field in ACTIVITY_REFS.get(action, ()):
        if detail.get(field) is not None:
            number = int(detail[field].split("-", 1)[1])
            found = c.execute("SELECT id FROM issues WHERE project_id=? AND number=?", (project["project_id"], number)).fetchone()
            detail[field] = ref("issues", found[0])
    if action == "merged_into":
        detail["children"] = [ref("issues", n) for n in detail["children"]]
    if action == "attached":
        detail["attachment"] = ref("attachments", detail["attachment"])
    if action in ("command_sent", "command_delivered") and "command_id" in detail:
        detail["source_command_id"] = detail.pop("command_id")
    c.execute("UPDATE sync_entities SET metadata=? WHERE project_id=? AND kind='activity' AND local_id=?",
              (json.dumps({"detail": detail}, ensure_ascii=False), project["project_id"], activity_id))


def _capture(store, c, slug):
    p = _project(c, slug)
    pid = p["id"]
    identity = c.execute("SELECT uuid FROM sync_projects WHERE project_id=?", (pid,)).fetchone()
    if identity is None:
        project_uuid = str(uuid.uuid4())
        c.execute("INSERT INTO sync_projects(project_id,uuid) VALUES (?,?)", (pid, project_uuid))
    else:
        project_uuid = identity[0]
    raw = {}
    for kind in KINDS_OF_RECORD:
        sql = (f"SELECT * FROM {kind} WHERE project_id=? ORDER BY " + ("version" if kind == "handoffs" else "id")
               if kind in ("issues", "handoffs") else f"SELECT t.* FROM {kind} t JOIN issues i ON i.id=t.issue_id WHERE i.project_id=? ORDER BY t.id")
        raw[kind] = [dict(row) for row in c.execute(sql, (pid,))]
    maps = {k: {} for k in KINDS_OF_RECORD}
    metadata = {}
    for row in c.execute("SELECT * FROM sync_entities WHERE project_id=?", (pid,)):
        if row["local_id"] is not None:
            maps[row["kind"]][row["local_id"]] = row["uuid"]
        metadata[row["uuid"]] = json.loads(row["metadata"])
    for kind in KINDS_OF_RECORD:
        for row in raw[kind]:
            if row["id"] not in maps[kind]:
                key = str(uuid.uuid4())
                maps[kind][row["id"]] = key
                c.execute("INSERT INTO sync_entities(uuid,project_id,kind,local_id) VALUES (?,?,?,?)", (key, pid, kind, row["id"]))
                metadata[key] = {}
    records = {kind: {} for kind in KINDS_OF_RECORD}
    # Old databases may contain references to deleted historical rows. Give those
    # references explicit tombstone identities; they never become live rows.
    def historical(kind, old_id=None, number=None):
        if isinstance(old_id, str):
            _uuid(old_id)
            if old_id not in records[kind]:
                found = c.execute("SELECT kind FROM sync_entities WHERE uuid=? AND project_id=?", (old_id, pid)).fetchone()
                if not found or found[0] != kind:
                    raise Invalid("unknown historical UUID")
            return old_id
        if old_id is not None and old_id in maps[kind]:
            return maps[kind][old_id]
        if number is not None:
            for r in raw["issues"]:
                if r["number"] == number:
                    return maps["issues"][r["id"]]
            for key, data in metadata.items():
                if data.get("number") == number:
                    return key
        name = f"historical:{kind}:" + (f"key:{number}" if number is not None else f"row:{old_id}")
        key = str(uuid.uuid5(uuid.UUID(project_uuid), name))
        c.execute("INSERT OR IGNORE INTO sync_entities(uuid,project_id,kind) VALUES (?,?,?)", (key, pid, kind))
        records[kind][key] = dict(TOMBSTONE)
        return key
    def issue_key(value):
        if value is None:
            return None
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
            return historical("issues", old_id=value)
        if not isinstance(value, str) or not value.startswith(p["prefix"] + "-"):
            raise Invalid("unrecognized historical issue key")
        try:
            return historical("issues", number=int(value.split("-", 1)[1]))
        except ValueError:
            raise Invalid("invalid historical issue key") from None
    previous_handoff = None
    for kind in KINDS_OF_RECORD:
        sequence = max((v.get("source_order", 0) for v in metadata.values()), default=0)
        for row in raw[kind]:
            key = maps[kind][row["id"]]
            data = {field: row[field] for field in FIELDS[kind] if field in row}
            for field in JSON_FIELDS & data.keys():
                data[field] = json.loads(data[field])
            for field, target in REF_FIELDS.items():
                if field in data and data[field] is not None:
                    if data[field] not in maps[target]:
                        raise Invalid("dangling local relationship")
                    data[field] = maps[target][data[field]]
            if "build" in data:
                data["build"] = _strip_provenance(data["build"])
            extra = metadata[key]
            if kind in ("comments", "attachments", "activity"):
                if "source_order" not in extra:
                    sequence += 1
                    extra["source_order"] = sequence
                data["source_order"] = extra["source_order"]
            if kind == "issues":
                extra = {"number": row["number"]}
            elif kind == "attachments":
                path = (store.data_dir / row["stored_path"]).resolve()
                if not path.is_relative_to(store.attachments_dir.resolve()) or path.is_symlink():
                    raise Invalid("unsafe local attachment path")
                if path.stat().st_size > MAX_ATTACHMENT_BYTES:
                    raise Invalid("attachment exceeds limit")
                blob = path.read_bytes()
                data.update(sha256=hashlib.sha256(blob).hexdigest(), data_base64=base64.b64encode(blob).decode("ascii"))
            elif kind == "activity":
                detail = data["detail"]
                if "detail" in extra:
                    data["detail"] = extra["detail"]
                else:
                    for field in ACTIVITY_REFS.get(row["action"], ()):
                        if field in detail:
                            detail[field] = issue_key(detail[field])
                    if row["action"] == "merged_into":
                        detail["children"] = [historical("issues", old_id=n) for n in detail.get("children", [])]
                    if row["action"] == "attached":
                        detail["attachment"] = historical("attachments", old_id=detail["attachment"])
                    if row["action"] in ("command_sent", "command_delivered") and "command_id" in detail:
                        detail["source_command_id"] = detail.pop("command_id")
                    extra["detail"] = copy.deepcopy(detail)
            elif kind == "handoffs":
                if not extra:
                    extra = {"source_version": row["version"], "parents": [previous_handoff] if previous_handoff else []}
                data.update(extra)
                previous_handoff = key
            c.execute("UPDATE sync_entities SET metadata=? WHERE uuid=?", (json.dumps(extra), key))
            records[kind][key] = data
    for row in c.execute("SELECT uuid,kind,local_id FROM sync_entities WHERE project_id=?", (pid,)):
        if row["local_id"] is None:
            records[row["kind"]][row["uuid"]] = dict(TOMBSTONE)
    return {"format": FORMAT, "version": VERSION, "complete": True,
            "project": {"uuid": project_uuid, **{field: p[field] for field in ("slug", "name", "prefix", "default_seed", "created_at")},
                        "build": _strip_provenance(json.loads(p["build"]))},
            "records": records, "handoff_head": previous_handoff}


def snapshot(store: Store, slug: str) -> dict:
    """Capture raw records consistently, atomically assigning missing identities.

    Local lead, notifications, links, queued commands, number cursor/blocks and
    execution capability are excluded. No export_project/import_data path is used.
    """
    with store._tx() as c:
        document = _capture(store, c, slug)
        validate(document)
        return document


def apply_snapshot(store: Store, slug: str, document: dict, expected: dict | None = None,
                   on_commit: Callable[[sqlite3.Connection], None] | None = None) -> dict:
    """Atomically import a trusted repository writer's fully validated manifest.

    The existing project must have matching configured identity. `expected` is
    compared to a freshly captured local snapshot INSIDE the write transaction.
    `on_commit(c)` may persist the coordinator's checkpoint using this connection;
    it runs before COMMIT, must not start transactions or perform external effects,
    and any exception rolls back import and checkpoint together.

    Attachment files use locally generated names. New files are removed on any
    failure; replaced/deleted files are retained (safe orphan GC is separate).
    Reapplying an identical head changes no entity rows or history. Import never
    changes the local issue-number reservation/cursor or replays any commands.
    """
    document = copy.deepcopy(document)
    validate(document)
    if expected is not None:
        expected = copy.deepcopy(expected)
        validate(expected)
    created_files = []
    stats = {"inserted": 0, "updated": 0, "deleted": 0}
    try:
        with store._tx() as c:
            p = _project(c, slug)
            identity = c.execute("SELECT * FROM sync_projects WHERE project_id=?", (p["id"],)).fetchone()
            if not identity or not identity["shared"] or identity["uuid"] != document["project"]["uuid"]:
                raise Conflict("configure matching shared project identity before import")
            if document["project"]["slug"] != slug:
                raise Conflict("snapshot slug does not match local project")
            current = _capture(store, c, slug)
            if expected is not None and current != expected:
                raise Conflict("local project changed since sync snapshot")
            if current["records"]["issues"] and p["prefix"] != document["project"]["prefix"]:
                raise Invalid("populated project prefix is immutable")
            for key, row in document["records"]["issues"].items():
                old = current["records"]["issues"].get(key)
                if old and old != TOMBSTONE and row != TOMBSTONE and old["number"] != row["number"]:
                    raise Invalid("issue UUID number is immutable; renumbering is forbidden")
            # A complete manifest cannot silently omit a previously known UUID.
            for kind in KINDS_OF_RECORD:
                if current["records"][kind].keys() - document["records"][kind].keys():
                    raise Invalid("complete manifest omitted known UUID; explicit tombstone required")
            # A UUID may never be imported over another project's or kind's map.
            for kind, rows in document["records"].items():
                for key in rows:
                    found = c.execute("SELECT project_id,kind FROM sync_entities WHERE uuid=?", (key,)).fetchone()
                    if found and (found["project_id"] != p["id"] or found["kind"] != kind):
                        raise Invalid("UUID belongs to another local entity")
            ids = {kind: {r["uuid"]: r["local_id"] for r in c.execute(
                "SELECT uuid,local_id FROM sync_entities WHERE project_id=? AND kind=?", (p["id"], kind))} for kind in KINDS_OF_RECORD}
            # Delete dependent rows first, retaining tombstone identities in triggers.
            for kind in ("attachments", "activity", "comments", "issues", "handoffs"):
                for key, row in document["records"][kind].items():
                    if row == TOMBSTONE and ids[kind].get(key) is not None:
                        c.execute(f"DELETE FROM {kind} WHERE id=?", (ids[kind][key],))
                        ids[kind][key] = None
                        stats["deleted"] += 1
                    if row == TOMBSTONE:
                        c.execute("INSERT OR IGNORE INTO sync_entities(uuid,project_id,kind) VALUES (?,?,?)", (key, p["id"], kind))
            # Preallocate all row IDs so forward references can be translated before
            # insertion. IDs are explicit, globally above every retained local row.
            for kind in KINDS_OF_RECORD:
                maximum = c.execute(f"SELECT COALESCE(MAX(id),0) FROM {kind}").fetchone()[0]
                ordered_keys = sorted(document["records"][kind], key=lambda key: (document["records"][kind][key].get("source_order", 0), key))
                for key in ordered_keys:
                    row = document["records"][kind][key]
                    if row != TOMBSTONE and ids[kind].get(key) is None:
                        maximum += 1
                        ids[kind][key] = maximum
            rows = document["records"]
            # Handoff display versions are local; topological history order with the
            # causal current head last, retaining original version and parents in map.
            handoff_order = []
            pending = {key for key, row in rows["handoffs"].items() if row != TOMBSTONE}
            while pending:
                ready = sorted(key for key in pending if all(parent not in pending for parent in rows["handoffs"][key]["parents"]))
                for key in ready:
                    if key != document["handoff_head"]:
                        handoff_order.append(key)
                pending.difference_update(ready)
            if document["handoff_head"]:
                handoff_order.append(document["handoff_head"])
            versions = {key: n + 1 for n, key in enumerate(handoff_order)}
            # Avoid unique-version collisions when a different branch becomes head.
            if current["records"]["handoffs"] != rows["handoffs"] or current["handoff_head"] != document["handoff_head"]:
                c.execute("UPDATE handoffs SET version=-version WHERE project_id=?", (p["id"],))
            for kind in KINDS_OF_RECORD:
                order = handoff_order if kind == "handoffs" else sorted(rows[kind], key=lambda k: (rows[kind][k].get("source_order", 0), k))
                for key in order:
                    row = rows[kind][key]
                    if row == TOMBSTONE:
                        c.execute("INSERT OR IGNORE INTO sync_entities(uuid,project_id,kind) VALUES (?,?,?)", (key, p["id"], kind))
                        continue
                    old = current["records"][kind].get(key)
                    if old == row and (kind != "handoffs" or current["records"]["handoffs"] == rows["handoffs"] and current["handoff_head"] == document["handoff_head"]):
                        continue
                    data = copy.deepcopy(row)
                    if kind in ("comments", "attachments", "activity"):
                        del data["source_order"]
                    for field, target in REF_FIELDS.items():
                        if field in data and data[field] is not None:
                            data[field] = ids[target][data[field]]
                    if kind == "issues":
                        data["project_id"] = p["id"]
                    if kind in ("issues",) and data["build"]:
                        previous = c.execute("SELECT build FROM issues WHERE id=?", (ids[kind][key],)).fetchone() if old else None
                        prior = json.loads(previous[0]) if previous else {}
                        if _strip_provenance(prior) != row["build"] or prior.get("sync_remote"):
                            data["build"]["sync_remote"] = True
                    elif kind == "attachments":
                        del data["sha256"], data["data_base64"]
                        if old and old != TOMBSTONE and old["sha256"] == row["sha256"]:
                            data["stored_path"] = c.execute("SELECT stored_path FROM attachments WHERE id=?", (ids[kind][key],)).fetchone()[0]
                        else:
                            path = store.attachments_dir / (uuid.uuid4().hex + ".blob")
                            with path.open("xb") as stream:
                                created_files.append(path)
                                stream.write(base64.b64decode(row["data_base64"], validate=True))
                            data["stored_path"] = path.relative_to(store.data_dir).as_posix()
                    elif kind == "activity":
                        detail = data["detail"]
                        for field in ACTIVITY_REFS.get(row["action"], ()):
                            if field in detail and detail[field] is not None:
                                value = rows["issues"][detail[field]]
                                if value == TOMBSTONE:
                                    # Retain a portable historical reference rather than
                                    # invent a live numeric ID. Capture accepts this UUID.
                                    continue
                                detail[field] = f"{document['project']['prefix']}-{value['number']}"
                        if row["action"] == "merged_into":
                            detail["children"] = [ids["issues"].get(k) or k for k in detail["children"]]
                        if row["action"] == "attached":
                            detail["attachment"] = ids["attachments"].get(detail["attachment"]) or detail["attachment"]
                    elif kind == "handoffs":
                        del data["source_version"], data["parents"]
                        data.update(project_id=p["id"], version=versions[key])
                    for field in JSON_FIELDS & data.keys():
                        data[field] = json.dumps(data[field], ensure_ascii=False)
                    existing = old is not None and old != TOMBSTONE
                    if existing:
                        c.execute(f"UPDATE {kind} SET " + ",".join(f"{k}=?" for k in data) + " WHERE id=?", [*data.values(), ids[kind][key]])
                        stats["updated"] += 1
                    else:
                        c.execute(f"INSERT INTO {kind}(id," + ",".join(data) + ") VALUES (" + ",".join("?" for _ in range(len(data) + 1)) + ")", [ids[kind][key], *data.values()])
                        # INSERT trigger supplied a fresh local UUID; adopt the validated
                        # repository UUID in this same transaction, removing tombstone
                        # mapping first when an explicitly resolved record is restored.
                        c.execute("DELETE FROM sync_entities WHERE uuid=? AND local_id IS NULL", (key,))
                        c.execute("UPDATE sync_entities SET uuid=? WHERE project_id=? AND kind=? AND local_id=?", (key, p["id"], kind, ids[kind][key]))
                        stats["inserted"] += 1
                    if kind == "handoffs":
                        c.execute("UPDATE sync_entities SET metadata=? WHERE uuid=?", (json.dumps({"source_version": row["source_version"], "parents": row["parents"]}), key))
                    elif kind in ("comments", "attachments", "activity"):
                        metadata = {"source_order": row["source_order"]}
                        if kind == "activity":
                            metadata["detail"] = row["detail"]
                        c.execute("UPDATE sync_entities SET metadata=? WHERE uuid=?", (json.dumps(metadata, ensure_ascii=False), key))
            project = copy.deepcopy(document["project"])
            build = project["build"]
            prior = json.loads(p["build"])
            if build.get("current") and (_strip_provenance(prior) != build or prior.get("current", {}).get("sync_remote")):
                build["current"]["sync_remote"] = True
            c.execute("UPDATE projects SET name=?,prefix=?,default_seed=?,created_at=?,build=? WHERE id=?",
                      (project["name"], project["prefix"], project["default_seed"], project["created_at"], json.dumps(build, ensure_ascii=False), p["id"]))
            # Enforce complete round-trip semantics before committing, including maps.
            if _capture(store, c, slug) != document:
                raise Invalid("import could not reconstruct the complete snapshot")
            if on_commit is not None:
                on_commit(c)
        return stats
    except BaseException:
        for path in created_files:
            path.unlink(missing_ok=True)
        raise


def merge(base: dict, local: dict, remote: dict) -> tuple[dict, list[dict]]:
    """Fieldwise three-way merge; conflicts retain both inputs, never content LWW.

    Histories append by UUID. Plans, build state, location commands and relationship
    bundles are atomic. Status and closed_at conflict together at their field paths;
    the coordinator must select the same side for both. Deletion always requires an
    explicit tombstone. Conflicts contain full-document paths and JSON
    base/local/remote values; {$missing:true}
    represents absence, distinct from null. The candidate uses local on conflict
    and MUST NOT be applied until the coordinator resolves/validates all conflicts.
    """
    for doc in (base, local, remote):
        validate(doc)
    identity = base["project"]["uuid"]
    if any(doc["project"]["uuid"] != identity for doc in (local, remote)):
        raise Conflict("cannot merge different project identities")
    for kind in KINDS_OF_RECORD:
        known = base["records"][kind].keys()
        if known - local["records"][kind].keys() or known - remote["records"][kind].keys():
            raise Invalid("complete manifest omitted known UUID; deletion needs tombstone")
    conflicts = []
    def conflict(path, b, l, r, kind="field"):
        conflicts.append({"path": path, "base": copy.deepcopy(b), "local": copy.deepcopy(l), "remote": copy.deepcopy(r), "kind": kind})
        return copy.deepcopy(l)
    def combine(b, l, r, path):
        if l == r:
            return copy.deepcopy(l)
        if l == b:
            return copy.deepcopy(r)
        if r == b:
            return copy.deepcopy(l)
        if l == MISSING or r == MISSING or l == TOMBSTONE or r == TOMBSTONE:
            return conflict(path, b, l, r, "deletion-edit")
        lifecycle_conflict = False
        if len(path) == 3 and path[:2] == ["records", "issues"]:
            lifecycle = lambda row: (row.get("status"), row.get("closed_at"))
            lifecycle_conflict = (lifecycle(l) != lifecycle(b) and lifecycle(r) != lifecycle(b)
                                  and lifecycle(l) != lifecycle(r))
        atomic = path[-1:] in (["plan"], ["build"], ["location"], ["detail"], ["parents"])
        # A history record is immutable, apart from movement during merge/unmerge.
        if len(path) == 3 and path[0] == "records" and path[1] != "issues":
            atomic = True
        if isinstance(b, dict) and isinstance(l, dict) and isinstance(r, dict) and not atomic:
            result = {}
            for key in sorted(b.keys() | l.keys() | r.keys()):
                if lifecycle_conflict and key in ("status", "closed_at"):
                    # Expose both fields, even if one agrees, so resolution takes
                    # a valid pair without replacing unrelated title/body edits.
                    value = conflict([*path, key], b.get(key, MISSING), l[key], r[key], "lifecycle")
                else:
                    value = combine(b.get(key, MISSING), l.get(key, MISSING), r.get(key, MISSING), [*path, key])
                if value != MISSING:
                    result[key] = value
            return result
        if path[-1:] == ["updated_at"] and isinstance(l, str) and isinstance(r, str):
            # Derived audit time only, after semantic fields merge. It never decides
            # title/status/body/plan content; original times remain in activity rows.
            return max((l, r), key=lambda value: dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
        return conflict(path, b, l, r, "atomic" if atomic else "field")
    def related_changes(before, after, issue_uuid):
        if before["issues"].get(issue_uuid) != after["issues"].get(issue_uuid):
            return True
        for kind in ("comments", "attachments", "activity"):
            def related(rows):
                return {k: v for k, v in rows.items() if v != TOMBSTONE and issue_uuid in (v["issue_id"], v["merged_from"])}
            if related(before[kind]) != related(after[kind]):
                return True
        return False
    graph_conflict = None
    for key, row in base["records"]["issues"].items():
        if row == TOMBSTONE:
            continue
        l = local["records"]["issues"][key]
        r = remote["records"]["issues"][key]
        if (l == TOMBSTONE and r != TOMBSTONE and related_changes(base["records"], remote["records"], key)
                or r == TOMBSTONE and l != TOMBSTONE and related_changes(base["records"], local["records"], key)):
            graph_conflict = "deletion-edit"
            break
        # A merge/unmerge/reparent changes several records together. Concurrent
        # edits touching that issue cannot be silently spliced into half the move.
        relation = lambda value: tuple(value.get(f) for f in ("parent_id", "merged_into")) if value != TOMBSTONE else None
        if (relation(l) != relation(row) and related_changes(base["records"], remote["records"], key)
                or relation(r) != relation(row) and related_changes(base["records"], local["records"], key)) and l != r:
            graph_conflict = "coupled-relationships"
            break
    merged = combine(base, local, remote, [])
    if graph_conflict:
        conflicts = [c for c in conflicts if c["path"][:1] != ["records"]]
        merged["records"] = conflict(["records"], base["records"], local["records"], remote["records"], graph_conflict)
        merged["handoff_head"] = local["handoff_head"]
    # Cross-field status/closed-at and parent/merge/history edits are coupled. A
    # structurally invalid candidate is a conflict, never a partial import or repair.
    try:
        validate(merged)
    except Invalid:
        conflicts = [c for c in conflicts if c["path"][:1] != ["records"]]
        merged["records"] = conflict(["records"], base["records"], local["records"], remote["records"], "coupled-graph")
        merged["handoff_head"] = local["handoff_head"]
    return merged, conflicts
