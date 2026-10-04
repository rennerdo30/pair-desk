"""The Pair Desk HTTP server: JSON API under /api plus the static web UI.

Binds 127.0.0.1 unless started with --lan. Rejects non-loopback clients, foreign Host headers
(DNS rebinding) and foreign browser Origins unless --lan. Writes (POST, PATCH, DELETE) are also refused from an
opaque `null` Origin and from requests the browser marks cross-site, so no web page can write to the desk.
Opening a build's folder or running it on this machine is stricter still (_launch_guard): only the page this
server served, which carries its per-server token, from this machine.
"""

from __future__ import annotations

import hmac
import hashlib
import ipaddress
import json
import mimetypes
import queue
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import APP_NAME, VERSION, launch
from .store import DeskError, Invalid, NotFound, Store, decode_base64, feed_cursor, read_changes


class Forbidden(DeskError):
    status = 403

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 80 * 1024 * 1024
LOOPBACK_ORIGIN = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$")
LOOPBACK_HOST = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$")
# Attachments open inline only for types a browser shows without running code.
INLINE_TYPES = re.compile(r"^(image/(png|jpeg|gif|webp|avif|bmp)|video/(mp4|webm)|text/plain|application/pdf)$")

# The page learns the server's launch token from this tag in index.html, filled in as it is served.
TOKEN_META = '<meta name="pair-desk-token" content="">'
TOKEN_HEADER = "X-Pair-Desk-Token"
WRITE_METHODS = ("POST", "PATCH", "DELETE")

STATIC_CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
              "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

ROUTES: list[tuple[str, re.Pattern, str]] = []

# Live updates: how often the watcher looks for commits, and the keep-alive interval of a stream.
WATCH_INTERVAL = 0.4
STREAM_PING = 15.0


class ChangeHub:
    """Watches the desk database and fans changes out to the web UI's event streams.

    Writers are not only this server: the CLI and the MCP server write the same SQLite file from
    other processes. So a watcher thread keeps its own read connection, checks `PRAGMA
    data_version` (it moves whenever any other connection commits, including this server's own
    store connection), and reads the change feed (store.read_changes) after its cursor. Each
    project's subscribers get the comments, activity and handoff saves of their project; a commit
    that left no feed row (a deletion, a project setting, a command pickup) sends `refresh`."""

    def __init__(self, db_path: Path, interval: float = WATCH_INTERVAL):
        self.db_path = Path(db_path)
        self.interval = interval
        self._subs: dict[str, set[queue.Queue]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.seq = 0

    def start(self) -> None:
        # The first cursor is taken now, before the server takes requests, so nothing written
        # between a page subscribing and the watcher's first look is missed.
        self._conn, self._cursor, self._version = self._open()
        self._thread = threading.Thread(target=self._run, name="pair-desk-watch", daemon=True)
        self._thread.start()

    def _open(self):
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=5,
                               check_same_thread=False)
        return conn, feed_cursor(conn), conn.execute("PRAGMA data_version").fetchone()[0]

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for subs in self._subs.values():
                for q in subs:
                    q.put(None)
        if self._thread:
            self._thread.join(timeout=2)

    def subscribe(self, slug: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.setdefault(slug, set()).add(q)
        return q

    def unsubscribe(self, slug: str, q: queue.Queue) -> None:
        with self._lock:
            self._subs.get(slug, set()).discard(q)

    def subscribers(self, slug: str | None = None) -> int:
        with self._lock:
            return sum(len(v) for k, v in self._subs.items() if slug is None or k == slug)

    def _publish(self, slug: str | None, payload: dict) -> None:
        with self._lock:
            self.seq += 1
            payload = {**payload, "seq": self.seq}
            targets = [q for k, v in self._subs.items() if slug is None or k == slug for q in v]
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # a stalled client; it reloads everything when it reconnects

    def _run(self) -> None:
        conn, cursor, version = self._conn, self._cursor, self._version
        while not self._stop.is_set():
            try:
                if conn is None:
                    conn, _, version = self._open()  # keep the cursor: catch up on what was missed
                    version = None
                now_version = conn.execute("PRAGMA data_version").fetchone()[0]
                if now_version != version:
                    version = now_version
                    res = read_changes(conn, cursor)
                    cursor = res["cursor"]
                    by_project: dict[str, list] = {}
                    for ch in res["changes"]:
                        by_project.setdefault(ch["project"], []).append(ch)
                    for slug, changes in by_project.items():
                        self._publish(slug, {"type": "change", "changes": changes})
                    if not by_project:
                        self._publish(None, {"type": "refresh"})
            except sqlite3.Error:
                if conn is not None:
                    conn.close()
                conn = None
            self._stop.wait(self.interval)
        if conn is not None:
            conn.close()


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn
    return deco


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    # The stdlib default backlog of five makes simultaneous clients wait for TCP retries.
    request_queue_size = 128

    def __init__(self, addr, store: Store, lan: bool = False, verbose: bool = False):
        self.store = store
        self.lan = lan
        self.verbose = verbose
        # A fresh secret per server run: only a page this server served can open or run a build.
        self.token = secrets.token_urlsafe(24)
        super().__init__(addr, Handler)
        self.hub = ChangeHub(store.db_path)
        self.hub.start()

    def server_close(self):
        self.hub.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: DeskServer
    protocol_version = "HTTP/1.1"
    server_version = f"PairDesk/{VERSION}"
    disable_nagle_algorithm = True

    # -- plumbing ---------------------------------------------------------------------------

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    @property
    def store(self) -> Store:
        return self.server.store

    def _origin_allowed(self, origin: str) -> bool:
        if origin == "null" or LOOPBACK_ORIGIN.match(origin):
            return True
        if self.server.lan:
            host = self.headers.get("Host", "")
            return origin in (f"http://{host}", f"https://{host}")
        return False

    def _loopback_client(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            return ip.is_loopback
        except ValueError:
            return False

    def _guard(self, method: str = "GET") -> bool:
        """Local-only policy. Returns False after sending a 403."""
        if not self.server.lan:
            if not self._loopback_client():
                self._json({"error": "Pair Desk only accepts loopback clients (start with --lan to allow the LAN)"}, 403)
                return False
            host = self.headers.get("Host", "")
            if host and not LOOPBACK_HOST.match(host):
                self._json({"error": "foreign Host header rejected"}, 403)
                return False
        origin = self.headers.get("Origin")
        if origin is not None and not self._origin_allowed(origin):
            self._json({"error": "origin not allowed"}, 403)
            return False
        if method in WRITE_METHODS and (origin == "null" or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            # A sandboxed frame or a page on another site: it may read nothing private and write nothing.
            self._json({"error": "cross-site writes are not allowed"}, 403)
            return False
        return True

    def _launch_guard(self) -> None:
        """Opening a folder or starting a program on this machine: only from this machine, only from the page
        this server served (it carries the server's token in a header a foreign page cannot send without a
        preflight the desk refuses), and only same-origin."""
        if not self._loopback_client():
            raise Forbidden("builds are opened and run only from the machine the desk runs on")
        if not hmac.compare_digest(self.headers.get(TOKEN_HEADER, "").encode(), self.server.token.encode()):
            raise Forbidden("missing or wrong desk token: open and run work only from the desk's own page")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin is not None and origin != f"http://{host}":
            raise Forbidden("open and run are allowed only from the desk's own page")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise Forbidden("open and run are allowed only from the desk's own page")

    def _local(self, build: dict | None) -> dict | None:
        """The build with `local` (what this machine can do with its path) for a client on this machine."""
        if not build:
            return build
        info = launch.local_info(build.get("path")) if self._loopback_client() else launch.local_info(None)
        return {**build, "local": info}

    def _cors(self):
        self.send_header("Vary", "Origin")
        origin = self.headers.get("Origin")
        if origin is not None and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._representation(status, body, "application/json; charset=utf-8")

    def _representation(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        headers = {"Cache-Control": "no-store", **(extra or {})}
        if status == 200 and self.command in ("GET", "HEAD"):
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            headers.update({"ETag": etag, "Cache-Control": "private, no-cache"})
            tags = [tag.strip().removeprefix("W/") for tag in self.headers.get("If-None-Match", "").split(",")]
            if etag in tags or "*" in tags:
                return self._send(304, b"", ctype, headers)
        self._send(status, body, ctype, headers)

    def _cached_json(self, key: tuple, load):
        def snapshot():
            # Nested store reads bypass their object cache while inside this snapshot.
            # Cold responses are serialized once, not cached/decoded/re-encoded twice.
            with self.store._snapshot():
                return load()
        body = self.store.cached_json(("http", *key), snapshot)
        self._representation(200, body, "application/json; charset=utf-8")

    def _no_content(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise Invalid(f"request body larger than {MAX_BODY // (1024 * 1024)} MB")
        self._body_consumed = True
        return self.rfile.read(length) if length > 0 else b""

    def _json_body(self) -> dict:
        raw = self._read_body()
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Invalid("request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise Invalid("request body must be a JSON object")
        return data

    def _dispatch(self, method: str):
        self._body_consumed = False
        try:
            self._route(method)
        finally:
            if not self._body_consumed and int(self.headers.get("Content-Length") or 0) > 0:
                # An unread request body would corrupt the next request on this connection.
                self.close_connection = True

    def _route(self, method: str):
        if not self._guard(method):
            return
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        self.query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=False).items()}
        try:
            if path.startswith("/api/"):
                for m, rx, name in ROUTES:
                    if m != method:
                        continue
                    match = rx.match(path)
                    if match:
                        return getattr(self, name)(*match.groups())
                if any(rx.match(path) for _, rx, _ in ROUTES):
                    return self._json({"error": f"{method} not allowed here"}, 405)
                return self._json({"error": f"no API route {path}"}, 404)
            if method in ("GET", "HEAD"):
                return self._static(path)
            return self._json({"error": "not found"}, 404)
        except DeskError as e:
            return self._json({"error": str(e)}, e.status)
        except (ConnectionError, TimeoutError):
            return None
        except Exception as e:  # noqa: BLE001 - last line of defence, report and keep serving
            import traceback
            traceback.print_exc()
            return self._json({"error": f"internal error: {e}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        if not self._guard():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    # -- static -----------------------------------------------------------------------------

    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        if rel == "index.html":
            body = body.replace(TOKEN_META.encode(), TOKEN_META.replace('content=""', f'content="{self.server.token}"').encode())
        self._representation(200, body, ctype, {
            "Cache-Control": "no-cache",
            "Content-Security-Policy": STATIC_CSP,
        })

    # -- API --------------------------------------------------------------------------------

    @route("GET", r"/api/health")
    def api_health(self):
        self._json({"ok": True, "version": VERSION, "name": APP_NAME})

    @route("GET", r"/api/projects")
    def api_projects(self):
        self._cached_json(("projects",), lambda: {"projects": self.store.list_projects()})

    @route("POST", r"/api/projects")
    def api_project_create(self):
        b = self._json_body()
        self._json(self.store.create_project(b.get("slug"), b.get("name"), b.get("prefix")), 201)

    @route("GET", r"/api/projects/([^/]+)")
    def api_project(self, slug):
        p = self.store.project_overview(slug)
        self._json({**p, "build": self._local(p["build"])})

    @route("PATCH", r"/api/projects/([^/]+)")
    def api_project_update(self, slug):
        self._json(self.store.update_project(slug, self._json_body()))

    @route("GET", r"/api/projects/([^/]+)/build")
    def api_build(self, slug):
        self._json(self.store.get_build(slug))

    @route("POST", r"/api/projects/([^/]+)/build")
    def api_build_set(self, slug):
        b = self._json_body()
        self._json(self.store.set_build(slug, b.get("path"), b.get("commit"), b.get("label"), b.get("built_at"),
                                        b.get("author")))

    @route("DELETE", r"/api/projects/([^/]+)/build")
    def api_build_clear(self, slug):
        self._json(self.store.clear_build(slug, off=self.query.get("off") in ("1", "true", "yes")))

    def _launch(self, build: dict | None, action: str):
        """Open or run a stored build. The request body is read and ignored: the path is always the stored one."""
        self._launch_guard()
        self._read_body()
        if not build:
            raise NotFound("there is no build to " + action)
        self._json((launch.reveal if action == "open" else launch.run)(build["path"]))

    @route("POST", r"/api/projects/([^/]+)/build/(open|run)")
    def api_build_launch(self, slug, action):
        self._launch(self.store.get_build(slug)["build"], action)

    @route("POST", r"/api/issues/([^/]+)/build/(open|run)")
    def api_issue_build_launch(self, key, action):
        self._launch(self.store.get_issue(key, full=False)["build"], action)

    @route("GET", r"/api/projects/([^/]+)/events")
    def api_events(self, slug):
        """Server-Sent Events: `change` (feed rows of this project) and `refresh` (reload)."""
        project = self.store.get_project(slug)
        q = self.server.hub.subscribe(project["slug"])
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            hello = json.dumps({"type": "hello", "project": project["slug"], "cursor": self.store.event_cursor()})
            self.wfile.write(f"retry: 3000\nevent: hello\ndata: {hello}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=STREAM_PING)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                data = json.dumps(item, ensure_ascii=False)
                self.wfile.write(f"id: {item['seq']}\nevent: {item['type']}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            self.server.hub.unsubscribe(project["slug"], q)

    @route("GET", r"/api/projects/([^/]+)/suggest-groups")
    def api_suggest_groups(self, slug):
        self._json(self.store.suggest_groups(slug, int(self.query.get("limit") or 20)))

    @route("GET", r"/api/projects/([^/]+)/handoff")
    def api_handoff(self, slug):
        self._cached_json(("handoff", slug, self.query.get("version")),
                          lambda: self.store.get_handoff(slug, self.query.get("version")))

    @route("POST", r"/api/projects/([^/]+)/handoff")
    def api_handoff_save(self, slug):
        b = self._json_body()
        if "section" in b:
            self._json(self.store.update_handoff(slug, b.get("section"), b.get("text"), b.get("author")))
        else:
            self._json(self.store.set_handoff(slug, b.get("markdown"), b.get("author"), b.get("note")))

    @route("GET", r"/api/projects/([^/]+)/handoff/history")
    def api_handoff_history(self, slug):
        self._json(self.store.handoff_history(slug))

    @route("GET", r"/api/projects/([^/]+)/handoff/diff")
    def api_handoff_diff(self, slug):
        self._json(self.store.handoff_diff(slug, self.query.get("from"), self.query.get("to")))

    @route("GET", r"/api/projects/([^/]+)/issues")
    def api_issues(self, slug):
        self._cached_json(("issues", slug, json.dumps(self.query, sort_keys=True)),
                          lambda: self.store.list_issues(slug, self.query))

    @route("POST", r"/api/projects/([^/]+)/issues")
    def api_issue_create(self, slug):
        b = self._json_body()
        self._json(self.store.create_issue(slug, b, actor=b.get("author")), 201)

    @route("GET", r"/api/projects/([^/]+)/commands")
    def api_commands(self, slug):
        self._json({"commands": self.store.list_commands(slug, int(self.query.get("limit") or 20))})

    @route("POST", r"/api/projects/([^/]+)/commands")
    def api_command_queue(self, slug):
        b = self._json_body()
        self._json(self.store.queue_command(slug, b.get("command"), b.get("issue"), b.get("author")), 201)

    @route("GET", r"/api/projects/([^/]+)/commands/next")
    def api_command_next(self, slug):
        cmd = self.store.next_command(slug, self.query.get("client"))
        if cmd is None:
            return self._no_content()
        self._json(cmd)

    @route("GET", r"/api/commands/(\d+)")
    def api_command(self, cid):
        self._json(self.store.get_command(cid))

    @route("GET", r"/api/issues/([^/]+)")
    def api_issue(self, key):
        # Cache the serialized timeline too. The small base issue supplies the build path;
        # its current filesystem capabilities are part of the key, so deleting/moving a
        # build without a DB write still updates the owner's Open/Run controls immediately.
        base = self.store.get_issue(key, full=False)
        local = self._local(base["build"])
        def load():
            issue = self.store.get_issue(key)
            return {**issue, "build": self._local(issue["build"])}
        self._cached_json(("issue", key, json.dumps(local, sort_keys=True)), load)

    @route("PATCH", r"/api/issues/([^/]+)")
    def api_issue_update(self, key):
        b = self._json_body()
        self._json(self.store.update_issue(key, b, actor=b.get("actor")))

    @route("DELETE", r"/api/issues/([^/]+)")
    def api_issue_delete(self, key):
        self._json(self.store.delete_issue(key))

    @route("POST", r"/api/issues/([^/]+)/comments")
    def api_comment(self, key):
        b = self._json_body()
        self._json(self.store.add_comment(key, b.get("author"), b.get("text"), b.get("verdict"),
                                          b.get("attachments")), 201)

    @route("POST", r"/api/issues/([^/]+)/plan")
    def api_plan(self, key):
        b = self._json_body()
        self._json(self.store.set_plan(key, b.get("steps"), b.get("verification"), b.get("actor") or "owner"))

    @route("PATCH", r"/api/issues/([^/]+)/plan/steps/(\d+)")
    def api_plan_step(self, key, index):
        b = self._json_body()
        self._json(self.store.update_step(key, int(index), b.get("state"), b.get("commit"), b.get("note"),
                                          b.get("text"), b.get("actor") or "owner"))

    @route("POST", r"/api/issues/([^/]+)/parent")
    def api_parent(self, key):
        b = self._json_body()
        self._json(self.store.link_parent(key, b.get("parent"), b.get("actor") or "owner"))

    @route("POST", r"/api/issues/([^/]+)/merge")
    def api_merge(self, key):
        b = self._json_body()
        self._json(self.store.merge_issues(key, b.get("sources") or [], b.get("actor") or "owner"))

    @route("POST", r"/api/issues/([^/]+)/unmerge")
    def api_unmerge(self, key):
        b = self._json_body()
        self._json(self.store.unmerge(key, b.get("actor") or "owner"))

    @route("POST", r"/api/issues/([^/]+)/attachments")
    def api_attach(self, key):
        ctype = self.headers.get("Content-Type", "")
        if ctype.lower().startswith("multipart/form-data"):
            raw = self._read_body()
            msg = BytesParser(policy=policy.HTTP).parsebytes(
                b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
            if not msg.is_multipart():
                raise Invalid("malformed multipart body")
            fields, files = {}, []
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if filename is not None:
                    files.append((filename, part.get_content_type(), payload))
                elif name:
                    fields[name] = payload.decode("utf-8", "replace")
            if not files:
                raise Invalid("no file in multipart body")
            out = [self.store.add_attachment(key, fn, mt, data, fields.get("comment_id") or None,
                                             fields.get("author")) for fn, mt, data in files]
        else:
            b = self._json_body()
            items = b.get("attachments") if isinstance(b.get("attachments"), list) else [b]
            out = []
            for a in items:
                if not isinstance(a, dict):
                    raise Invalid("each attachment must be an object")
                data = decode_base64(a.get("data_base64") or "")
                out.append(self.store.add_attachment(key, a.get("filename") or "attachment", a.get("mime"), data,
                                                     a.get("comment_id") or b.get("comment_id"),
                                                     b.get("author")))
        self._json({"attachments": out}, 201)

    @route("GET", r"/api/attachments/(\d+)")
    def api_attachment(self, att_id):
        meta, path = self.store.get_attachment(att_id)
        try:
            f = path.open("rb")
        except OSError:
            raise NotFound("attachment file is missing from the data folder") from None
        inline = bool(INLINE_TYPES.match(meta["mime"]))
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", meta["filename"])
        # Keep memory bounded for large files and concurrent image galleries. HEAD sends
        # metadata without reading any bytes; opening before headers preserves missing-file errors.
        with f:
            import os
            self.send_response(200)
            self.send_header("Content-Type", meta["mime"] if inline else "application/octet-stream")
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{safe_name}"')
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'")
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                try:
                    shutil.copyfileobj(f, self.wfile, length=256 * 1024)
                except OSError:
                    # Headers are already sent; end this connection rather than append a
                    # second JSON response to a partially transferred file.
                    self.close_connection = True

    @route("DELETE", r"/api/attachments/(\d+)")
    def api_attachment_delete(self, att_id):
        self._json(self.store.delete_attachment(att_id))


def make_server(store: Store, port: int = 8765, lan: bool = False, verbose: bool = False) -> DeskServer:
    host = "0.0.0.0" if lan else "127.0.0.1"
    return DeskServer((host, port), store, lan=lan, verbose=verbose)


def start_in_thread(store: Store, port: int = 0, lan: bool = False) -> tuple[DeskServer, threading.Thread]:
    srv = make_server(store, port, lan=lan)
    t = threading.Thread(target=srv.serve_forever, name="pair-desk-http", daemon=True)
    t.start()
    return srv, t
