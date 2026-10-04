"""Isolated latency benchmark (stdlib only). Never opens a live Store or stops a foreign process.

First: --snapshot-live <data-folder> --snapshot .cache/perf-snapshot
Then: --snapshot .cache/perf-snapshot --data .cache/perf-data --out .cache/before.json
Repeat with the same snapshot after editing. Only the restored copy is writable.
Pass --project <slug>. --mixed-only --rounds 30 measures 20 MCP readers/writers alongside
owner HTTP reads, using rich copied histories. The HTTP server and 20 stdio MCP processes
are owned and cleaned up by this script.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
from http.client import HTTPConnection
import json
import math
import queue
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def snapshot(source: Path, dest: Path):
    if dest.is_relative_to(source):
        raise ValueError("snapshot must be outside the source data folder")
    if dest.exists():
        raise ValueError("snapshot already exists; use another destination")
    dest.mkdir(parents=True)
    # SQLite's online backup includes committed WAL pages and is safe while writers run.
    with sqlite3.connect(source.joinpath("desk.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as a:
        with sqlite3.connect(dest / "desk.sqlite") as b:
            a.backup(b, pages=256, sleep=0.01)
            assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for name in ("attachments", "projects.json"):
        src = source / name
        if src.is_dir():
            shutil.copytree(src, dest / name)
        elif src.is_file():
            shutil.copy2(src, dest / name)
    print("Read-only online backup completed", flush=True)
    with sqlite3.connect(dest / "desk.sqlite") as c:
        rows = c.execute("SELECT stored_path, size FROM attachments").fetchall()
        bad = [name for name, size in rows if not (dest / name).is_file() or (dest / name).stat().st_size != size]
        if bad:
            raise RuntimeError(f"{len(bad)} attachments changed during the snapshot; take a new snapshot")


def stats(values):
    vals = sorted(values)
    return {"n": len(vals), "p50_ms": round(statistics.median(vals), 3),
            "p95_ms": round(vals[math.ceil(len(vals) * .95) - 1], 3)}


def http(port, path, method="GET", body=None, connection=None):
    conn = connection or HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=json.dumps(body) if body else None,
                     headers={"Content-Type": "application/json"} if body else {})
        res = conn.getresponse()
        data = res.read()
        if res.status not in (200, 201):
            raise RuntimeError(f"{path}: {res.status} {data[:200]!r}")
        return data
    finally:
        if connection is None:
            conn.close()


def timed(fn):
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


class McpClient:
    def __init__(self, data, log, code=ROOT):
        self.log = log.open("wb")
        self.proc = subprocess.Popen([sys.executable, str(code / "desk.py"), "--data", str(data), "mcp"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        self.responses = queue.Queue()
        def read():
            for line in self.proc.stdout:
                self.responses.put(line)
            self.responses.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.seq = 0
        try:
            self.call("ping", {})
        except BaseException:
            self.close()
            raise

    def call(self, method, params):
        self.seq += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.seq, "method": method,
                                         "params": params}).encode() + b"\n")
        self.proc.stdin.flush()
        line = self.responses.get(timeout=60)
        if not line:
            raise RuntimeError("MCP process ended unexpectedly")
        res = json.loads(line)
        if "error" in res or res.get("result", {}).get("isError"):
            raise RuntimeError(res)
        return res

    def tool(self, name, args):
        return self.call("tools/call", {"name": name, "arguments": args})

    def close(self):
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        self.proc.stdout.close()
        self.log.close()


def run(args):
    snap, data = args.snapshot.resolve(), args.data.resolve()
    code = args.code.resolve()
    # Never delete arbitrary directories: only overwrite known benchmark files under .cache.
    cache = (ROOT / ".cache").resolve()
    if not data.is_relative_to(cache) or data == snap or data in snap.parents:
        raise ValueError("--data must be a separate child of this worktree's .cache")
    import socket
    # Refuse an occupied port before touching the benchmark copy.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    data.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        data.joinpath("desk.sqlite" + suffix).unlink(missing_ok=True)
    shutil.copy2(snap / "desk.sqlite", data / "desk.sqlite")
    if not (data / "attachments").exists():
        shutil.copytree(snap / "attachments", data / "attachments")
    if (snap / "projects.json").exists():
        shutil.copy2(snap / "projects.json", data / "projects.json")
    with sqlite3.connect(data / "desk.sqlite") as c:
        summary = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("projects", "issues", "comments", "attachments", "activity", "handoffs")}
        detail = c.execute("SELECT p.prefix || '-' || i.number FROM issues i JOIN projects p ON p.id=i.project_id "
                           "WHERE p.slug=? AND i.merged_into IS NULL ORDER BY "
                           "(SELECT COUNT(*) FROM comments WHERE issue_id=i.id) + "
                           "(SELECT COUNT(*) FROM attachments WHERE issue_id=i.id) DESC LIMIT 1", (args.project,)).fetchone()[0]
        area = c.execute("SELECT area FROM issues i JOIN projects p ON p.id=i.project_id WHERE p.slug=? "
                         "AND area<>'' GROUP BY area ORDER BY COUNT(*) DESC LIMIT 1", (args.project,)).fetchone()[0]
        rich_keys = [r[0] for r in c.execute(
            "SELECT p.prefix || '-' || i.number FROM issues i JOIN projects p ON p.id=i.project_id "
            "WHERE p.slug=? AND i.merged_into IS NULL ORDER BY "
            "(SELECT COUNT(*) FROM comments WHERE issue_id=i.id) DESC, i.number LIMIT 20", (args.project,))]
    from urllib.parse import urlencode
    prefix = f"/api/projects/{args.project}"
    endpoints = {
        "health": "/api/health", "projects": "/api/projects", "project": prefix,
        "triage_200": prefix + "/issues?sort=triage&limit=200&status=reported,open,in_progress,auto_check,to_check,failed",
        "triage_500": prefix + "/issues?sort=triage&limit=500",
        "backlog": prefix + "/issues?sort=backlog&limit=5000&status=open,in_progress",
        "ui_triage": prefix + "/issues?sort=triage&limit=300&summary=1&status=reported,open,in_progress,auto_check,to_check,failed",
        "ui_backlog": prefix + "/issues?sort=backlog&limit=5000&summary=1&status=open,in_progress",
        "filtered": prefix + "/issues?" + urlencode({"sort": "triage", "area": area, "priority": "p1,p2", "limit": 200}),
        "search": prefix + "/issues?q=terrain&limit=200",
        "detail": f"/api/issues/{detail}", "handoff": prefix + "/handoff", "html": "/",
    }
    server = None
    clients = []
    results = {"dataset": summary, "detail": detail, "area": area,
               "snapshot_sha256": hashlib.sha256((snap / "desk.sqlite").read_bytes()).hexdigest(),
               "configuration": {"project": args.project, "samples": args.samples,
                                 "rounds": args.rounds, "pace_ms": args.pace_ms, "port": args.port},
               "endpoints": endpoints,
               "python": sys.version, "serial": {}, "concurrent_20": {},
               "mode": "mixed" if args.mixed_only else "hot",
               "http_connections": "owner keep-alive" if args.mixed_only else "new per request"}
    log_path = args.out.with_suffix(".server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # A bind probe refuses to compete with an existing server, even on the test port.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
        with log_path.open("wb") as log:
            server = subprocess.Popen([sys.executable, str(code / "desk.py"), "--data", str(data),
                                       "serve", "--port", str(args.port)], stdout=log, stderr=log)
        for _ in range(200):
            try:
                http(args.port, "/api/health")
                break
            except OSError:
                if server.poll() is not None:
                    raise RuntimeError(f"server failed; see {log_path}")
                time.sleep(.05)
        hot_endpoints = {} if args.mixed_only else endpoints
        for name, path in hot_endpoints.items():
            for _ in range(3):
                http(args.port, path)
            results["serial"][name] = stats([timed(lambda: http(args.port, path)) for _ in range(args.samples)])
            print("serial", name, results["serial"][name], flush=True)
            args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            for name, path in hot_endpoints.items():
                barrier = threading.Barrier(20)
                def worker(_):
                    barrier.wait()
                    return [timed(lambda: http(args.port, path)) for _ in range(args.rounds)]
                values = [v for batch in pool.map(worker, range(20)) for v in batch]
                results["concurrent_20"][name] = stats(values)
                print("concurrent_20", name, results["concurrent_20"][name], flush=True)
                args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        # Mixed mode exercises rich histories; all writes are still only on the copied desk.
        keys = rich_keys if args.mixed_only else []
        if not args.mixed_only:
            http(args.port, "/api/projects", "POST", {"slug": "perf-fixture", "name": "Perf Fixture", "prefix": "PF"})
        for i in range(20):
            if not args.mixed_only:
                obj = json.loads(http(args.port, "/api/projects/perf-fixture/issues", "POST", {"title": f"Client {i}"}))
                keys.append(obj["id"])
            clients.append(McpClient(data, args.out.with_suffix(f".mcp-{i}.log"), code))
        tools = {
            "mcp_list": ("list_issues", {"project": args.project, "sort": "backlog", "status": "open,in_progress", "limit": 50}),
            "mcp_get": ("get_issue", {"id": detail}),
            "mcp_comment": ("comment", {"text": "Isolated benchmark comment", "author": "bench"}),
        }
        hot_tools = {} if args.mixed_only else tools
        for label, (name, params) in hot_tools.items():
            def tool_args(i):
                return {**params, "id": keys[i]} if name == "comment" else params
            for _ in range(3):
                clients[0].tool(name, tool_args(0))
            results["serial"][label] = stats([timed(lambda: clients[0].tool(name, tool_args(0))) for _ in range(args.samples)])
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                barrier = threading.Barrier(20)
                def worker(i):
                    barrier.wait()
                    return [timed(lambda: clients[i].tool(name, tool_args(i))) for _ in range(args.rounds)]
                values = [v for batch in pool.map(worker, range(20)) for v in batch]
            results["concurrent_20"][label] = stats(values)
            print(label, results["serial"][label], results["concurrent_20"][label], flush=True)
            args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        if args.mixed_only:
            barrier = threading.Barrier(21)
            owner_done = threading.Event()
            mixed_start = time.perf_counter()
            names = list(tools)
            def agent(i):
                barrier.wait()
                start = time.perf_counter()
                values = {name: [] for name in names}
                turn = 0
                # Keep the offered write rate steady until the owner finishes too. A slow
                # baseline must not get an idle tail merely because its reads take longer.
                while turn < args.rounds or not owner_done.is_set():
                    if args.pace_ms:
                        target = start + args.pace_ms / 1000 * (turn + i / 20)
                        time.sleep(max(0, target - time.perf_counter()))
                    for j in range(3):
                        label = names[(i + j) % 3]
                        name, params = tools[label]
                        params = {**params, "id": keys[i % len(keys)]} if name == "comment" else params
                        values[label].append(timed(lambda: clients[i].tool(name, params)))
                    turn += 1
                return values
            def owner():
                barrier.wait()
                values = {name: [] for name in ("health", "triage_200", "backlog", "ui_triage", "ui_backlog", "search", "detail", "project")}
                conn = HTTPConnection("127.0.0.1", args.port, timeout=60)
                try:
                    for _ in range(args.rounds):
                        for name in values:
                            values[name].append(timed(lambda: http(args.port, endpoints[name], connection=conn)))
                finally:
                    conn.close()
                    owner_done.set()
                return values
            with concurrent.futures.ThreadPoolExecutor(max_workers=21) as pool:
                workers = [pool.submit(agent, i) for i in range(20)]
                owner_future = pool.submit(owner)
                agents = [f.result() for f in workers]
                results["http_during_20_mcp"] = {k: stats(v) for k, v in owner_future.result().items()}
            results["mixed_20_mcp"] = {k: stats([v for a in agents for v in a[k]]) for k in names}
            results["mixed_elapsed_s"] = round(time.perf_counter() - mixed_start, 3)
            print("http_during_20_mcp", results["http_during_20_mcp"], flush=True)
            print("mixed_20_mcp", results["mixed_20_mcp"], flush=True)
        if args.browser:
            browser_out = args.out.with_suffix(".browser.json")
            subprocess.run(["node", str(ROOT / "scripts/bench-ui.mjs"), f"http://127.0.0.1:{args.port}",
                            args.project, str(browser_out)], check=True)
            results["browser"] = json.loads(browser_out.read_text())
        with sqlite3.connect(data / "desk.sqlite") as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    finally:
        for client in clients:
            client.close()
        if server is not None:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot-live", type=Path)
    p.add_argument("--code", type=Path, default=ROOT, help="checkout to run (e.g. an archived baseline)")
    p.add_argument("--snapshot", type=Path, default=ROOT / ".cache/perf-snapshot")
    p.add_argument("--data", type=Path, default=ROOT / ".cache/perf-data")
    p.add_argument("--out", type=Path, default=ROOT / ".cache/bench.json")
    p.add_argument("--project", default="mygame")
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--samples", type=int, default=40)
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--browser", action="store_true")
    p.add_argument("--mixed-only", action="store_true", help="20 MCP readers/writers plus owner HTTP reads; uses rich copied issues")
    p.add_argument("--pace-ms", type=float, default=0, help="mixed agent round interval, with staggered arrivals (0 = maximum throughput)")
    args = p.parse_args()
    if args.samples < 1 or args.rounds < 1 or args.pace_ms < 0:
        p.error("samples and rounds must be positive; pace-ms must be nonnegative")
    if args.snapshot_live:
        snapshot(args.snapshot_live.resolve(), args.snapshot.resolve())
    else:
        run(args)
