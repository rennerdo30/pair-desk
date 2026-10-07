"""HTTP routes for plans, groups, merges and the handoff; the live event stream; the MCP tools and
channel; the prompt hook and the CLI commands of PD-1."""

import contextlib
import http.client
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

from helpers import ROOT, StoreCase, TempDirCase
from test_api import ApiCase
from test_mcp_cli import DESK, McpCase, rpc

from pair_desk import notify
from pair_desk.cli import main
from pair_desk.context import save_link
from pair_desk.store import Store


class ApiRouteTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.store.create_project("mygame", "MyGame", "MG")
        for t in ("Boat sinks", "Boat sinks again", "Water epic"):
            self.store.create_issue("mygame", {"title": t})

    def test_plan_parent_merge_routes(self):
        st, i, _ = self.req("POST", "/api/issues/MG-1/plan", {"steps": ["a", "b"], "verification": "shot", "actor": "claude"})
        self.assertEqual((st, i["plan_progress"]), (200, {"done": 0, "total": 2}))
        st, i, _ = self.req("PATCH", "/api/issues/MG-1/plan/steps/2", {"state": "done"})
        self.assertEqual((st, i["plan"]["steps"][1]["state"], i["activity"][-1]["actor"]), (200, "done", "owner"))
        self.assertEqual(self.req("PATCH", "/api/issues/MG-1/plan/steps/9", {"state": "done"})[0], 400)
        st, i, _ = self.req("POST", "/api/issues/MG-1/parent", {"parent": "MG-3"})
        self.assertEqual((st, i["parent"]), (200, "MG-3"))
        st, res, _ = self.req("POST", "/api/issues/MG-1/merge", {"sources": ["MG-2"]})
        self.assertEqual((st, res["merged"]), (200, ["MG-2"]))
        st, i, _ = self.req("GET", "/api/issues/MG-2")
        self.assertEqual((i["id"], i["redirected_from"]), ("MG-1", "MG-2"))
        self.assertEqual(self.req("POST", "/api/issues/MG-3/merge", {"sources": ["MG-2"]})[0], 409)  # already merged
        self.assertEqual(self.req("POST", "/api/issues/MG-2/merge", {"sources": ["MG-1"]})[0], 400)  # MG-2 is MG-1 now
        st, res, _ = self.req("POST", "/api/issues/MG-2/unmerge", {})
        self.assertEqual((st, res["issue"]["status"]), (200, "reported"))
        st, res, _ = self.req("GET", "/api/projects/mygame/suggest-groups")
        self.assertEqual(res["groups"][0]["issues"], ["MG-1", "MG-2"])

    def test_handoff_and_notify_routes(self):
        st, h, _ = self.req("POST", "/api/projects/mygame/handoff", {"markdown": "## State\n\nok\n", "author": "owner"})
        self.assertEqual((st, h["version"]), (200, 1))
        st, h, _ = self.req("POST", "/api/projects/mygame/handoff", {"section": "Traps", "text": "none", "author": "claude"})
        self.assertEqual((h["version"], h["sections"]["Traps"]), (2, "none"))
        self.assertEqual(self.req("GET", "/api/projects/mygame/handoff?version=1")[1]["version"], 1)
        self.assertEqual(len(self.req("GET", "/api/projects/mygame/handoff/history")[1]["versions"]), 2)
        self.assertIn("+none", self.req("GET", "/api/projects/mygame/handoff/diff?from=1")[1]["diff"])
        st, p, _ = self.req("PATCH", "/api/projects/mygame", {"notify": {"reports": False}})
        self.assertEqual((st, p["notify"]["reports"]), (200, False))


class SseTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.store.create_project("mygame", "MyGame", "MG")
        self.store.create_issue("mygame", {"title": "x"})

    def open_stream(self, path="/api/projects/mygame/events"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertTrue(res.getheader("Content-Type").startswith("text/event-stream"))
        events: queue.Queue = queue.Queue()

        def reader():
            ev = {}
            try:
                for raw in res:
                    line = raw.decode("utf-8").rstrip("\n")
                    if not line:
                        if ev:
                            events.put(ev)
                        ev = {}
                    elif line.startswith("event: "):
                        ev["event"] = line[7:]
                    elif line.startswith("data: "):
                        ev["data"] = json.loads(line[6:])
            except (OSError, ValueError):
                pass

        threading.Thread(target=reader, daemon=True).start()
        self.addCleanup(conn.close)
        return events

    def next_event(self, events, name, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev = events.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                break
            if ev.get("event") == name:
                return ev
        self.fail(f"no {name} event within {timeout}s")

    def test_stream_delivers_writes_from_any_process(self):
        events = self.open_stream()
        self.next_event(events, "hello")
        # a write through the HTTP API
        self.req("POST", "/api/issues/MG-1/comments", {"author": "owner", "text": "hi"})
        ev = self.next_event(events, "change")
        self.assertIn(("MG-1", "comment", "owner"), [(c["issue"], c["type"], c["actor"]) for c in ev["data"]["changes"]])
        # a write from another connection, as the CLI or the MCP server makes it
        with Store(self.tmp) as other:
            other.set_plan("MG-1", ["a"], actor="claude")
        ev = self.next_event(events, "change")
        self.assertEqual(ev["data"]["changes"][0]["action"], "plan")
        # a commit without a feed row (a deletion) asks the page to reload
        with Store(self.tmp) as other:
            other.create_project("other", "Other", "OT")
        self.next_event(events, "refresh")

    def test_unknown_project_is_404(self):
        self.assertEqual(self.req("GET", "/api/projects/nope/events")[0], 404)

    def test_shared_stream_receives_each_project_and_project_stream_stays_scoped(self):
        self.store.create_project("other", "Other", "OT")
        self.store.create_issue("other", {"title": "Other issue"})
        all_events = self.open_stream("/api/events")
        project_events = self.open_stream()
        self.assertIsNone(self.next_event(all_events, "hello")["data"]["project"])
        self.next_event(project_events, "hello")
        self.req("POST", "/api/issues/OT-1/comments", {"text": "other change"})
        # The watcher may still publish fixture writes after the hello cursor;
        # wait for the requested write, rather than assuming it is the first batch.
        deadline = time.time() + 5
        while True:
            change = self.next_event(all_events, "change", max(0.05, deadline - time.time()))
            if any(c.get("issue") == "OT-1" and c["type"] == "comment" for c in change["data"]["changes"]):
                break
        self.assertEqual({c["project"] for c in change["data"]["changes"]}, {"other"})
        self.req("POST", "/api/issues/MG-1/comments", {"text": "mygame change"})
        own = self.next_event(project_events, "change")
        self.assertEqual({c["project"] for c in own["data"]["changes"]}, {"mygame"})
        own_shared = self.next_event(all_events, "change")
        self.assertEqual({c["project"] for c in own_shared["data"]["changes"]}, {"mygame"})


class McpPd1Tests(McpCase):
    def setUp(self):
        os.environ["PAIR_DESK_CHANNEL"] = "off"
        self.addCleanup(os.environ.pop, "PAIR_DESK_CHANNEL", None)
        super().setUp()

    def test_plan_progress_merge_handoff_tools(self):
        init = rpc(self.proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertIn("claude/channel", init["result"]["capabilities"]["experimental"])
        err, i = self.call(2, "create_issue", {"title": "Lava tips", "kind": "task", "size": "M", "milestone": "W1"})
        self.assertFalse(err, i)
        self.assertEqual((i["size"], i["milestone"]), ("M", "W1"))
        err, plan = self.call(3, "set_plan", {"id": "MG-1", "steps": ["round", "clamp"], "verification": "shot 12"})
        self.assertEqual((plan["progress"], plan["steps"][0]["n"]), ("0/2", 1))
        err, msg = self.call(4, "set_status", {"id": "MG-1", "status": "to_check"})
        self.assertTrue(err)
        self.assertIn("open plan steps (1, 2)", msg)
        err, plan = self.call(5, "update_step", {"id": "MG-1", "index": 1, "state": "done", "commit": "abc"})
        self.assertEqual(plan["progress"], "1/2")
        err, res = self.call(6, "progress", {"id": "MG-1", "step": 2, "state": "dropped", "text": "not needed"})
        # the first step done started the work; the plan is finished, but without a location command it is not
        # handed to the owner
        self.assertEqual(res["ok"], "MG-1 updated (auto_check, plan 1/1)")
        err, msg = self.call(7, "set_status", {"id": "MG-1", "status": "to_check"})
        self.assertTrue(err)
        self.assertIn("no location command", msg)
        err, loc = self.call(7, "set_location", {"id": "MG-1", "location": {"command": "/goto 1 2", "seed": 1234}})
        self.assertFalse(err, loc)
        self.assertEqual(loc["location"]["command"], "/goto 1 2")
        err, i = self.call(7, "set_status", {"id": "MG-1", "status": "to_check"})
        self.assertFalse(err, i)
        self.assertEqual(i["plan"], "1/1")
        self.call(8, "create_issue", {"title": "Lava tips square", "kind": "bug"})
        err, sug = self.call(9, "suggest_groups", {})
        self.assertEqual(sug["groups"][0]["issues"], ["MG-1", "MG-2"])
        err, m = self.call(10, "merge_issues", {"target": "MG-1", "sources": ["MG-2"]})
        self.assertEqual(m["merged"], ["MG-2"])
        err, g = self.call(11, "get_issue", {"id": "MG-2"})
        self.assertEqual((g["id"], g["redirected_from"]), ("MG-1", "MG-2"))
        err, u = self.call(12, "unmerge", {"id": "MG-2"})
        self.assertEqual(u["issue"]["id"], "MG-2")
        err, lp = self.call(13, "link_parent", {"child": "MG-2", "parent": "MG-1"})
        self.assertEqual(lp["parent"], "MG-1")
        err, h = self.call(14, "update_handoff", {"section": "next", "text": "UI checks"})
        self.assertEqual(h["version"], 1)
        err, h = self.call(15, "get_handoff", {})
        self.assertIn("## Next step\n\nUI checks", h["markdown"])
        err, h = self.call(16, "set_handoff", {"markdown": "## State\n\nall new"})
        self.assertEqual(h["version"], 2)


class ChannelTests(TempDirCase):
    """The MCP server pushes owner activity into the session as channel notifications."""

    def test_owner_comment_is_pushed(self):
        with Store(self.tmp) as s:
            s.create_project("mygame", "MyGame", "MG")
            s.create_issue("mygame", {"title": "Lava", "kind": "check", "status": "to_check", "source": "agent"})
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / ".pair-desk.json").write_text('{"project": "mygame"}', encoding="utf-8")
        env = dict(os.environ, PAIR_DESK_CHANNEL="on", PAIR_DESK_PORT="1")  # no desk server: poll the database
        env.pop("CLAUDE_PROJECT_DIR", None)
        proc = subprocess.Popen([sys.executable, DESK, "--data", str(self.tmp), "mcp"], cwd=repo, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [lines.put(json.loads(l)) for l in proc.stdout], daemon=True).start()
        try:
            proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                             b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()
            self.assertEqual(lines.get(timeout=10)["id"], 1)
            # wait for the channel to start (its heartbeat file appears)
            end = time.time() + 20
            while time.time() < end and not list((self.tmp / "channels").glob("*.json")):
                time.sleep(0.1)
            self.assertTrue(list((self.tmp / "channels").glob("*.json")), "channel did not start")
            with Store(self.tmp) as s:
                s.add_comment("MG-1", "claude", "agent chatter is not pushed")
                s.add_comment("MG-1", "owner", "The east flow is still square")
            msg = lines.get(timeout=15)
            self.assertEqual(msg["method"], "notifications/claude/channel")
            self.assertEqual(msg["params"]["meta"], {"issue": "MG-1", "event": "comment", "project": "mygame"})
            self.assertIn("The east flow is still square", msg["params"]["content"])
        finally:
            proc.stdin.close()
            proc.wait(timeout=15)
            proc.stdout.close()
        self.assertEqual(list((self.tmp / "channels").glob("*.json")), [])  # heartbeat removed on exit


class HookTests(StoreCase):
    def hook(self, event, session="s1", env=None):
        payload = {"cwd": str(self.repo), "session_id": session, "prompt": "go"}
        out = subprocess.run([sys.executable, DESK, "--data", str(self.tmp), "hook", event],
                             input=json.dumps(payload).encode(), capture_output=True, timeout=30,
                             env=dict(os.environ, **(env or {})))
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if out.stdout.strip() else None

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "game"
        self.repo.mkdir()
        save_link(self.tmp, self.repo, "mygame")
        self.store.create_issue("mygame", {"title": "Lava", "kind": "check", "status": "to_check", "source": "agent"})

    def test_prompt_hook_lists_owner_activity_once(self):
        start = self.hook("session-start")
        self.assertIn("1 waiting for the owner", start["systemMessage"])
        self.assertIn("none written yet", start["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("user-prompt-submit"))
        self.store.add_comment("MG-1", "owner", "The east flow is still square")
        self.store.add_comment("MG-1", "claude", "agent note")
        self.store.create_issue("mygame", {"title": "Boat sinks", "source": "game"})
        out = self.hook("user-prompt-submit")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        lines = ctx.splitlines()
        self.assertTrue(lines[0].startswith("Pair Desk (MyGame): owner activity"))
        self.assertIn("MG-2 new report from game", lines[1])
        self.assertIn('MG-1 owner commented', lines[2])
        self.assertNotIn("agent note", ctx)
        self.assertIsNone(self.hook("user-prompt-submit"))  # the cursor moved
        # another session that never looked starts from the project cursor
        self.store.add_comment("MG-1", "owner", "", verdict="failed")
        self.assertIn("STILL BROKEN", self.hook("user-prompt-submit", session="s2")["hookSpecificOutput"]["additionalContext"])
        self.assertIn("STILL BROKEN", self.hook("user-prompt-submit", session="s1")["hookSpecificOutput"]["additionalContext"])

    def test_settings_limit_and_channel_silence(self):
        self.hook("session-start")
        self.store.update_project("mygame", {"notify": {"comments": False}})
        self.store.add_comment("MG-1", "owner", "muted")
        self.assertIsNone(self.hook("user-prompt-submit"))
        for n in range(20):
            self.store.create_issue("mygame", {"title": f"report {n}", "source": "game"})
        ctx = self.hook("user-prompt-submit")["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx.splitlines()), notify.MAX_LINES)
        self.assertIn("more", ctx.splitlines()[-1])
        # a live channel for this Claude process (the test process is the hook's parent) keeps it quiet
        notify.write_heartbeat(self.tmp, os.getpid(), "mygame")
        self.store.create_issue("mygame", {"title": "pushed instead", "source": "game"})
        self.assertIsNone(self.hook("user-prompt-submit"))
        notify.remove_heartbeat(self.tmp, os.getpid())

    def test_session_start_shows_handoff(self):
        self.store.set_handoff("mygame", "## Next step\n\nWire the UI\n\n## Traps\n\nworktree dirty\n", author="claude")
        ctx = self.hook("session-start")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("handoff v1", ctx)
        self.assertIn("- Next step: Wire the UI", ctx)
        self.assertIn("- Traps: worktree dirty", ctx)

    def test_silent_without_link(self):
        out = subprocess.run([sys.executable, DESK, "--data", str(self.tmp), "hook", "user-prompt-submit"],
                             input=b'{"cwd": "C:/nowhere", "session_id": "x"}', capture_output=True, timeout=20)
        self.assertEqual((out.returncode, out.stdout), (0, b""))


class NotifyUnitTests(unittest.TestCase):
    def test_channel_flag_detection(self):
        f = notify.cmdline_enables_channel
        self.assertTrue(f("claude --dangerously-load-development-channels plugin:agent-pair-programming@agent-pair-programming"))
        self.assertTrue(f('claude.exe --channels=plugin:agent-bridge@agent-bridge,plugin:agent-pair-programming@x'))
        self.assertTrue(f("claude --channels plugin:a@a plugin:agent-pair-programming@b --resume"))
        self.assertFalse(f("claude --dangerously-load-development-channels plugin:agent-bridge@agent-bridge"))
        self.assertFalse(f("claude --resume agent-pair-programming"))

    def test_format(self):
        e = {"issue": "MG-4", "type": "verdict", "verdict": "failed", "actor": "owner", "text": "x " * 200,
             "at": "2026-09-29T12:00:00.000Z"}
        line = notify.format_event(e)
        self.assertTrue(line.startswith("MG-4 owner: STILL BROKEN"))
        self.assertLess(len(line), 200)
        msg = notify.channel_message(e, "mygame")
        self.assertIn("Full text", msg["content"])


class CliPd1Tests(StoreCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--data", str(self.tmp), *argv])
        return code, buf.getvalue()

    def test_commands(self):
        self.run_cli("add", "--project", "mygame", "--title", "Lava tips", "--size", "m", "--milestone", "W1")
        self.run_cli("add", "--project", "mygame", "--title", "Lava tips square", "--parent", "MG-1")
        steps = self.tmp / "plan.md"
        steps.write_text("- [x] Round the tips\n- [ ] Clamp\n3. Stage shot\n", encoding="utf-8")
        code, out = self.run_cli("plan", "MG-1", "--steps-file", str(steps), "--verification", "shot 12")
        self.assertIn("Plan 1/3", out)
        code, out = self.run_cli("step", "MG-1", "2", "done", "--commit", "abc")
        self.assertIn("step 2: done (2/3 done)", out)
        code, out = self.run_cli("show", "MG-1")
        self.assertIn("Children 0/1 done", out)
        self.assertIn("[x] Clamp  (abc)", out)
        code, out = self.run_cli("parent", "MG-2", "--none")
        self.assertIn("has no parent", out)
        code, out = self.run_cli("suggest-groups", "--project", "mygame")
        self.assertIn("into MG-1", out)
        code, out = self.run_cli("merge", "MG-1", "MG-2")
        self.assertIn("Merged MG-2 into MG-1", out)
        code, out = self.run_cli("unmerge", "MG-2")
        self.assertIn("Unmerged MG-2", out)
        code, out = self.run_cli("list", "--project", "mygame", "--sort", "backlog", "--size", "M")
        self.assertIn("[plan 2/3]", out)
        code, out = self.run_cli("handoff", "section", "--project", "mygame", "--section", "Next step", "--text", "UI")
        self.assertIn("v1", out)
        code, out = self.run_cli("handoff", "--project", "mygame")
        self.assertIn("## Next step\n\nUI", out)
        code, out = self.run_cli("project-set", "--project", "mygame", "--notify", "verdicts,reports")
        self.assertIn("notify: verdicts, reports", out)


if __name__ == "__main__":
    unittest.main()
