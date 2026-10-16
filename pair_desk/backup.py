"""Whole-desk backup and restore (CLI `backup` and `restore`).

A backup is one zip:
- `desk.sqlite`: a consistent snapshot taken with SQLite's online backup, so the desk keeps running.
  The search index is left out (it holds copies only) and is rebuilt when the restored desk opens.
- `attachments/...`: every file the snapshot's attachment rows point at.
- `projects.json`: the repo-folder links, when there are any.
- `manifest.json`: format, app and schema version, counts, and the size and SHA-256 of every file.

Restore never overwrites and checks everything before it touches the data folder: it unpacks into a
staging folder next to it, checks every entry, the database (integrity, schema, attachment paths)
and opens it once (migrations, search index). Only then does it move the current folder aside to
`<data>.before-restore-<time>` (only with `replace`) and put the restored one in its place.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
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
# Path parts that are unsafe on some file system: Windows device names, characters Windows refuses,
# trailing dots or spaces.
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')


class BackupError(DeskError):
    pass


def _stamp(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def _safe_attachment_path(name: str) -> bool:
    """`attachments/...`, relative, every part a plain file name on every platform."""
    if not isinstance(name, str) or not name:
        return False
    parts = name.split("/")
    return (len(parts) > 1 and parts[0] == "attachments"
            and all(p and p not in (".", "..") and not _BAD_CHARS.search(p) and not _RESERVED.match(p)
                    and not p.endswith((".", " ")) for p in parts))


def _safe_member(name: str) -> bool:
    """A zip entry this format writes, whose path stays inside the folder it is unpacked into."""
    return name in (MANIFEST, DATABASE, LINKS_FILE) or _safe_attachment_path(name)


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


def _add_file(z: zipfile.ZipFile, name: str, src: Path) -> dict:
    """Stream `src` into the zip as `name`, hashing what was written (one read, so a file that changes
    meanwhile still matches its manifest entry)."""
    info = zipfile.ZipInfo.from_file(src, name)
    info.compress_type = zipfile.ZIP_STORED if PurePosixPath(name).suffix.lower() in _STORED else zipfile.ZIP_DEFLATED
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as f, z.open(info, "w", force_zip64=True) as dst:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
            dst.write(chunk)
    return {"path": name, "size": size, "sha256": h.hexdigest()}


def _publish(part: Path, out: Path) -> None:
    """Move the finished `part` to `out` without ever replacing a file that appeared there meanwhile."""
    try:
        os.link(part, out)
    except FileExistsError:
        raise BackupError(f"{out} already exists; choose another --out") from None
    except OSError:
        # No hard links on this file system (FAT, some network shares): check, then rename.
        if out.exists():
            raise BackupError(f"{out} already exists; choose another --out") from None
        os.replace(part, out)
        return
    part.unlink()


def create_backup(data_dir: Path, out: Path | None = None, now: _dt.datetime | None = None) -> dict:
    """Write a backup zip of the desk in `data_dir` to `out` (default `<data>/backups/pair-desk-<time>.zip`).
    Returns the manifest plus `path`, `size`, `attachment_files` and `missing` (attachment rows whose file
    is gone)."""
    data_dir = Path(data_dir)
    out = Path(out) if out else default_backup_path(data_dir, now)
    if out.exists():
        raise BackupError(f"{out} already exists; choose another --out")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
    with tempfile.TemporaryDirectory(prefix="pair-desk-backup-") as tmp:
        db_copy = Path(tmp) / DATABASE
        snap = _snapshot(data_dir / DATABASE, db_copy)
        missing = [rel for rel in snap["attachments"] if not _safe_attachment_path(rel)]
        try:
            with zipfile.ZipFile(part, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                files = [_add_file(z, DATABASE, db_copy)]
                for rel in snap["attachments"]:
                    if rel in missing:
                        continue
                    try:
                        files.append(_add_file(z, rel, data_dir / Path(*rel.split("/"))))
                    except FileNotFoundError:
                        missing.append(rel)  # gone before or while this backup ran
                links = data_dir / LINKS_FILE
                if links.is_file():
                    files.append(_add_file(z, LINKS_FILE, links))
                manifest = {
                    "format": FORMAT,
                    "format_version": FORMAT_VERSION,
                    "app_version": VERSION,
                    "schema": snap["schema"],
                    "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "source": str(data_dir),
                    "counts": snap["counts"],
                    "projects": snap["projects"],
                    "missing_attachments": sorted(missing),
                    "files": files,
                }
                # Last, so it describes exactly what was written.
                z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            _publish(part, out)
        finally:
            part.unlink(missing_ok=True)
    return {**manifest, "path": str(out), "size": out.stat().st_size, "missing": manifest["missing_attachments"],
            "attachment_files": sum(1 for f in files if f["path"].startswith("attachments/"))}


def _schema_number(value) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 1 << 30  # unreadable: treat as newer, so it is refused


def read_manifest(zip_path: Path) -> dict:
    """The backup's manifest, after checking it is a Pair Desk backup this version can restore."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise BackupError(f"{zip_path} is not a Pair Desk backup ({e})") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError(f"{zip_path} is not a Pair Desk backup (no {FORMAT} manifest)")
    if _schema_number(manifest.get("format_version")) > FORMAT_VERSION:
        raise BackupError(f"{zip_path} was made by a newer Pair Desk ({manifest.get('app_version')}); update first")
    if _schema_number(manifest.get("schema")) > int(SCHEMA_VERSION):
        raise BackupError(f"{zip_path} has database schema {manifest.get('schema')}, newer than this Pair Desk "
                          f"({VERSION}, schema {SCHEMA_VERSION}); update first")
    files = manifest.get("files")
    if (not isinstance(files, list) or not files
            or not all(isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("size"), int)
                       and isinstance(f.get("sha256"), str) for f in files)
            or not isinstance(manifest.get("counts"), dict)):
        raise BackupError(f"{zip_path} has a damaged manifest")
    paths = [f["path"] for f in files]
    if DATABASE not in paths:
        raise BackupError(f"{zip_path} has no database ({DATABASE})")
    unsafe = [p for p in paths if not _safe_member(p)]
    if unsafe:
        raise BackupError(f"unsafe or unexpected entry in the backup: {unsafe[0]!r}")
    folded = [p.casefold() for p in paths]
    if len(set(folded)) != len(folded):
        raise BackupError(f"{zip_path} lists the same file twice (names may differ only in case)")
    return manifest


def _extract(z: zipfile.ZipFile, manifest: dict, into: Path | None) -> None:
    """Check every manifest entry's size and checksum, writing it under `into` (or only reading it)."""
    names = set(z.namelist())
    for info in manifest["files"]:
        name = info["path"]
        if name not in names:
            raise BackupError(f"the backup is incomplete: {name} is missing")
        if z.getinfo(name).file_size != info["size"]:
            raise BackupError(f"the backup is damaged: {name} does not have its listed size")
        target = None
        if into is not None:
            target = into / Path(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        with z.open(name) as src, (open(target, "xb") if target else open(os.devnull, "wb")) as dst:
            for chunk in iter(lambda: src.read(_CHUNK), b""):
                size += len(chunk)
                if size > info["size"]:
                    raise BackupError(f"the backup is damaged: {name} is larger than listed")
                h.update(chunk)
                dst.write(chunk)
        if size != info["size"] or h.hexdigest() != info["sha256"]:
            raise BackupError(f"the backup is damaged: {name} does not match its checksum")


def check_backup(zip_path: Path) -> dict:
    """Read the manifest and verify every file's size and checksum, without writing anything."""
    manifest = read_manifest(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        _extract(z, manifest, None)
    return manifest


def _check_database(path: Path, manifest: dict) -> list[str]:
    """Integrity, schema and attachment paths of the unpacked database; returns the attachment files it
    references that the backup neither holds nor lists as missing."""
    if not path.is_file() or path.stat().st_size == 0:
        raise BackupError("the backup's database is empty")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"the backed-up database fails its integrity check: {check}")
        schema = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if schema is None or _schema_number(schema[0]) > int(SCHEMA_VERSION):
            raise BackupError(f"the backed-up database has schema {schema[0] if schema else 'none'}, which this "
                              f"Pair Desk ({VERSION}, schema {SCHEMA_VERSION}) cannot open; update first")
        stored = [r[0] for r in conn.execute("SELECT DISTINCT stored_path FROM attachments")]
    except sqlite3.DatabaseError as e:
        raise BackupError(f"the backup's database cannot be read ({e})") from None
    finally:
        conn.close()
    bad = [p for p in stored if not _safe_attachment_path(p)]
    if bad:
        raise BackupError(f"the backed-up database points an attachment outside the desk: {bad[0]!r}")
    held = {f["path"] for f in manifest["files"]} | set(manifest.get("missing_attachments") or [])
    return sorted(p for p in stored if p not in held)


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


def _database_in_use(data_dir: Path) -> bool:
    """True when another process has the desk database open. In WAL mode the last connection to close
    removes the -wal file, so it is still there after this check's own connection closes only while
    someone else (the server, an MCP server, a hook) holds the database."""
    db = data_dir / DATABASE
    if not db.is_file():
        return False
    try:
        conn = sqlite3.connect(db, timeout=2)
        try:
            wal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    return wal and Path(str(db) + "-wal").exists()


def _has_data(data_dir: Path) -> bool:
    return data_dir.is_dir() and any(data_dir.iterdir())


def _free_name(path: Path, kind: str, stamp: str) -> Path:
    candidate = path.with_name(f"{path.name}.{kind}-{stamp}")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{kind}-{stamp}-{n}")
        n += 1
    return candidate


def restore_backup(zip_path: Path, data_dir: Path, replace: bool = False, now: _dt.datetime | None = None) -> dict:
    """Restore a backup into `data_dir`. A folder that already has data needs `replace`: it is moved aside
    to `<data>.before-restore-<time>`, never deleted. Returns the manifest plus `data`, `previous` (where
    the old folder went, or None), `missing_links` (linked repo folders that do not exist here) and
    `unlisted_attachments` (files the database references that the backup does not hold)."""
    zip_path = Path(zip_path)
    data_dir = Path(os.path.abspath(data_dir))
    if data_dir.parent == data_dir:
        raise BackupError(f"{data_dir} is a drive or file system root; restore into a folder")
    if data_dir.exists() and not data_dir.is_dir():
        raise BackupError(f"{data_dir} is a file, not a data folder")
    manifest = read_manifest(zip_path)
    if _has_data(data_dir):
        if not replace:
            raise BackupError(f"{data_dir} already has a desk. Restore into an empty folder (--data), or pass "
                              f"--replace to move the current one aside first")
        pid = _server_running(data_dir)
        if pid:
            raise BackupError(f"the desk on {data_dir} is running (pid {pid}); stop it first (pair-desk stop)")
        if _database_in_use(data_dir):
            raise BackupError(f"the desk on {data_dir} is in use (the web server, or an agent session's MCP server "
                              f"or hook); stop the desk and close those sessions, then try again")
    stamp = _stamp(now)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = _free_name(data_dir, "restoring", stamp)
    keep_staging = False
    previous = None
    try:
        staging.mkdir()
        with zipfile.ZipFile(zip_path) as z:
            _extract(z, manifest, staging)
        unlisted = _check_database(staging / DATABASE, manifest)
        # First open here, before anything moves: migrations for an older backup and the search index.
        from .store import Store
        with Store(staging):
            pass
        if data_dir.exists():
            if _has_data(data_dir):
                previous = _free_name(data_dir, "before-restore", stamp)
                try:
                    os.replace(data_dir, previous)
                except OSError as e:
                    previous = None
                    raise BackupError(f"cannot move {data_dir} aside ({e}); stop the desk and close agent sessions "
                                      f"that use it, then try again") from None
            else:
                data_dir.rmdir()
        try:
            os.replace(staging, data_dir)
        except OSError as e:
            if previous is not None:
                try:
                    os.replace(previous, data_dir)
                except OSError:
                    keep_staging = True
                    raise BackupError(
                        f"the restore could not finish ({e}) and something recreated {data_dir} meanwhile. Nothing "
                        f"was deleted: your previous desk is in {previous}, the restored one in {staging}. Stop "
                        f"every desk process and agent session, then move the one you want to {data_dir}") from None
                previous = None
            raise BackupError(f"cannot move the restored desk into {data_dir} ({e}); nothing changed") from None
    except BaseException:
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    links = load_links(data_dir)
    return {**manifest, "data": str(data_dir), "previous": str(previous) if previous else None,
            "missing_links": sorted(path for path in links if not Path(path).is_dir()),
            "unlisted_attachments": unlisted}
