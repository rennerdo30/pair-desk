"""Opt-in project synchronization; Git I/O never runs in a SQLite transaction.

The journal is written before publishing. A manifest acknowledgement makes an
interrupted publication recoverable without mistaking subsequent local edits for
already-published data. The OS lock serializes whole cycles across all clients.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from . import sync_data
from .store import SLUG_RE, Conflict, DeskError, Store
from .sync_git import GitBranch, PushRejected, SyncError

LOG = logging.getLogger(__name__)
BRANCH = "pair-desk-data"
INTERVAL = 30.0
RANGE_SIZE = 1000
MANIFEST = ".pair-desk/manifest.json"
STATE = ".pair-desk/state.json"
SCHEMA = """CREATE TABLE IF NOT EXISTS git_sync (
    slug TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
    remote TEXT NOT NULL, branch TEXT NOT NULL, device_uuid TEXT,
    author_name TEXT NOT NULL, author_email TEXT NOT NULL, binding TEXT,
    baseline TEXT, pending TEXT, conflicts TEXT, sequence INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'disabled', last_attempt TEXT, last_success TEXT,
    remote_head TEXT
)"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise SyncError("Invalid sync UUID.") from None
    return value


def _counter(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - RANGE_SIZE - 1:
        raise SyncError("Invalid sync counter.")
    return value


def _load(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise SyncError("Invalid sync JSON.") from None


def _max_number(doc):
    return max((r.get("number", 0) for r in doc["records"]["issues"].values()
                if not r.get("deleted")), default=0)


def _manifest(doc):
    return {"format": "pair-desk-sync-manifest", "version": 1,
            "project_uuid": doc["project"]["uuid"], "ranges": {},
            "next_number": _max_number(doc) + 1, "acks": {}}


def _validate_manifest(manifest, doc):
    if (not isinstance(manifest, dict)
            or set(manifest) != {"format", "version", "project_uuid", "ranges", "next_number", "acks"}
            or manifest["format"] != "pair-desk-sync-manifest" or type(manifest["version"]) is not int
            or manifest["version"] != 1 or _uuid(manifest["project_uuid"]) != doc["project"]["uuid"]
            or not isinstance(manifest["ranges"], dict) or not isinstance(manifest["acks"], dict)):
        raise SyncError("Invalid sync manifest or project identity.")
    next_number = _counter(manifest["next_number"], 1)
    ranges = []
    for device, blocks in manifest["ranges"].items():
        _uuid(device)
        if not isinstance(blocks, list) or not blocks:
            raise SyncError("Invalid number reservations.")
        previous = 0
        for block in blocks:
            if not isinstance(block, list) or len(block) != 2:
                raise SyncError("Invalid number reservation.")
            start, end = (_counter(n, 1) for n in block)
            if end - start != RANGE_SIZE - 1 or start <= previous or end >= next_number:
                raise SyncError("Invalid number reservation.")
            ranges.append((start, end))
            previous = end
    ranges.sort()
    if any(a[1] >= b[0] for a, b in zip(ranges, ranges[1:])) or next_number <= _max_number(doc):
        raise SyncError("Overlapping reservations or invalid number counter.")
    for device, seq in manifest["acks"].items():
        _uuid(device)
        _counter(seq)


def _decode(head, files):
    if head is None:
        if files:
            raise SyncError("Files without a remote branch.")
        return None, None
    if set(files) != {MANIFEST, STATE}:
        raise SyncError("Incomplete or unsupported sync files.")
    doc, manifest = _load(files[STATE]), _load(files[MANIFEST])
    sync_data.validate(doc)
    _validate_manifest(manifest, doc)
    return doc, manifest


def _encode(doc, manifest):
    sync_data.validate(doc)
    _validate_manifest(manifest, doc)
    return {STATE: _json(doc).encode("utf-8"), MANIFEST: _json(manifest).encode("utf-8")}


@contextmanager
def _file_lock(data_dir, name):
    """Nonblocking advisory OS lock. No PID checks and no stale lock deletion."""
    directory = Path(data_dir) / "sync"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / name, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def cycle_lock(data_dir):
    return _file_lock(data_dir, "cycle.lock")


def _has_sync(data_dir, enabled_only=True):
    """Cheap read-only probe; unconfigured desks do no synchronization work."""
    db = Path(data_dir) / "desk.sqlite"
    if not db.is_file():
        return False
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            return bool(conn.execute("SELECT 1 FROM git_sync"
                                     + (" WHERE enabled=1" if enabled_only else "") + " LIMIT 1").fetchone())
    except sqlite3.OperationalError:
        return False


def has_enabled(data_dir):
    return _has_sync(data_dir)


def prepare_store(store):
    """Check copied-database bindings before exposing any application writer."""
    if _has_sync(store.data_dir, enabled_only=False):
        SyncManager(store).status()


class SyncManager:
    def __init__(self, store: Store, stop: threading.Event | None = None):
        self.store = store
        self.stop = stop or threading.Event()
        with store._tx() as c:
            c.execute(SCHEMA)
            if "remote_head" not in {r[1] for r in c.execute("PRAGMA table_info(git_sync)")}:
                c.execute("ALTER TABLE git_sync ADD COLUMN remote_head TEXT")

    def _row(self, slug):
        rows = self.store._read("SELECT * FROM git_sync WHERE slug=?", (slug,))
        if not rows:
            raise SyncError("Project synchronization has not been configured.")
        return dict(rows[0])

    def _binding(self):
        directory = self.store.data_dir / "sync"
        directory.mkdir(parents=True, exist_ok=True)
        key = directory / "device-key"
        try:
            with key.open("x", encoding="ascii") as f:
                f.write(str(uuid.uuid4()))
        except FileExistsError:
            pass
        value = _uuid(key.read_text(encoding="ascii").strip())
        # The path catches entire directory copies, the key catches DB-only copies
        # and restores to the same path. Including the local host name also
        # detects a whole-directory copy to the same path on another computer.
        # Only this digest is stored locally; none of these inputs is shared.
        return hashlib.sha256((str(self.store.data_dir.resolve()) + "\n" + socket.gethostname()
                               + "\n" + value).encode()).hexdigest()

    def _check_binding(self, row):
        try:
            binding = self._binding()
        except (SyncError, OSError, ValueError):
            binding = None
        if binding is not None and row["binding"] == binding and row["device_uuid"]:
            return True
        with self.store._tx() as c:
            c.execute("UPDATE git_sync SET enabled=0,device_uuid=NULL,binding=NULL,status='rebind-required' WHERE slug=?",
                      (row["slug"],))
            c.execute("UPDATE sync_projects SET shared=1,range_start=NULL,range_end=NULL "
                      "WHERE project_id=(SELECT id FROM projects WHERE slug=?)", (row["slug"],))
        return False

    def _transport(self, row):
        if not isinstance(row["slug"], str) or not SLUG_RE.fullmatch(row["slug"]):
            raise SyncError("Invalid sync project slug.")
        if self.stop.is_set():
            raise SyncError("Synchronization stopped.")
        return GitBranch(self.store.data_dir / "sync" / row["slug"], row["remote"], row["branch"],
                         author_name=row["author_name"], author_email=row["author_email"], timeout=20,
                         cancel_event=self.stop)

    def _update(self, slug, **fields):
        with self.store._tx() as c:
            c.execute("UPDATE git_sync SET " + ",".join(k + "=?" for k in fields) + " WHERE slug=?",
                      (*fields.values(), slug))

    def status(self, slug=None):
        rows = self.store._read("SELECT * FROM git_sync" + (" WHERE slug=?" if slug else "") + " ORDER BY slug",
                                (slug,) if slug else ())
        out = []
        for raw in rows:
            row = dict(raw)
            if (row["enabled"] or row["device_uuid"] or row["binding"]) and not self._check_binding(row):
                row = self._row(row["slug"])
            out.append({k: row[k] for k in ("slug", "enabled", "branch", "device_uuid", "status",
                                            "last_attempt", "last_success", "remote_head")}
                       | {"pending": bool(row["pending"]),
                          "conflicts": _load(row["conflicts"])["items"] if row["conflicts"] else []})
        return out

    def enable(self, slug, remote, author_name, author_email, repo=None):
        """Explicit opt-in. Only this API can configure sharing; no MCP wrapper."""
        if not isinstance(slug, str) or not slug:
            raise SyncError("A project is required.")
        # Transport validates public identity/credential-free address before use.
        config = {"slug": slug, "remote": remote, "branch": BRANCH,
                  "author_name": author_name, "author_email": author_email}
        transport = self._transport(config)
        with cycle_lock(self.store.data_dir) as acquired:
            if not acquired:
                raise SyncError("Another synchronization is running; try again.")
            head, files = transport.read()
            remote_doc, manifest = _decode(head, files)
            if remote_doc and remote_doc["project"]["slug"] != slug:
                raise SyncError("Remote project slug does not match.")
            old_rows = self.store._read("SELECT * FROM git_sync WHERE slug=?", (slug,))
            old = dict(old_rows[0]) if old_rows else None
            if old and (old["remote"] != remote or old["branch"] != BRANCH):
                raise SyncError("Changing a configured data remote requires a separate desk.")
            exists = bool(self.store._read("SELECT 1 FROM projects WHERE slug=?", (slug,)))
            if not exists:
                if remote_doc is None:
                    raise SyncError("Create the selected local project before bootstrapping an empty remote.")
                project = remote_doc["project"]
                self.store.create_project(slug, project["name"], project["prefix"])
                sync_data.configure_identity(self.store, slug, project["uuid"])
            current = sync_data.snapshot(self.store, slug)
            if remote_doc and current["project"]["uuid"] != remote_doc["project"]["uuid"]:
                if any(current["records"][k] for k in current["records"]) or current["handoff_head"]:
                    raise SyncError("A populated project cannot join a different sync identity.")
                sync_data.configure_identity(self.store, slug, remote_doc["project"]["uuid"])
                current = sync_data.snapshot(self.store, slug)
            sync_data.configure_identity(self.store, slug, current["project"]["uuid"])
            binding = self._binding()
            rebound = old and (old["binding"] != binding or not old["device_uuid"])
            device = old["device_uuid"] if old and not rebound else str(uuid.uuid4())
            if rebound:
                # Clear a copied reservation before any shared create is possible.
                with self.store._tx() as c:
                    c.execute("UPDATE sync_projects SET range_start=NULL,range_end=NULL,shared=1 "
                              "WHERE project_id=(SELECT id FROM projects WHERE slug=?)", (slug,))
            with self.store._tx() as c:
                c.execute("INSERT INTO git_sync(slug,remote,branch,author_name,author_email) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(slug) DO NOTHING", (slug, remote, BRANCH, author_name, author_email))
                c.execute("UPDATE git_sync SET enabled=1,device_uuid=?,binding=?,author_name=?,author_email=?,"
                          "baseline=COALESCE(baseline,?),status='pending' WHERE slug=?",
                          (device, binding, author_name, author_email, _json(current), slug))
            # Same lock covers validation, configuration, reservation publication and
            # installation. Workers cannot run against half-enabled configuration.
            return self._run_locked(slug, first_read=(head, files))

    def _allocation(self, slug, manifest, device, candidate):
        rows = self.store._read("SELECT p.next_number,s.range_start,s.range_end FROM projects p "
                                "JOIN sync_projects s ON s.project_id=p.id WHERE p.slug=?", (slug,))
        row = rows[0]
        blocks = manifest["ranges"].get(device, [])
        if row["range_start"] is not None and [row["range_start"], row["range_end"]] not in blocks:
            raise SyncError("Remote lost this device's installed reservation.")
        if row["range_end"] is not None and row["next_number"] <= row["range_end"]:
            return [row["range_start"], row["range_end"]], False
        # A publication may have succeeded before the reservation was installed.
        if blocks and row["range_start"] is None and row["next_number"] <= blocks[-1][1]:
            return blocks[-1], False
        start = max(manifest["next_number"], _max_number(candidate) + 1, row["next_number"])
        end = start + RANGE_SIZE - 1
        _counter(end, 1)
        manifest["ranges"].setdefault(device, []).append([start, end])
        manifest["next_number"] = end + 1
        return [start, end], True

    def _checkpoint(self, slug, baseline, head, block, status="synced"):
        def commit(c):
            if block:
                row = c.execute("SELECT p.id,p.next_number,s.range_start,s.range_end FROM projects p "
                                "JOIN sync_projects s ON s.project_id=p.id WHERE p.slug=?", (slug,)).fetchone()
                if row["range_start"] != block[0] or row["range_end"] != block[1]:
                    if row["range_end"] is not None and block[0] <= row["range_end"]:
                        raise SyncError("Cannot rewind a number reservation.")
                    c.execute("UPDATE projects SET next_number=MAX(next_number,?) WHERE id=?", (block[0], row["id"]))
                    c.execute("UPDATE sync_projects SET shared=1,range_start=?,range_end=? WHERE project_id=?",
                              (*block, row["id"]))
            c.execute("UPDATE git_sync SET baseline=?,pending=NULL,conflicts=NULL,status=?,remote_head=?,"
                      "last_success=CASE WHEN ?='synced' THEN ? ELSE last_success END WHERE slug=?",
                      (_json(baseline), status, head, status, self.store.now(), slug))
        return commit

    def _conflicted(self, slug, items, candidate, captured, remote, head):
        self._update(slug, status="conflict", conflicts=_json({"items": items, "candidate": candidate,
                     "captured": captured, "remote": remote, "remote_head": head}))
        return self.status(slug)[0]

    def _run_locked(self, slug, first_read=None):
        row = self._row(slug)
        if not row["enabled"] or not self._check_binding(row):
            return self.status(slug)[0]
        self._update(slug, last_attempt=self.store.now(), status="syncing")
        try:
            if row["conflicts"]:
                stored = _load(row["conflicts"])
                current = sync_data.snapshot(self.store, slug)
                if (current == stored["captured"]
                        and self._transport(row).head() == stored["remote_head"]):
                    self._update(slug, status="conflict")
                    return self.status(slug)[0]
                # A repair on either desk makes the old choices stale. Recompute
                # from the original baseline, preserving all branch histories.
                self._update(slug, conflicts=None, status="pending")
            for attempt in range(3):
                row = self._row(slug)
                transport = self._transport(row)
                current = sync_data.snapshot(self.store, slug)
                baseline = _load(row["baseline"]) if row["baseline"] else current
                pending = _load(row["pending"]) if row["pending"] else None
                if first_read is None and not pending and current == baseline and row["remote_head"]:
                    reservation = self.store._read("SELECT p.next_number,s.range_end FROM projects p "
                                                   "JOIN sync_projects s ON s.project_id=p.id WHERE p.slug=?", (slug,))[0]
                    if (reservation["range_end"] is not None
                            and reservation["next_number"] <= reservation["range_end"]
                            and transport.head() == row["remote_head"]):
                        # Recheck local state atomically, even on the no-fetch path.
                        sync_data.apply_snapshot(self.store, slug, current, expected=current,
                            on_commit=self._checkpoint(slug, baseline, row["remote_head"], None))
                        return self.status(slug)[0]
                head, files = first_read if first_read is not None else transport.read()
                first_read = None
                remote, manifest = _decode(head, files)
                if remote is None:
                    if row["remote_head"]:
                        raise SyncError("Remote data branch was removed; explicit recovery is required.")
                    remote, manifest = baseline, _manifest(baseline)
                if remote["project"]["uuid"] != current["project"]["uuid"]:
                    raise SyncError("Remote project identity changed.")
                if remote["project"]["slug"] != slug:
                    raise SyncError("Remote project slug changed.")
                if pending and manifest["acks"].get(pending["device_uuid"], 0) >= pending["sequence"]:
                    candidate, conflicts = sync_data.merge(pending["captured"], current, remote)
                    if conflicts:
                        return self._conflicted(slug, conflicts, candidate, current, remote, head)
                    # Preserve edits made since the captured publication, checkpoint
                    # the fetched baseline and retire the acknowledged journal together.
                    sync_data.apply_snapshot(self.store, slug, candidate, expected=current,
                        on_commit=self._checkpoint(slug, remote, head, None, "pending"))
                    current, baseline = sync_data.snapshot(self.store, slug), remote
                candidate, conflicts = sync_data.merge(baseline, current, remote)
                if conflicts:
                    return self._conflicted(slug, conflicts, candidate, current, remote, head)
                block, allocated = self._allocation(slug, manifest, row["device_uuid"], candidate)
                if candidate == remote and not allocated and head is not None:
                    sync_data.apply_snapshot(self.store, slug, candidate, expected=current,
                        on_commit=self._checkpoint(slug, candidate, head, block))
                    return self.status(slug)[0]
                sequence = max(row["sequence"], manifest["acks"].get(row["device_uuid"], 0)) + 1
                _counter(sequence, 1)
                manifest["acks"][row["device_uuid"]] = sequence
                journal = {"baseline": baseline, "captured": current, "intended": candidate,
                           "sequence": sequence, "device_uuid": row["device_uuid"]}
                outbound = _encode(candidate, manifest)
                self._update(slug, sequence=sequence, pending=_json(journal), status="pending")
                if self.stop.is_set():
                    raise SyncError("Synchronization stopped.")
                try:
                    published = transport.publish(outbound, head)
                except PushRejected:
                    if attempt == 2:
                        raise SyncError("Remote kept changing; synchronization will retry later.") from None
                    continue
                # Expected is the pre-I/O snapshot, not a fresh post-push snapshot.
                # If it changed, the durable journal survives for ack recovery.
                sync_data.apply_snapshot(self.store, slug, candidate, expected=current,
                    on_commit=self._checkpoint(slug, candidate, published, block))
                return self.status(slug)[0]
        except Conflict:
            self._update(slug, status="local-changed")
            return self.status(slug)[0]
        except (SyncError, DeskError, OSError, ValueError, sqlite3.Error):
            # Never persist subprocess diagnostics or remote addresses/credentials.
            self._update(slug, status="error")
            raise SyncError("Synchronization failed; local changes remain pending. Check connectivity and sync status.") from None

    def run(self, slug):
        with cycle_lock(self.store.data_dir) as acquired:
            if not acquired:
                return {"slug": slug, "status": "busy"}
            return self._run_locked(slug)

    def disable(self, slug):
        with cycle_lock(self.store.data_dir) as acquired:
            if not acquired:
                raise SyncError("Another synchronization is running; try again.")
            if not self._check_binding(self._row(slug)):
                return self.status(slug)[0]
            # Disabling stops network work, not ownership of the shared namespace.
            # Preserve the exact cursor/reservation and its exhaustion guard;
            # imported peer numbers must never become this device's cursor.
            with self.store._tx() as c:
                c.execute("UPDATE git_sync SET enabled=0,status='disabled' WHERE slug=?", (slug,))
            return self.status(slug)[0]

    def resolve(self, slug, take):
        if take not in ("local", "remote"):
            raise SyncError("Choose --take local or --take remote.")
        with cycle_lock(self.store.data_dir) as acquired:
            if not acquired:
                raise SyncError("Another synchronization is running; try again.")
            row = self._row(slug)
            if not row["enabled"] or not self._check_binding(row):
                raise SyncError("Enable synchronization before resolving conflicts.")
            if not row["conflicts"]:
                raise SyncError("There are no stored conflicts.")
            state = _load(row["conflicts"])
            current = sync_data.snapshot(self.store, slug)
            if current != state["captured"]:
                self._update(slug, conflicts=None, status="local-changed")
                raise SyncError("Local data changed; run synchronization to refresh the conflicts.")
            candidate = copy.deepcopy(state["candidate"])
            if any(item["path"] == ["records"] for item in state["items"]):
                raise SyncError("Structural conflict requires repair through ordinary issue/relationship operations, "
                                "then run sync to refresh. Generic --take cannot replace a record graph.")
            for conflict in state["items"]:
                path, value = conflict["path"], conflict[take]
                target = candidate
                for key in path[:-1]:
                    target = target.setdefault(key, {})
                if value == {"$missing": True}:
                    target.pop(path[-1], None)
                else:
                    target[path[-1]] = copy.deepcopy(value)
            # Resolution is an agent-facing operation. It cannot promote any
            # passed state or verdict, including a whole-record conflict choice.
            for kind, field in (("issues", "status"), ("comments", "verdict")):
                for key, record in candidate["records"][kind].items():
                    value = record.get(field)
                    protected = value == "passed" if field == "status" else bool(value)
                    historical = current["records"][kind].get(key, {}).get(field)
                    protected_path = ["records", kind, key, field]
                    selected = any(protected_path[:len(item["path"])] == item["path"]
                                   or item["path"][:len(protected_path)] == protected_path
                                   for item in state["items"])
                    if protected and value != historical and selected:
                        raise SyncError("Conflict resolution cannot manufacture passed status or a verdict.")
            sync_data.validate(candidate)
            sync_data.apply_snapshot(self.store, slug, candidate, expected=current,
                on_commit=self._checkpoint(slug, state["remote"], state["remote_head"], None, "pending"))
            return self.status(slug)[0]


class SyncWorker:
    """One bounded cycle per interval, with a Store owned by the worker thread."""
    def __init__(self, data_dir, interval=INTERVAL):
        self.data_dir = Path(data_dir)
        self.interval = interval
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="pair-desk-sync", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                run_enabled(self.data_dir, self._stop)
            except (DeskError, SyncError, OSError, sqlite3.Error):
                LOG.info("Project sync will retry on the next cycle.")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=25)
        if self.thread.is_alive():
            LOG.warning("Sync worker is finishing its bounded Git operation.")


def run_enabled(data_dir, stop=None):
    if not has_enabled(data_dir):
        return False
    retry = False
    with Store(data_dir) as store:
        manager = SyncManager(store, stop)
        for row in manager.status():
            if stop is not None and stop.is_set():
                break
            if row["enabled"]:
                try:
                    result = manager.run(row["slug"])
                    retry |= result["status"] in ("busy", "local-changed", "pending")
                except (SyncError, DeskError, OSError, sqlite3.Error):
                    LOG.info("Project sync will retry on the next cycle.")
    return retry


def start_worker(data_dir):
    return SyncWorker(data_dir)


def schedule_once(data_dir):
    """CLI calls return promptly; a hidden, time-limited child attempts one cycle.

    A separate scheduling lock protects the persistent throttle. A CLI write can
    queue a child even after a worker's checkpoint but before its cycle unlocks.
    The child coalesces writes for INTERVAL before acquiring the cycle lock, so
    the throttle expires before its checkpoint and a later write can queue its
    successor. Concurrent CLI callers still launch at most one per interval.
    """
    if not has_enabled(data_dir):
        return None
    directory = Path(data_dir) / "sync"
    with _file_lock(data_dir, "schedule.lock") as acquired:
        if not acquired:
            return None
        throttle = directory / "scheduled-at"
        try:
            age = time.time() - float(throttle.read_text(encoding="ascii"))
            if 0 <= age < INTERVAL:
                return None
        except (OSError, ValueError):
            pass
        throttle.write_text(str(time.time()), encoding="ascii")
        args = [sys.executable, "-m", "pair_desk.sync", "--once", str(Path(data_dir).resolve())]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            proc = subprocess.Popen(args, cwd=Path(__file__).resolve().parent.parent,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    **options)
        except OSError:
            throttle.unlink(missing_ok=True)
            raise
        with (directory / "children.jsonl").open("a", encoding="ascii") as log:
            log.write(_json({"pid": proc.pid, "started": time.time(), "timeout": 150}) + "\n")
        # Keep Popen alive until completion so short-lived hosts do not emit
        # ResourceWarning; the child's own deadline bounds its entire lifetime.
        threading.Thread(target=proc.wait, kwargs={"timeout": 155}, daemon=True).start()
        return proc.pid


def _once(data_dir):
    # A cooperative deadline leaves transport's exact-PID cleanup intact. Its
    # shorter per-command bounds finish any operation already in flight.
    stop = threading.Event()
    deadline = threading.Timer(120, stop.set)
    deadline.daemon = True
    deadline.start()
    try:
        # Coalesce a burst of CLI writes before the single background attempt.
        # Throttling without this delayed attempt could strand the final write
        # of a burst when no persistent server or MCP session is running.
        if not stop.wait(INTERVAL):
            for _ in range(3):
                if not run_enabled(data_dir, stop) or stop.wait(0.1):
                    break
    finally:
        deadline.cancel()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--once":
        raise SystemExit("usage: python -m pair_desk.sync --once DATA_DIR")
    _once(sys.argv[2])
