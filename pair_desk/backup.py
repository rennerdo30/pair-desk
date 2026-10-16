"""Whole-desk backup and restore (CLI `backup` and `restore`).

A backup is one zip:
- `desk.sqlite`: a consistent snapshot taken with SQLite's online backup, so the desk keeps running.
  The search index is left out (it holds copies only) and is rebuilt when the restored desk opens.
- `attachments/...`: every file the snapshot's attachment rows point at.
- `projects.json`: the repo-folder links, when there are any.
- `manifest.json`: format, app and schema version, counts, and the size and SHA-256 of every file.

Restore checks all of that before it touches anything, and never overwrites: it unpacks next to
the data folder, checks the database, then moves the current folder aside to
`<data>.before-restore-<time>` (only with `replace`) and puts the restored one in its place.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from . import VERSION
from .context import LINKS_FILE, load_links
from .store import SCHEMA_VERSION, SEARCH_TRIGGER_NAMES, DeskError

FORMAT = "pair-desk-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DATABASE = "desk.sqlite"
BACKUPS_DIR = "backups"
_CHUNK = 1024 * 1024
# Already compressed: stored as is, since deflating them again only costs time.
_STORED = frozenset(".png .jpg .jpeg .gif .webp .avif .mp4 .webm .mov .zip .gz .7z .pdf".split())


class BackupError(DeskError):
    pass


def _stamp(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_member(name: str) -> bool:
    """A zip entry this format writes, whose path stays inside the folder it is unpacked into."""
    if name in (MANIFEST, DATABASE, LINKS_FILE):
        return True
    p = PurePosixPath(name)
    return ("\\" not in name and ":" not in name and not p.is_absolute() and ".." not in p.parts
            and len(p.parts) > 1 and p.parts[0] == "attachments")


def _snapshot(db: Path, out: Path) -> dict:
    """Copy the live database to `out` (consistent, while writers continue), drop the search index from
    the copy and compact it. Returns the schema version and counts."""
    if not db.is_file():
        raise BackupError(f"no desk database at {db}")
    src = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        src.close()
    try:
        dst.execute("PRAGMA journal_mode=DELETE")
        for name in SEARCH_TRIGGER_NAMES:
            dst.execute(f"DROP TRIGGER IF EXISTS {name}")
        try:
            dst.execute("DROP TABLE IF EXISTS issue_search")
        except sqlite3.OperationalError:
            # No FTS5 here: the table stays; the restored desk's first open rebuilds it.
            pass
        dst.execute("DELETE FROM meta WHERE key='search_index'")
        dst.commit()
        dst.execute("VACUUM")
        schema = (dst.execute("SELECT value FROM meta WHERE key='schema'").fetchone() or [None])[0]
        counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("projects", "issues", "comments", "attachments", "handoffs")}
        projects = [{"slug": r[0], "name": r[1], "issues": r[2]} for r in dst.execute(
            "SELECT p.slug, p.name, (SELECT COUNT(*) FROM issues i WHERE i.project_id=p.id) FROM projects p ORDER BY p.slug")]
        attachments = [r[0] for r in dst.execute("SELECT DISTINCT stored_path FROM attachments ORDER BY stored_path")]
    finally:
        dst.close()
    return {"schema": schema, "counts": counts, "projects": projects, "attachments": attachments}


def default_backup_path(data_dir: Path, now: _dt.datetime | None = None) -> Path:
    return Path(data_dir) / BACKUPS_DIR / f"pair-desk-{_stamp(now)}.zip"


def create_backup(data_dir: Path, out: Path | None = None, now: _dt.datetime | None = None) -> dict:
    """Write a backup zip of the desk in `data_dir` to `out` (default `<data>/backups/pair-desk-<time>.zip`).
    Returns the manifest plus `path` and `missing` (attachment rows whose file is gone)."""
    data_dir = Path(data_dir)
    out = Path(out) if out else default_backup_path(data_dir, now)
    if out.exists():
        raise BackupError(f"{out} already exists; choose another --out")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pair-desk-backup-") as tmp:
        db_copy = Path(tmp) / DATABASE
        snap = _snapshot(data_dir / DATABASE, db_copy)
        files = [(DATABASE, db_copy)]
        missing = []
        for rel in snap["attachments"]:
            p = PurePosixPath(rel)
            if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "attachments":
                missing.append(rel)
                continue
            src = data_dir / Path(*p.parts)
            if src.is_file():
                files.append((p.as_posix(), src))
            else:
                missing.append(rel)
        links = data_dir / LINKS_FILE
        if links.is_file():
            files.append((LINKS_FILE, links))
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app_version": VERSION,
            "schema": snap["schema"],
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": str(data_dir),
            "counts": snap["counts"],
            "projects": snap["projects"],
            "missing_attachments": missing,
            "files": [{"path": name, "size": src.stat().st_size, "sha256": _sha256(src)} for name, src in files],
        }
        part = out.with_name(out.name + ".part")
        try:
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                for name, src in files:
                    z.write(src, name, compress_type=zipfile.ZIP_STORED
                            if PurePosixPath(name).suffix.lower() in _STORED else zipfile.ZIP_DEFLATED)
            os.replace(part, out)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    return {**manifest, "path": str(out), "size": out.stat().st_size, "missing": missing,
            "attachment_files": sum(1 for f in manifest["files"] if f["path"].startswith("attachments/"))}


def read_manifest(zip_path: Path) -> dict:
    """The backup's manifest, after checking it is a Pair Desk backup this version can restore."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise BackupError(f"{zip_path} is not a Pair Desk backup ({e})") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError(f"{zip_path} is not a Pair Desk backup (no {FORMAT} manifest)")
    if int(manifest.get("format_version") or 0) > FORMAT_VERSION:
        raise BackupError(f"{zip_path} was made by a newer Pair Desk ({manifest.get('app_version')}); update first")
    try:
        newer = int(manifest.get("schema") or 0) > int(SCHEMA_VERSION)
    except ValueError:
        newer = True
    if newer:
        raise BackupError(f"{zip_path} has database schema {manifest.get('schema')}, newer than this Pair Desk "
                          f"({VERSION}, schema {SCHEMA_VERSION}); update first")
    return manifest


def _server_running(data_dir: Path) -> int | None:
    """The pid of a desk started with `serve --detach` on this folder, if it is still running."""
    try:
        pid = int((data_dir / "server.pid").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        code = ctypes.c_ulong()
        alive = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259
        ctypes.windll.kernel32.CloseHandle(handle)
        return pid if alive else None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _has_data(data_dir: Path) -> bool:
    return data_dir.is_dir() and any(data_dir.iterdir())


def restore_backup(zip_path: Path, data_dir: Path, replace: bool = False, now: _dt.datetime | None = None) -> dict:
    """Restore a backup into `data_dir`. A folder that already has data needs `replace`: it is moved aside
    to `<data>.before-restore-<time>`, never deleted. Returns the manifest plus `data`, `previous` (where
    the old folder went, or None) and `missing_links` (linked repo folders that do not exist here)."""
    zip_path, data_dir = Path(zip_path), Path(data_dir)
    manifest = read_manifest(zip_path)
    if _has_data(data_dir):
        if not replace:
            raise BackupError(f"{data_dir} already has a desk. Restore into an empty folder (--data), or pass "
                              f"--replace to move the current one aside first")
        pid = _server_running(data_dir)
        if pid:
            raise BackupError(f"the desk on {data_dir} is running (pid {pid}); stop it first (pair-desk stop)")
    stamp = _stamp(now)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = data_dir.with_name(f"{data_dir.name}.restoring-{stamp}")
    if staging.exists():
        raise BackupError(f"{staging} already exists")
    expected = {f["path"]: f for f in manifest.get("files") or []}
    try:
        staging.mkdir()
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            for name in names:
                if not _safe_member(name):
                    raise BackupError(f"unsafe or unexpected entry in the backup: {name!r}")
            for name in expected:
                if name not in names:
                    raise BackupError(f"the backup is incomplete: {name} is missing")
            for name, info in expected.items():
                target = staging / Path(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                size = 0
                with z.open(name) as src, open(target, "wb") as dst:
                    for chunk in iter(lambda: src.read(_CHUNK), b""):
                        h.update(chunk)
                        size += len(chunk)
                        dst.write(chunk)
                if size != info.get("size") or h.hexdigest() != info.get("sha256"):
                    raise BackupError(f"the backup is damaged: {name} does not match its checksum")
        conn = sqlite3.connect(staging / DATABASE)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise BackupError(f"the backed-up database fails its integrity check: {check}")
        previous = None
        if data_dir.exists():
            if _has_data(data_dir):
                previous = data_dir.with_name(f"{data_dir.name}.before-restore-{stamp}")
                try:
                    os.replace(data_dir, previous)
                except OSError as e:
                    raise BackupError(f"cannot move {data_dir} aside ({e}); close the desk and agent sessions "
                                      f"that use it, then try again") from None
            else:
                data_dir.rmdir()
        try:
            os.replace(staging, data_dir)
        except OSError:
            if previous is not None:
                os.replace(previous, data_dir)  # put the old desk back
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # First open: migrations for an older backup and the search index rebuild.
    from .store import Store
    with Store(data_dir):
        pass
    links = load_links(data_dir)
    return {**manifest, "data": str(data_dir), "previous": str(previous) if previous else None,
            "missing_links": sorted(path for path in links if not Path(path).is_dir())}
