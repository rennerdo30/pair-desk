import io
import json
import os
import queue
import subprocess
import sys
import threading
import unittest

from helpers import ROOT, StoreCase, TempDirCase

from pair_desk.cli import _msys_root, main, session_summary, undo_msys_paths
from pair_desk.context import resolve_project, save_link
from pair_desk.mcp import McpServer
from pair_desk.store import Store

DESK = str(ROOT / "desk.py")


class FramingTests(TempDirCase):
    def frame(self, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    def replies(self, raw):
        source = io.BytesIO(raw)
        values = []
        while line := source.readline():
            self.assertTrue(line.startswith(b"Content-Length: "), line)
            length = int(line.split(b":", 1)[1])
            self.assertEqual(source.readline(), b"\r\n")
            body = source.read(length)
            self.assertEqual(len(body), length)
            values.append(json.loads(body))
        return values

    def serve(self, raw, source_type=io.BytesIO):
        output = io.BytesIO()
        McpServer(self.tmp).serve(source_type(raw), output)
        return output.getvalue()

    def test_content_length_multiple_frames_notifications_and_utf8(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "method": "notifications/cancelled"},
                    {"jsonrpc": "2.0", "id": "\u65e5\u672c", "method": "unknown/\u65e5\u672c"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
        result = self.replies(self.serve(b"".join(self.frame(value) for value in messages)))
        self.assertEqual([value["id"] for value in result], [1, "\u65e5\u672c", 3])
        self.assertEqual(result[1]["error"]["code"], -32601)
        self.assertEqual(result[2]["result"], {})

    def test_partial_body_reads_and_content_type_header(self):
        class Fragmented(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))
        value = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
        raw = b"Content-Type: application/json\r\n" + self.frame(value)
        self.assertEqual(self.replies(self.serve(raw, Fragmented))[0]["id"], 7)

    def test_malformed_or_truncated_frames_fail_and_stop(self):
        cases = [b"Content-Length: -1\r\n\r\n{}", b"Content-Length: 0\r\n\r\n",
                 b"Content-Length: 999999999\r\n\r\n", b"Content-Length: 10\r\n\r\n{}",
                 b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
                 b"Content-Type: application/json\r\n\r\n{}", b"Content-Length: 2\r\n"]
        for raw in cases:
            with self.subTest(raw=raw):
                result = self.replies(self.serve(raw))
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["error"]["code"], -32700)

    def test_jsonl_remains_the_default_and_recovers_after_parse_error(self):
        raw = b'{broken\n{"jsonrpc":"2.0","id":5,"method":"ping"}\n'
        result = [json.loads(line) for line in self.serve(raw).splitlines()]
        self.assertEqual(result[0]["error"]["code"], -32700)
        self.assertEqual(result[1]["id"], 5)

    def test_long_jsonl_messages_are_not_subject_to_header_limit(self):
        value = {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {"text": "x" * 9000}}
        raw = json.dumps(value).encode() + b"\n"
        self.assertEqual(json.loads(self.serve(raw))["id"], 6)

    def test_header_budget_includes_blank_delimiter_and_accepts_exact_limit(self):
        value = {"jsonrpc": "2.0", "id": 8, "method": "ping"}
        body = json.dumps(value).encode()
        prefix = f"Content-Length: {len(body)}\r\nX-Pad: ".encode()
        raw = prefix + b"x" * (8192 - len(prefix) - 4) + b"\r\n\r\n" + body
        self.assertEqual(self.replies(self.serve(raw))[0]["id"], 8)

    def test_oversized_header_is_rejected_without_newline_or_eof(self):
        for raw in (b"Content-Length: " + b"1" * 9000,
                    b"Content-Length: 2\r\nX-Pad: " + b"x" * 9000):
            with self.subTest(first_header=raw.startswith(b"Content-Length: 111")):
                proc = subprocess.Popen([sys.executable, DESK, "--data", str(self.tmp), "mcp"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    result = queue.Queue()
                    threading.Thread(target=lambda: result.put(proc.stdout.read()), daemon=True).start()
                    try:
                        proc.stdin.write(raw)
                        proc.stdin.flush()
                    except BrokenPipeError:
                        pass
                    # Leave stdin open: a rejected frame must not wait for EOF or a newline.
                    messages = self.replies(result.get(timeout=5))
                    self.assertEqual(messages[0]["error"]["code"], -32700)
                    self.assertEqual(proc.wait(timeout=5), 0)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=5)
                    for pipe in (proc.stdin, proc.stdout, proc.stderr):
                        try:
                            pipe.close()
                        except BrokenPipeError:
                            pass


def rpc(proc, msg):
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    if "id" not in msg:
        return None
    return json.loads(proc.stdout.readline())


class McpCase(TempDirCase):
    """Runs the real stdio server as a subprocess, the way Claude Code launches it."""

    def setUp(self):
        super().setUp()
        with Store(self.tmp) as s:
            s.create_project("mygame", "MyGame", "MG")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / ".pair-desk.json").write_text('{"project": "mygame"}', encoding="utf-8")
        env = dict(os.environ, PAIR_DESK_AUTHOR="claude")
        env.pop("CLAUDE_PROJECT_DIR", None)
        self.proc = subprocess.Popen([sys.executable, DESK, "--data", str(self.tmp), "mcp"], cwd=self.repo,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        super().tearDown()

    def call(self, n, name, args):
        res = rpc(self.proc, {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": args}})
        result = res["result"]
        text = result["content"][0]["text"]
        return result.get("isError", False), (text if result.get("isError") else json.loads(text))


class McpTests(McpCase):

    def test_protocol_round_trip(self):
        init = rpc(self.proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(rpc(self.proc, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = rpc(self.proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), sorted([
            "list_projects", "list_issues", "get_issue", "create_issue", "update_issue", "comment", "set_status", "set_location", "set_build",
            "queue_command", "import_checks", "set_plan", "update_step", "progress", "link_parent",
            "merge_issues", "unmerge", "suggest_groups", "get_handoff", "set_handoff", "update_handoff", "search"]))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")

        err, created = self.call(3, "create_issue", {"title": "Lava tips are round", "command": "/goto 5 6 7",
                                                     "external_ref": "commit 5ea28d6"})
        self.assertFalse(err)
        self.assertEqual((created["id"], created["status"], created["kind"], created["source"]),
                         ("MG-1", "to_check", "check", "agent"))
        err, projects = self.call(4, "list_projects", {})
        self.assertEqual(projects["linked_project"], "mygame")
        err, listing = self.call(5, "list_issues", {"status": "to_check"})
        self.assertEqual([i["id"] for i in listing["issues"]], ["MG-1"])
        self.assertEqual(listing["issues"][0]["command"], "/goto 5 6 7")

        err, msg = self.call(6, "set_status", {"id": "MG-1", "status": "passed"})
        self.assertTrue(err)
        self.assertIn("owner", msg)
        err, msg = self.call(7, "comment", {"id": "MG-1", "text": "done", "verdict": "passed"})
        self.assertTrue(err)
        err, res = self.call(8, "comment", {"id": "MG-1", "text": "reproduced: still square", "verdict": "failed"})
        self.assertEqual((res["issue"]["status"], res["comment"]["author"]), ("failed", "claude"))

        err, summary = self.call(9, "import_checks", {"checks": [
            {"title": "Lava again", "external_ref": "commit 5ea28d6"}, {"title": "Boats", "external_ref": "TODO boats"}]})
        self.assertEqual((summary["created"], len(summary["skipped"])), (["MG-2"], 1))
        err, cmd = self.call(10, "queue_command", {"command": "/goto 5 6 7", "issue": "MG-1"})
        self.assertEqual(cmd["state"], "pending")
        err, issue = self.call(11, "get_issue", {"id": "MG-1"})
        self.assertEqual(issue["comments"][0]["verdict"], "failed")
        err, msg = self.call(12, "get_issue", {"id": "MG-404"})
        self.assertTrue(err)

        unknown = rpc(self.proc, {"jsonrpc": "2.0", "id": 13, "method": "bogus/method"})
        self.assertEqual(unknown["error"]["code"], -32601)
        self.proc.stdin.write(b"{broken\n")
        self.proc.stdin.flush()
        self.assertEqual(json.loads(self.proc.stdout.readline())["error"]["code"], -32700)


class ContextAndHookTests(StoreCase):
    def test_links_and_marker(self):
        repo = self.tmp / "game"
        (repo / "sub" / "deep").mkdir(parents=True)
        self.assertIsNone(resolve_project(self.tmp, repo))
        save_link(self.tmp, repo, "mygame")
        self.assertEqual(resolve_project(self.tmp, repo / "sub" / "deep"), "mygame")
        self.assertIsNone(resolve_project(self.tmp, self.tmp / "gamex"))
        (repo / "sub" / ".pair-desk.json").write_text('{"project": "other"}', encoding="utf-8")
        self.assertEqual(resolve_project(self.tmp, repo / "sub" / "deep"), "other")

    def test_session_summary(self):
        repo = self.tmp / "game"
        repo.mkdir()
        self.assertIsNone(session_summary(self.tmp, repo))
        save_link(self.tmp, repo, "mygame")
        self.assertIn("nothing new", session_summary(self.tmp, repo))
        s = self.store
        s.create_issue("mygame", {"title": "r1"})
        s.create_issue("mygame", {"title": "c1", "status": "to_check"})
        s.create_issue("mygame", {"title": "c2", "status": "to_check"})
        s.create_issue("mygame", {"title": "f1", "status": "failed"})
        self.assertEqual(session_summary(self.tmp, repo),
                         "Pair Desk (MyGame): 1 new report, 1 failed check, 2 waiting for the owner, "
                         "0 auto_check waiting for agent verification (screenshots, logs, tests)")

    def test_hook_is_silent_without_setup(self):
        empty = self.tmp / "no-data"
        out = subprocess.run([sys.executable, DESK, "--data", str(empty), "hook", "session-start"],
                             input=b'{"cwd": "C:/nowhere"}', capture_output=True, timeout=20)
        self.assertEqual((out.returncode, out.stdout), (0, b""))
        self.assertFalse(empty.exists())
        out = subprocess.run([sys.executable, DESK, "--data", str(self.tmp), "hook", "session-start"],
                             input=b"not json at all", capture_output=True, timeout=20)
        self.assertEqual(out.returncode, 0)

    def test_hook_prints_summary(self):
        repo = self.tmp / "game"
        repo.mkdir()
        save_link(self.tmp, repo, "mygame")
        self.store.create_issue("mygame", {"title": "f1", "status": "failed"})
        out = subprocess.run([sys.executable, DESK, "--data", str(self.tmp), "hook", "session-start"],
                             input=json.dumps({"cwd": str(repo)}).encode(), capture_output=True, timeout=20)
        data = json.loads(out.stdout)
        self.assertIn("1 failed check", data["systemMessage"])
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SessionStart")


class CliTests(StoreCase):
    def run_cli(self, *argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--data", str(self.tmp), *argv])
        return code, buf.getvalue()

    def test_agent_flow(self):
        code, out = self.run_cli("add", "--project", "mygame", "--title", "Check fog", "--kind", "check",
                                 "--command", "/goto 1 1 1", "--ref", "TODO fog", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "to_check")
        code, out = self.run_cli("comment", "MG-1", "--author", "owner", "--text", "fog pops", "--verdict", "failed")
        self.assertIn("failed", out)
        code, out = self.run_cli("list", "--project", "mygame", "--status", "failed", "--json")
        self.assertEqual(json.loads(out)["issues"][0]["id"], "MG-1")
        code, out = self.run_cli("status", "MG-1", "in_progress")
        self.assertIn("in_progress", out)
        code, out = self.run_cli("show", "MG-1")
        self.assertIn("fog pops", out)
        code, out = self.run_cli("send", "--issue", "MG-1")
        self.assertIn("/goto 1 1 1", out)
        path = self.tmp / "import.json"
        path.write_text(json.dumps([{"title": "Fog again", "external_ref": "TODO fog"},
                                    {"title": "Sky", "external_ref": "TODO sky"}]), encoding="utf-8")
        code, out = self.run_cli("import-json", str(path), "--project", "mygame", "--json")
        self.assertEqual(json.loads(out)["created"], ["MG-2"])
        code, out = self.run_cli("export", "--project", "mygame", "--json")
        self.assertEqual(len(json.loads(out)["issues"]), 2)

    def test_errors_exit_nonzero(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("show", "MG-99")[0], 1)
            self.assertEqual(self.run_cli("status", "MG-99", "open")[0], 1)


if __name__ == "__main__":
    unittest.main()


class MsysPathTests(unittest.TestCase):
    """Git Bash turns a leading `/` into its install folder before Python sees the argument."""

    def test_game_commands_get_their_slash_back(self):
        root = "C:/Program Files/Git"
        argv = ["add", "--command", "C:/Program Files/Git/goto 1 2; /time 06:00", "--title=C:/Program Files/Git/goto slow",
                "--text", "C:\Program Files\Git\check MG-1", "--attach", "C:/Users/me/shot.png"]
        self.assertEqual(undo_msys_paths(argv, root),
                         ["add", "--command", "/goto 1 2; /time 06:00", "--title=/goto slow",
                          "--text", "/check MG-1", "--attach", "C:/Users/me/shot.png"])

    def test_nothing_changes_outside_msys(self):
        argv = ["add", "--command", "C:/Program Files/Git/goto 1 2"]
        self.assertEqual(undo_msys_paths(argv, None), argv)
        self.assertIsNone(_msys_root({}))
