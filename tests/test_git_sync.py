"""PD-16: opt-in synchronization, using disposable desks and bare remotes only."""
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pair_desk.cli import build_parser, main
from pair_desk import sync, sync_data
from pair_desk.store import Conflict, DeskError, Store
from pair_desk.sync_git import GitBranch, PushRejected, SyncError


NAME = "Example"
EMAIL = "example@example.test"


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pairdesk-sync-test-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "shared.git"
        self.git("init", "--bare", "--template=", str(self.remote))
        self.stores = []
        self.a = self.desk("a", create=True)
        self.ma = sync.SyncManager(self.a)

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_AUTHOR_NAME=NAME, GIT_AUTHOR_EMAIL=EMAIL,
                   GIT_COMMITTER_NAME=NAME, GIT_COMMITTER_EMAIL=EMAIL,
                   GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        proc = subprocess.Popen(["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
            raise
        self.assertEqual(proc.returncode, 0, err.decode(errors="replace"))
        return out

    def desk(self, name, create=False):
        store = Store(self.root / name)
        self.stores.append(store)
        if create:
            store.create_project("mygame", "MyGame", "MG")
        return store

    def enable(self, manager):
        return manager.enable("mygame", str(self.remote), NAME, EMAIL)

    def pair(self):
        self.enable(self.ma)
        self.b = self.desk("b")
        self.mb = sync.SyncManager(self.b)
        self.enable(self.mb)
        return self.b, self.mb

    def issue(self, store, title="A test issue"):
        return store.create_issue("mygame", {"title": title, "kind": "bug", "status": "open"})

    def test_two_desks_third_join_distinct_ranges_and_code_untouched(self):
        code = self.root / "code"
        self.git("init", "--template=", str(code))
        (code / "source.txt").write_text("initial")
        self.git("add", "source.txt", cwd=code)
        self.git("commit", "-m", "initial", cwd=code)
        (code / "source.txt").write_text("staged")
        self.git("add", "source.txt", cwd=code)
        (code / "source.txt").write_text("dirty")
        before = (self.git("status", "--porcelain=v1", cwd=code), (code / ".git/index").read_bytes(),
                  self.git("rev-parse", "HEAD", cwd=code))
        first = self.issue(self.a)
        self.ma.enable("mygame", str(self.remote), NAME, EMAIL, repo=code)
        self.pair()
        ia, ib = self.issue(self.a, "from a"), self.issue(self.b, "from b")
        self.assertNotEqual(ia["id"], ib["id"])
        self.ma.run("mygame")
        self.mb.run("mygame")
        self.ma.run("mygame")
        third = self.desk("c")
        mc = sync.SyncManager(third)
        self.enable(mc)
        self.assertEqual({i["id"] for i in third.list_issues("mygame")["issues"]}, {first["id"], ia["id"], ib["id"]})
        self.assertNotIn(self.issue(third, "from c")["id"], {ia["id"], ib["id"]})
        after = (self.git("status", "--porcelain=v1", cwd=code), (code / ".git/index").read_bytes(),
                 self.git("rev-parse", "HEAD", cwd=code))
        self.assertEqual(before, after)

    def test_offline_reconnect_and_exhaustion(self):
        self.enable(self.ma)
        moved = self.root / "offline.git"
        self.remote.rename(moved)
        item = self.issue(self.a, "offline")
        with self.assertRaises(SyncError):
            self.ma.run("mygame")
        self.assertEqual(self.ma.status()[0]["status"], "error")
        self.assertEqual(self.a.get_issue(item["id"])["title"], "offline")
        with self.a._tx() as c:
            c.execute("UPDATE projects SET next_number=(SELECT range_end+1 FROM sync_projects WHERE project_id=projects.id)")
        with self.assertRaises(DeskError):
            self.issue(self.a, "exhausted")
        moved.rename(self.remote)
        self.assertEqual(self.ma.run("mygame")["status"], "synced")
        self.assertGreater(int(self.issue(self.a, "replenished")["id"].split("-")[1]), 1000)

    def test_distinct_fields_and_append_comments_merge(self):
        item = self.issue(self.a)
        self.pair()
        self.a.update_issue(item["id"], {"title": "changed title"})
        self.b.update_issue(item["id"], {"body": "changed body"})
        self.a.add_comment(item["id"], "left", "first")
        self.b.add_comment(item["id"], "right", "second")
        self.ma.run("mygame")
        self.assertEqual(self.mb.run("mygame")["status"], "synced")
        self.ma.run("mygame")
        result = self.a.get_issue(item["id"])
        self.assertEqual((result["title"], result["body"]), ("changed title", "changed body"))
        self.assertEqual({c["text"] for c in result["comments"]}, {"first", "second"})

    def test_conflict_stale_resolution_and_explicit_choice(self):
        item = self.issue(self.a)
        self.pair()
        self.a.update_issue(item["id"], {"title": "left"})
        self.b.update_issue(item["id"], {"title": "right"})
        self.ma.run("mygame")
        result = self.mb.run("mygame")
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflicts"][0]["path"][-1], "title")
        self.b.update_issue(item["id"], {"body": "intervening"})
        with self.assertRaises(SyncError):
            self.mb.resolve("mygame", "remote")
        self.assertEqual(self.mb.run("mygame")["status"], "conflict")
        self.assertEqual(self.mb.resolve("mygame", "remote")["status"], "pending")
        self.assertEqual(self.b.get_issue(item["id"])["title"], "left")
        self.assertEqual(self.mb.run("mygame")["status"], "synced")
        self.ma.run("mygame")
        self.assertEqual(self.a.get_issue(item["id"])["body"], "intervening")

    def test_noop_uses_head_without_payload(self):
        self.enable(self.ma)
        with patch.object(GitBranch, "read", side_effect=AssertionError("payload read")), \
                patch.object(GitBranch, "publish", side_effect=AssertionError("payload publish")):
            self.assertEqual(self.ma.run("mygame")["status"], "synced")

    def test_push_rejection_refetches_and_is_bounded(self):
        self.enable(self.ma)
        self.issue(self.a)
        original = GitBranch.publish
        calls = []
        def reject_once(branch, files, parent):
            calls.append(parent)
            if len(calls) == 1:
                raise PushRejected("simulated contention")
            return original(branch, files, parent)
        with patch.object(GitBranch, "publish", reject_once):
            self.assertEqual(self.ma.run("mygame")["status"], "synced")
        self.assertEqual(len(calls), 2)
        self.issue(self.a, "another")
        with patch.object(GitBranch, "publish", side_effect=PushRejected("contention")) as publish:
            with self.assertRaises(SyncError):
                self.ma.run("mygame")
        self.assertEqual(publish.call_count, 3)
        self.assertTrue(self.ma.status()[0]["pending"])

    def test_crash_after_push_local_write_and_ack_recovery(self):
        self.enable(self.ma)
        item = self.issue(self.a)
        original = GitBranch.publish
        def publish_and_write(branch, files, parent):
            result = original(branch, files, parent)
            self.a.update_issue(item["id"], {"body": "written during Git I/O"})
            return result
        with patch.object(GitBranch, "publish", publish_and_write):
            self.assertEqual(self.ma.run("mygame")["status"], "local-changed")
        self.assertTrue(self.ma.status()[0]["pending"])
        self.assertEqual(self.ma.run("mygame")["status"], "synced")
        self.assertFalse(self.ma.status()[0]["pending"])
        b = self.desk("b")
        self.enable(sync.SyncManager(b))
        self.assertEqual(b.get_issue(item["id"])["body"], "written during Git I/O")

    def test_bootstrap_crash_recovers_published_range(self):
        with patch.object(sync_data, "apply_snapshot", side_effect=Conflict("simulated post-push interruption")):
            self.assertEqual(self.enable(self.ma)["status"], "local-changed")
        row = self.ma._row("mygame")
        pending = json.loads(row["pending"])
        with self.assertRaises(DeskError):
            self.issue(self.a, "blocked before range install")
        self.assertEqual(self.ma.run("mygame")["status"], "synced")
        rows = self.a._read("SELECT range_start,range_end FROM sync_projects")
        self.assertEqual(tuple(rows[0]), (1, 1000))
        self.assertEqual(self.ma._row("mygame")["sequence"], pending["sequence"])

    def test_disable_no_network_reenable_does_not_rewind(self):
        self.enable(self.ma)
        first = self.issue(self.a)
        self.ma.disable("mygame")
        with patch.object(GitBranch, "read", side_effect=AssertionError("disabled network")), \
                patch.object(GitBranch, "head", side_effect=AssertionError("disabled network")):
            self.assertEqual(self.ma.run("mygame")["status"], "disabled")
            self.assertIsNone(sync.schedule_once(self.a.data_dir))
            sync.run_enabled(self.a.data_dir)
        second = self.issue(self.a)
        self.enable(self.ma)
        third = self.issue(self.a)
        self.assertLess(int(first["id"].split("-")[1]), int(second["id"].split("-")[1]))
        self.assertLess(int(second["id"].split("-")[1]), int(third["id"].split("-")[1]))

    def test_disabled_device_keeps_own_range_after_importing_peer_numbers(self):
        self.pair()
        peer_first = self.issue(self.b, "peer first")
        self.assertEqual(peer_first["id"], "MG-1001")
        self.mb.run("mygame")
        self.ma.run("mygame")
        before = tuple(self.a._read("SELECT p.next_number,s.shared,s.range_start,s.range_end "
                                    "FROM projects p JOIN sync_projects s ON s.project_id=p.id")[0])
        self.ma.disable("mygame")
        after = tuple(self.a._read("SELECT p.next_number,s.shared,s.range_start,s.range_end "
                                   "FROM projects p JOIN sync_projects s ON s.project_id=p.id")[0])
        self.assertEqual(after, before)
        with patch.object(GitBranch, "read", side_effect=AssertionError("disabled network")), \
                patch.object(GitBranch, "head", side_effect=AssertionError("disabled network")):
            self.assertEqual(self.ma.run("mygame")["status"], "disabled")
            local = self.issue(self.a, "disabled local")
        peer_second = self.issue(self.b, "peer second")
        self.assertEqual(local["id"], "MG-1")
        self.assertEqual(peer_second["id"], "MG-1002")
        self.assertEqual(self.enable(self.ma)["status"], "synced")
        self.assertEqual(self.mb.run("mygame")["status"], "synced")
        self.assertEqual(self.ma.run("mygame")["status"], "synced")
        for store in (self.a, self.b):
            self.assertEqual({i["id"]: i["title"] for i in store.list_issues("mygame")["issues"]},
                             {local["id"]: "disabled local", peer_first["id"]: "peer first",
                              peer_second["id"]: "peer second"})

    def test_disabled_exhaustion_blocks_until_online_reenable(self):
        self.pair()
        self.ma.disable("mygame")
        with self.a._tx() as c:
            c.execute("UPDATE projects SET next_number=1001 WHERE slug='mygame'")
        with self.assertRaises(DeskError):
            self.issue(self.a, "must not use peer range")
        peer = self.issue(self.b, "peer owns 1001")
        self.assertEqual(peer["id"], "MG-1001")
        moved = self.root / "offline.git"
        self.remote.rename(moved)
        with self.assertRaises(SyncError):
            self.enable(self.ma)
        with self.assertRaises(DeskError):
            self.issue(self.a, "still exhausted offline")
        moved.rename(self.remote)
        self.assertEqual(self.enable(self.ma)["status"], "synced")
        self.assertEqual(self.issue(self.a, "fresh reservation")["id"], "MG-2001")

    def test_disabled_copied_binding_blocks_first_cli_create(self):
        self.enable(self.ma)
        self.ma.disable("mygame")
        b = self.desk("b")
        self.a.conn.backup(b.conn)
        with patch.object(sync, "schedule_once", return_value=None), \
                patch("sys.stderr", io.StringIO()), redirect_stdout(io.StringIO()):
            result = main(["--data", str(b.data_dir), "add", "--project", "mygame", "--title", "unsafe clone",
                           "--kind", "bug", "--status", "open", "--json"])
        self.assertEqual(result, 1)
        self.assertEqual(b.list_issues("mygame")["issues"], [])
        self.assertEqual(sync.SyncManager(b).status()[0]["status"], "rebind-required")

    def test_copied_database_requires_rebind_new_device_and_range(self):
        self.enable(self.ma)
        self.issue(self.a)
        original_device = self.ma._row("mygame")["device_uuid"]
        b = self.desk("b")
        self.a.conn.backup(b.conn)
        mb = sync.SyncManager(b)
        with patch.object(GitBranch, "read", side_effect=AssertionError("clone network")):
            self.assertEqual(mb.status()[0]["status"], "rebind-required")
        with self.assertRaises(DeskError):
            self.issue(b, "blocked clone")
        self.enable(mb)
        self.assertNotEqual(mb._row("mygame")["device_uuid"], original_device)
        self.assertGreater(int(self.issue(b, "new clone range")["id"].split("-")[1]), 1000)

    def test_malformed_manifest_or_state_rejected_atomically(self):
        self.enable(self.ma)
        before = sync_data.snapshot(self.a, "mygame")
        transport = self.ma._transport(self.ma._row("mygame"))
        head, files = transport.read()
        for mutate in (lambda m: m.update(next_number=True),
                       lambda m: m["ranges"].update({str(__import__('uuid').uuid4()): [[1, 1000]]}),
                       lambda m: m.update(project_uuid=str(__import__('uuid').uuid4()))):
            broken = dict(files)
            manifest = json.loads(files[sync.MANIFEST])
            mutate(manifest)
            broken[sync.MANIFEST] = json.dumps(manifest).encode()
            with patch.object(GitBranch, "head", return_value="changed"), patch.object(GitBranch, "read", return_value=(head, broken)):
                with self.assertRaises(SyncError):
                    self.ma.run("mygame")
            self.assertEqual(sync_data.snapshot(self.a, "mygame"), before)

    def test_cli_and_lifecycle_default_no_network(self):
        with patch.object(GitBranch, "read", side_effect=AssertionError("default network")), \
                patch.object(GitBranch, "head", side_effect=AssertionError("default network")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--data", str(self.a.data_dir), "sync", "status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), [])
            from pair_desk.mcp import McpServer
            mcp = McpServer(self.a.data_dir)
            mcp.serve(io.BytesIO(), io.BytesIO())
            self.assertFalse(mcp._sync_worker.thread.is_alive())
            from pair_desk.server import DeskServer
            server = DeskServer(("127.0.0.1", 0), self.a)
            server.server_close()
            self.assertFalse(server.sync_worker.thread.is_alive())

    def test_resolve_unrelated_title_retains_remote_historical_verdict(self):
        item = self.issue(self.a)
        self.pair()
        self.a.update_issue(item["id"], {"title": "left"})
        self.b.update_issue(item["id"], {"title": "right"})
        self.a.add_comment(item["id"], "owner", "historical owner approval", verdict="passed")
        self.ma.run("mygame")
        self.assertEqual(self.mb.run("mygame")["status"], "conflict")
        self.mb.resolve("mygame", "local")
        result = self.b.get_issue(item["id"])
        self.assertEqual(result["title"], "right")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["comments"][0]["verdict"], "passed")

    def test_resolve_explicit_status_cannot_promote_passed(self):
        item = self.issue(self.a)
        self.pair()
        self.a.set_status(item["id"], "passed", actor="owner")
        self.b.set_status(item["id"], "failed", actor="owner")
        self.ma.run("mygame")
        self.assertEqual(self.mb.run("mygame")["status"], "conflict")
        with self.assertRaisesRegex(SyncError, "cannot manufacture|Structural conflict"):
            self.mb.resolve("mygame", "remote")
        self.assertEqual(self.b.get_issue(item["id"])["status"], "failed")

    def _resolve_lifecycle_with_independent_fields(self, take):
        item = self.issue(self.a)
        self.pair()
        self.a.update_issue(item["id"], {"title": "local title"})
        self.a.set_status(item["id"], "closed")
        local_closed_at = self.a.get_issue(item["id"])["closed_at"]
        self.a.add_comment(item["id"], "local", "local history")
        self.b.update_issue(item["id"], {"body": "remote body"})
        self.b.set_status(item["id"], "in_progress")
        self.b.add_comment(item["id"], "remote", "remote history")
        self.mb.run("mygame")
        result = self.ma.run("mygame")
        self.assertEqual(result["status"], "conflict")
        self.assertEqual({c["path"][-1] for c in result["conflicts"]}, {"status", "closed_at"})
        self.assertTrue(all(c["kind"] == "lifecycle" for c in result["conflicts"]))
        self.assertEqual(self.ma.resolve("mygame", take)["status"], "pending")
        chosen = ("closed", local_closed_at) if take == "local" else ("in_progress", None)
        resolved = self.a.get_issue(item["id"])
        self.assertEqual((resolved["title"], resolved["body"]), ("local title", "remote body"))
        self.assertEqual((resolved["status"], resolved["closed_at"]), chosen)
        self.assertEqual(self.ma.run("mygame")["status"], "synced")
        self.assertEqual(self.mb.run("mygame")["status"], "synced")
        for store in (self.a, self.b):
            resolved = store.get_issue(item["id"])
            self.assertEqual((resolved["title"], resolved["body"]), ("local title", "remote body"))
            self.assertEqual((resolved["status"], resolved["closed_at"]), chosen)
            self.assertEqual({c["text"] for c in resolved["comments"]}, {"local history", "remote history"})

    def test_lifecycle_take_local_keeps_local_title_and_remote_body(self):
        self._resolve_lifecycle_with_independent_fields("local")

    def test_lifecycle_take_remote_keeps_local_title_and_remote_body(self):
        self._resolve_lifecycle_with_independent_fields("remote")

    def test_structural_resolution_keeps_all_records_and_does_not_publish(self):
        item = self.issue(self.a)
        parent = self.issue(self.a, "parent")
        self.pair()
        self.a.link_parent(item["id"], parent["id"])
        self.b.add_comment(item["id"], "local", "must be retained")
        self.ma.run("mygame")
        result = self.mb.run("mygame")
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(c["path"] == ["records"] for c in result["conflicts"]))
        before = sync_data.snapshot(self.b, "mygame")
        stored = self.mb._row("mygame")["conflicts"]
        with patch.object(GitBranch, "publish", side_effect=AssertionError("structural publication")):
            with self.assertRaisesRegex(SyncError, "Structural conflict"):
                self.mb.resolve("mygame", "remote")
        self.assertEqual(sync_data.snapshot(self.b, "mygame"), before)
        self.assertEqual(self.mb._row("mygame")["conflicts"], stored)
        # Repair the structural change at its origin; a run must notice the new
        # remote head even though this desk's captured local state is unchanged.
        self.a.link_parent(item["id"], None)
        self.ma.run("mygame")
        self.assertEqual(self.mb.run("mygame")["status"], "synced")
        self.assertIn("must be retained", [c["text"] for c in self.b.get_issue(item["id"])["comments"]])

    def test_copied_database_first_cli_write_is_blocked_before_rebind(self):
        self.enable(self.ma)
        b = self.desk("b")
        self.a.conn.backup(b.conn)
        with patch.object(sync, "schedule_once", return_value=None), patch("sys.stderr", io.StringIO()):
            result = main(["--data", str(b.data_dir), "add", "--project", "mygame", "--title", "unsafe clone",
                           "--kind", "bug", "--status", "open", "--json"])
        self.assertEqual(result, 1)
        self.assertEqual(b.list_issues("mygame")["issues"], [])
        self.assertEqual(sync.SyncManager(b).status()[0]["status"], "rebind-required")

    def test_cli_enable_run_disable_and_missing_enable_options(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--data", str(self.a.data_dir), "sync", "enable", "--project", "mygame",
                                   "--remote", str(self.remote), "--author-name", NAME, "--author-email", EMAIL]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "synced")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data", str(self.a.data_dir), "sync", "run", "--project", "mygame"]), 0)
            self.assertEqual(main(["--data", str(self.a.data_dir), "sync", "disable", "--project", "mygame"]), 0)
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["--data", str(self.a.data_dir), "sync", "enable", "--project", "mygame"]), 1)

    def test_automatic_worker_observes_independent_store_write_and_stops(self):
        self.enable(self.ma)
        other = self.desk("a")
        worker = sync.SyncWorker(self.a.data_dir, interval=0.02)
        published = threading.Event()
        original = GitBranch.publish
        def observe(branch, files, parent):
            result = original(branch, files, parent)
            published.set()
            return result
        try:
            with patch.object(GitBranch, "publish", observe):
                item = self.issue(other, "independent CLI/UI writer")
                self.assertTrue(published.wait(15))
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())
        third = self.desk("c")
        self.enable(sync.SyncManager(third))
        self.assertEqual(third.get_issue(item["id"])["title"], "independent CLI/UI writer")

    def test_scheduler_throttle_and_lock_do_not_launch_flood(self):
        self.enable(self.ma)
        fake = unittest.mock.Mock(pid=123456)
        fake.wait.return_value = 0
        with patch.object(sync.subprocess, "Popen", return_value=fake) as spawn:
            with sync._file_lock(self.a.data_dir, "schedule.lock") as held:
                self.assertTrue(held)
                self.assertIsNone(sync.schedule_once(self.a.data_dir))
            self.assertEqual(sync.schedule_once(self.a.data_dir), fake.pid)
            self.assertIsNone(sync.schedule_once(self.a.data_dir))
            self.assertEqual(spawn.call_count, 1)
        entries = (self.a.data_dir / "sync/children.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(entries[0])["pid"], fake.pid)

    def test_cli_write_after_checkpoint_before_unlock_gets_successor_cycle(self):
        self.enable(self.ma)
        self.issue(self.a, "first write")
        original_lock = sync.cycle_lock
        scheduled = []
        spawns = []
        injected = False
        fake = unittest.mock.Mock(pid=123456)
        fake.wait.return_value = 0

        @contextmanager
        def write_at_unlock(data_dir):
            nonlocal injected
            with original_lock(data_dir) as acquired:
                try:
                    yield acquired
                finally:
                    # The checkpoint already committed and returned status='synced'.
                    # The final CLI write lands before the cycle releases its lock.
                    if acquired and not injected:
                        injected = True
                        with Store(data_dir) as writer:
                            self.issue(writer, "last CLI write")
                        with patch.object(sync.subprocess, "Popen", return_value=fake) as spawn:
                            scheduled.append(sync.schedule_once(data_dir))
                            spawns.append(spawn.call_count)

        with patch.object(sync, "cycle_lock", write_at_unlock):
            self.assertFalse(sync.run_enabled(self.a.data_dir))
        self.assertEqual(scheduled, [fake.pid])
        self.assertEqual(spawns, [1])
        event = unittest.mock.Mock()
        event.wait.return_value = False
        event.is_set.return_value = False
        with patch.object(sync.threading, "Event", return_value=event), patch.object(sync.threading, "Timer"):
            sync._once(self.a.data_dir)
        self.assertEqual(event.wait.call_args_list[0], unittest.mock.call(sync.INTERVAL))
        third = self.desk("c")
        self.enable(sync.SyncManager(third))
        self.assertEqual({i["title"] for i in third.list_issues("mygame")["issues"]},
                         {"first write", "last CLI write"})

    def test_lock_is_nonblocking_across_processes(self):
        code = """import os,sys
f=open(sys.argv[1],'a+b');f.seek(0)
try:
 if os.name=='nt':
  import msvcrt
  msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
 else:
  import fcntl
  fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
except OSError:
 print('busy')
else:
 print('acquired')
finally:
 f.close()
"""
        with sync.cycle_lock(self.a.data_dir) as held:
            self.assertTrue(held)
            proc = subprocess.Popen([os.sys.executable, "-c", code, str(self.a.data_dir / "sync/cycle.lock")],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(out.strip(), b"busy")

    def test_coalesced_one_shot_retries_local_changes_without_daemon(self):
        event = unittest.mock.Mock()
        event.wait.return_value = False
        with patch.object(sync.threading, "Event", return_value=event), \
                patch.object(sync.threading, "Timer") as timer, \
                patch.object(sync, "run_enabled", side_effect=[True, False]) as run:
            sync._once(self.a.data_dir)
        self.assertEqual(event.wait.call_args_list[0], unittest.mock.call(sync.INTERVAL))
        self.assertEqual(run.call_count, 2)
        timer.return_value.cancel.assert_called_once()

    def test_remote_state_validation_precedes_fresh_project_creation(self):
        self.enable(self.ma)
        transport = self.ma._transport(self.ma._row("mygame"))
        head, files = transport.read()
        malformed = dict(files)
        doc = json.loads(files[sync.STATE])
        doc["complete"] = False
        malformed[sync.STATE] = json.dumps(doc).encode()
        b = self.desk("b")
        with patch.object(GitBranch, "read", return_value=(head, malformed)):
            with self.assertRaises(DeskError):
                self.enable(sync.SyncManager(b))
        self.assertEqual(b.list_projects(), [])
        self.assertEqual(b._read("SELECT * FROM git_sync"), [])

    def test_all_git_io_occurs_outside_sqlite_transaction(self):
        original_read, original_publish = GitBranch.read, GitBranch.publish
        def read(branch):
            self.assertFalse(self.a.conn.in_transaction)
            return original_read(branch)
        def publish(branch, files, parent):
            self.assertFalse(self.a.conn.in_transaction)
            row = self.ma._row("mygame")
            self.assertIsNotNone(row["pending"])
            return original_publish(branch, files, parent)
        with patch.object(GitBranch, "read", read), patch.object(GitBranch, "publish", publish):
            self.assertEqual(self.enable(self.ma)["status"], "synced")

    def test_attachment_roundtrip_and_commands_are_history_only(self):
        from helpers import PNG
        item = self.issue(self.a)
        self.a.add_attachment(item["id"], "proof.png", "image/png", PNG)
        self.a.queue_command("mygame", "/goto synthetic", issue=item["id"], actor="owner")
        self.pair()
        result = self.b.get_issue(item["id"])
        self.assertEqual(self.b.get_attachment(result["attachments"][0]["id"])[1].read_bytes(), PNG)
        self.assertEqual(self.b.list_commands("mygame"), [])
        self.assertTrue(any(a["action"] == "command_sent" for a in result["activity"]))

    def test_whole_folder_binding_also_checks_host_identity(self):
        self.enable(self.ma)
        with patch.object(sync.socket, "gethostname", return_value="different-synthetic-host"):
            self.assertEqual(self.ma.status()[0]["status"], "rebind-required")
        with self.assertRaises(DeskError):
            self.issue(self.a, "host clone blocked")

    def test_bad_slug_cannot_escape_managed_cache_directory(self):
        with patch.object(GitBranch, "read", side_effect=AssertionError("unsafe cache path")):
            with self.assertRaisesRegex(SyncError, "slug"):
                self.ma.enable("../../outside", str(self.remote), NAME, EMAIL)


class SyncCapabilityTests(unittest.TestCase):
    def test_explicit_opt_in_command(self):
        args = build_parser().parse_args([
            "sync", "enable", "--project", "mygame", "--remote", "example.git",
            "--author-name", "Example", "--author-email", "example@example.test",
        ])
        self.assertEqual(args.cmd, "sync")
        self.assertEqual(args.action, "enable")


if __name__ == "__main__":
    unittest.main()
