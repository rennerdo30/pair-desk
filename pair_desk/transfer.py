"""Portable selected-topic archives. Imports append; they never restore a database.

Relationships are closed over on export. IDs are remapped on import, while the
original rows (including command history) remain in a provenance activity entry.
No machine settings, project configuration, command queue or launch grants move.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from .store import Conflict, Invalid, MAX_ATTACHMENT_BYTES, MAX_PLAN_STEPS, normalize_step, normalize_location

FORMAT = "pair-desk-topics"
MAX_ZIP = 80 * 1024 * 1024
MAX_EXPANDED = 160 * 1024 * 1024
MAX_ROWS = 20000
TABLES = {
    "issues": "id project_id number title body kind status priority area tags location source external_ref created_at updated_at closed_at plan parent_id merged_into size milestone build",
    "comments": "id issue_id author text verdict created_at merged_from",
    "activity": "id issue_id actor action detail created_at merged_from",
    "attachments": "id issue_id comment_id filename mime size stored_path created_at merged_from",
    "commands": "id project_id issue_id command created_by created_at expires_at delivered_at client",
}
ATTACHMENT_LINK = re.compile(r"/api/attachments/([0-9]+)(?![0-9])")


def _validate_activity(action, detail):
    """Check the structured values consumed by the web timeline, not only JSON syntax."""
    if not isinstance(detail, dict):
        raise Invalid("invalid activity detail")
    string_fields = {
        "created": "status build", "status": "from to build", "build": "label previous",
        "attached": "filename", "detached": "filename", "command_sent": "command",
        "command_delivered": "client", "plan": "op text from to commit note verification old_text",
        "parent": "parent previous", "child": "op child", "merged_into": "into prev_status",
        "unmerged": "source from status", "merged": "from title body kind status source area created_at",
    }
    if any(k in detail and detail[k] is not None and not isinstance(detail[k], str)
           for k in string_fields.get(action, "").split()):
        raise Invalid("invalid activity text fields")
    for k in ({"edited": ["fields"], "plan": ["steps"], "merged": ["tags"]}.get(action, [])):
        if k in detail and (not isinstance(detail[k], list) or any(not isinstance(v, str) for v in detail[k])):
            raise Invalid("invalid activity list fields")
    if action == "edited" and "owner_original" in detail:
        original = detail["owner_original"]
        if not isinstance(original, dict) or any(k in original and not isinstance(original[k], str) for k in ("title", "body")):
            raise Invalid("invalid original owner report")
    if action == "merged" and "location" in detail:
        if not isinstance(detail["location"], dict) or normalize_location(detail["location"]) != detail["location"]:
            raise Invalid("invalid merge history location")
    if action == "merged_into" and (not isinstance(detail.get("children", []), list) or any(type(x) is not int for x in detail.get("children", []))):
        raise Invalid("invalid merge history children")


def _validate_links(tables):
    available = {r["id"] for r in tables["attachments"]}
    texts = [r[k] for r in tables["issues"] for k in ("body", "plan")]
    texts += [r["text"] for r in tables["comments"]]
    for row in tables["activity"]:
        detail = json.loads(row["detail"])
        if row["action"] == "merged":
            texts.append(detail.get("body") or "")
        if row["action"] == "edited":
            texts.append((detail.get("owner_original") or {}).get("body") or "")
    if any(int(match[1]) not in available for text in texts for match in ATTACHMENT_LINK.finditer(text)):
        raise Invalid("a local attachment link is outside this dataset; include its topic or remove the link")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def export_topics(store, slug, keys):
    if not isinstance(keys, list) or not keys or len(keys) > 5000 or any(not isinstance(k, str) for k in keys):
        raise Invalid("select between 1 and 5000 topics")
    with store._snapshot():
        project = dict(store._project_row(slug))
        rows = [dict(r) for r in store._read("SELECT * FROM issues WHERE project_id=?", (project["id"],))]
        by_key = {f"{project['prefix']}-{r['number']}": r["id"] for r in rows}
        if any(k not in by_key for k in keys):
            raise Invalid("every selected topic must belong to this project")
        selected = {by_key[k] for k in keys}
        tables = {"issues": rows}
        for table in ("comments", "activity", "attachments", "commands"):
            tables[table] = [dict(r) for r in store._read(
                f"SELECT t.* FROM {table} t JOIN issues i ON i.id=t.issue_id WHERE i.project_id=?", (project["id"],))]
        edges = [(r["id"], r[k]) for r in rows for k in ("parent_id", "merged_into") if r[k] is not None]
        edges += [(r["issue_id"], r["merged_from"]) for t in ("comments", "activity", "attachments")
                  for r in tables[t] if r["merged_from"] is not None]
        while True:
            expanded = selected | {x for a, b in edges if a in selected or b in selected for x in (a, b)}
            if expanded == selected:
                break
            selected = expanded
        if len(selected) > 5000:
            raise Invalid("related topics exceed the 5000 topic archive limit")
        tables = {t: [r for r in rs if r["id" if t == "issues" else "issue_id"] in selected] for t, rs in tables.items()}
        if sum(map(len, tables.values())) > MAX_ROWS:
            raise Invalid("too many history records for one archive; export a smaller selection")
        _validate_links(tables)
        manifest = {"format": FORMAT, "version": 1, "project": {k: project[k] for k in ("slug", "name", "prefix")},
                    "selected": keys, "tables": tables, "files": {}}
        output = io.BytesIO()
        total = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for row in tables["attachments"]:
                _, path = store.get_attachment(row["id"])
                if not path.resolve().is_relative_to(store.attachments_dir.resolve()) or not path.is_file():
                    raise Invalid("an attachment is missing or outside the attachment folder")
                size = path.stat().st_size
                if size > MAX_ATTACHMENT_BYTES or total + size > MAX_EXPANDED:
                    raise Invalid("archive is too large; export a smaller selection")
                blob = path.read_bytes()
                total += len(blob)
                name = f"files/{row['id']}"
                manifest["files"][name] = {"size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
                z.writestr(name, blob)
            data = _encoded(manifest)
            if total + len(data) > MAX_EXPANDED:
                raise Invalid("archive is too large; export a smaller selection")
            z.writestr("manifest.json", data)
        if output.tell() > MAX_ZIP:
            raise Invalid("archive exceeds 80 MB; export a smaller selection")
        return output.getvalue()


def read_archive(store, blob):
    """Validate untrusted ZIP completely before any persistent writes."""
    if len(blob) > MAX_ZIP:
        raise Invalid("archive exceeds 80 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if (len(infos) > MAX_ROWS + 1 or len(set(names)) != len(names)
                    or sum(i.file_size for i in infos) > MAX_EXPANDED
                    or any(i.flag_bits & 1 or not re.fullmatch(r"manifest\.json|files/[1-9][0-9]*", i.filename) for i in infos)):
                raise Invalid("unsafe or oversized archive entries")
            manifest = json.loads(z.read("manifest.json"))
            if manifest["format"] != FORMAT or manifest["version"] != 1:
                raise Invalid("unsupported topic archive format or version")
            tables = manifest["tables"]
            if set(tables) != set(TABLES) or sum(len(r) for r in tables.values()) > MAX_ROWS:
                raise Invalid("invalid archive tables")
            ids = {}
            for table, rows in tables.items():
                if not isinstance(rows, list):
                    raise Invalid("archive tables must be lists")
                ids[table] = set()
                for row in rows:
                    if set(row) != set(TABLES[table].split()):
                        raise Invalid(f"unsupported {table} columns")
                    if type(row["id"]) is not int or row["id"] <= 0 or row["id"] in ids[table]:
                        raise Invalid("invalid or duplicate record ID")
                    ids[table].add(row["id"])
                    for k, value in row.items():
                        integer = k in ("id", "project_id", "issue_id", "number", "parent_id", "merged_into", "merged_from", "comment_id") or (table == "attachments" and k == "size")
                        nullable = k in ("parent_id", "merged_into", "merged_from", "comment_id", "closed_at", "verdict", "delivered_at", "client")
                        if value is None and nullable:
                            continue
                        if (integer and type(value) is not int) or (not integer and not isinstance(value, str)):
                            raise Invalid(f"invalid {table} field {k}")
            if not 0 < len(ids["issues"]) <= 5000:
                raise Invalid("archive must contain 1 to 5000 topics")
            lookup = {r["id"]: r for r in tables["issues"]}
            for row in tables["issues"]:
                fields = dict(row)
                for k in ("tags", "location", "plan", "build"):
                    fields[k] = json.loads(row[k])
                    if not isinstance(fields[k], list if k == "tags" else dict):
                        raise Invalid(f"invalid topic {k}")
                normalized = store._validate_issue_fields(fields, creating=True)
                if any(fields[k] != value for k, value in normalized.items()):
                    raise Invalid("topic fields must use their canonical values")
                plan = fields["plan"]
                steps = plan.get("steps", [])
                if not isinstance(steps, list) or len(steps) > MAX_PLAN_STEPS:
                    raise Invalid("invalid plan steps")
                if any(not isinstance(st, dict) or normalize_step(st) != st for st in steps):
                    raise Invalid("invalid plan step fields")
                if any(k in plan and not isinstance(plan[k], str) for k in ("verification", "updated_at")):
                    raise Invalid("invalid plan metadata")
                build = fields["build"]
                if any(k in build and not isinstance(build[k], str) for k in ("path", "label", "commit", "built_at")):
                    raise Invalid("invalid historical build fields")
                if build and (not build.get("path") or not build.get("label")):
                    raise Invalid("historical build needs a path and label")
                for k in ("parent_id", "merged_into"):
                    if row[k] is not None and row[k] not in ids["issues"]:
                        raise Invalid("archive has a missing related topic")
                for k in ("parent_id", "merged_into"):
                    seen = {row["id"]}
                    current = row[k]
                    while current is not None:
                        if current in seen:
                            raise Invalid("archive has cyclic topic relationships")
                        seen.add(current)
                        current = lookup[current][k]
            comments = {r["id"]: r for r in tables["comments"]}
            for t in ("comments", "activity", "attachments", "commands"):
                for r in tables[t]:
                    if r["issue_id"] not in ids["issues"] or (r.get("merged_from") is not None and r["merged_from"] not in ids["issues"]):
                        raise Invalid("history references a missing topic")
                    if t == "activity":
                        detail = json.loads(r["detail"])
                        _validate_activity(r["action"], detail)
                    if t == "comments" and r["verdict"] not in (None, "passed", "failed"):
                        raise Invalid("invalid historical verdict")
                    if t == "attachments" and r["comment_id"] is not None and (r["comment_id"] not in comments or comments[r["comment_id"]]["issue_id"] != r["issue_id"]):
                        raise Invalid("attachment references another topic's comment")
            _validate_links(tables)
            files = manifest["files"]
            expected = {f"files/{r['id']}" for r in tables["attachments"]}
            if set(files) != expected or set(names) != expected | {"manifest.json"}:
                raise Invalid("archive attachment inventory does not match")
            blobs = {}
            for r in tables["attachments"]:
                name = f"files/{r['id']}"
                data = z.read(name)
                if len(data) != r["size"] or len(data) != files[name]["size"] or hashlib.sha256(data).hexdigest() != files[name]["sha256"]:
                    raise Invalid("attachment checksum or size mismatch")
                store._check_blob(r["filename"], r["mime"], data)
                blobs[r["id"]] = data
            if not all(isinstance(manifest["project"][k], str) for k in ("slug", "name", "prefix")):
                raise Invalid("invalid source project")
            return manifest, blobs
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
        raise Invalid("invalid topic archive") from e


def import_topics(store, slug, blob, *, preview=True):
    manifest, blobs = read_archive(store, blob)
    project = dict(store._project_row(slug))
    digest = hashlib.sha256(blob).hexdigest()
    marker = f"topic-import:{project['id']}:{digest}"
    tables = manifest["tables"]
    result = {"source": manifest["project"], "topics": len(tables["issues"]),
              "attachments": len(blobs), "already_imported": bool(store._read("SELECT 1 FROM meta WHERE key=?", (marker,))),
              "titles": [r["title"] for r in tables["issues"]],
              "notice": "Creates new topic numbers. Existing topics stay untouched. Related topics are included. Command history is retained as data; no commands run. Project settings and handoffs are not imported."}
    if preview:
        return result
    paths = []
    try:
        with store._tx() as c:
            if c.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
                raise Conflict("this archive was already imported into this project")
            number = c.execute("SELECT next_number FROM projects WHERE id=?", (project["id"],)).fetchone()[0]
            shared = c.execute("SELECT * FROM sync_projects WHERE project_id=? AND shared=1", (project["id"],)).fetchone()
            if shared and (shared["range_start"] is None or not shared["range_start"] <= number or number + result["topics"] - 1 > shared["range_end"]):
                raise Conflict("not enough reserved topic numbers; reconnect synchronization first")
            mapping, keys = {}, {}
            for row in tables["issues"]:
                build = json.loads(row["build"])
                if build:
                    build["sync_remote"] = True
                values = {**row, "project_id": project["id"], "number": number, "parent_id": None, "merged_into": None, "build": json.dumps(build)}
                values.pop("id")
                cols = list(values)
                iid = c.execute(f"INSERT INTO issues ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", list(values.values())).lastrowid
                mapping[row["id"]] = iid
                keys[row["id"]] = f"{project['prefix']}-{number}"
                number += 1
            c.execute("UPDATE projects SET next_number=? WHERE id=?", (number, project["id"]))
            for row in tables["issues"]:
                c.execute("UPDATE issues SET parent_id=?, merged_into=? WHERE id=?", (mapping.get(row["parent_id"]), mapping.get(row["merged_into"]), mapping[row["id"]]))
            comment_map, activity_map = {}, {}
            for table in ("comments", "activity"):
                for row in tables[table]:
                    values = {**row, "issue_id": mapping[row["issue_id"]], "merged_from": mapping.get(row["merged_from"])}
                    if table == "activity" and row["action"] == "merged_into":
                        detail = json.loads(row["detail"])
                        detail["children"] = [mapping[x] for x in detail.get("children", []) if x in mapping]
                        values["detail"] = json.dumps(detail)
                    values.pop("id")
                    cols = list(values)
                    newid = c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", list(values.values())).lastrowid
                    if table == "comments":
                        comment_map[row["id"]] = newid
                    else:
                        activity_map[row["id"]] = newid
            attachment_map = {}
            for row in tables["attachments"]:
                aid = store._store_attachment(c, mapping[row["issue_id"]], slug, comment_map.get(row["comment_id"]), row["filename"], row["mime"], blobs[row["id"]], "owner", store.now(), log=False)
                path = c.execute("SELECT stored_path FROM attachments WHERE id=?", (aid,)).fetchone()[0]
                paths.append(store.data_dir / path)
                attachment_map[row["id"]] = aid
                c.execute("UPDATE attachments SET created_at=?, merged_from=? WHERE id=?", (row["created_at"], mapping.get(row["merged_from"]), aid))
            def relink(text):
                return ATTACHMENT_LINK.sub(lambda m: f"/api/attachments/{attachment_map[int(m[1])]}", text)
            for row in tables["issues"]:
                c.execute("UPDATE issues SET body=?, plan=? WHERE id=?", (relink(row["body"]), relink(row["plan"]), mapping[row["id"]]))
            for row in tables["comments"]:
                c.execute("UPDATE comments SET text=? WHERE id=?", (relink(row["text"]), comment_map[row["id"]]))
            source_keys = {f"{manifest['project']['prefix']}-{row['number']}": keys[row["id"]] for row in tables["issues"]}
            # The merged card's `from` drives its Unmerge button; other key strings
            # are historical evidence, retained verbatim alongside original rows.
            link_fields = {"merged": ("from",)}
            for row in tables["activity"]:
                detail = json.loads(c.execute("SELECT detail FROM activity WHERE id=?", (activity_map[row["id"]],)).fetchone()[0])
                for field in link_fields.get(row["action"], ()):
                    if field in detail:
                        detail[field] = source_keys.get(detail[field], detail[field])
                if row["action"] == "merged" and detail.get("body"):
                    detail["body"] = relink(detail["body"])
                if row["action"] == "edited" and (detail.get("owner_original") or {}).get("body"):
                    detail["owner_original"]["body"] = relink(detail["owner_original"]["body"])
                c.execute("UPDATE activity SET detail=? WHERE id=?", (json.dumps(detail), activity_map[row["id"]]))
            originals = {r["id"]: {t: [] for t in tables} for r in tables["issues"]}
            for t, rows in tables.items():
                for r in rows:
                    originals[r["id" if t == "issues" else "issue_id"]][t].append(r)
            for n, row in enumerate(tables["issues"]):
                detail = {"archive": digest, "source_project": manifest["project"], "original": originals[row["id"]]}
                if n == 0:
                    detail["topic_mapping"] = keys
                store._log(c, mapping[row["id"]], "owner", "imported", detail, store.now())
            c.execute("INSERT INTO meta(key,value) VALUES (?,?)", (marker, store.now()))
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return {**result, "created": list(keys.values())}
