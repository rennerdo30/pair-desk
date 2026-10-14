"""The current build: publishing one stamps every to_check issue; an issue reaching to_check gets the current one."""
import contextlib
import io
import json
import sqlite3
import unittest

from helpers import StoreCase, TempDirCase
from test_api import ApiCase
from test_mcp_cli import McpCase

from pair_desk.cli import build_lines, main
from pair_desk.context import save_link
from pair_desk.store import Invalid, Store, default_build_label, handover_problem

EXE = "D:/builds/mygame/MyGame.exe"
CMD = "/goto 1240 -380"


class StoreBuildTests(StoreCase):
    def check(self, title="c", **fields):
        return self.store.create_issue("mygame", {"title": title, "kind": "check", "status": "to_check",
                                                  "source": "agent", "command": CMD, **fields})

    def test_projects_without_builds_behave_as_before(self):
        p = self.store.get_project("mygame")
        self.assertEqual((p["build"], p["builds_enabled"]), (None, False))
        i = self.check()
        self.assertIsNone(i["build"])
        self.assertEqual(i["activity"][0]["detail"], {"status": "to_check"})
        self.assertIsNone(handover_problem(i, p))

    def test_publishing_stamps_every_to_check_issue_once(self):
        self.check("a")
        self.check("b")
        self.store.create_issue("mygame", {"title": "open one", "command": CMD})
        res = self.store.set_build("mygame", EXE, commit="a2d78b49", actor="claude")
        self.assertEqual(res["stamped"], ["MG-1", "MG-2"])
        b = res["build"]
        self.assertEqual((b["number"], b["label"], b["path"], b["commit"], b["set_by"]), (1, "a2d78b49", EXE, "a2d78b49", "claude"))
        i = self.store.get_issue("MG-1")
        self.assertEqual(i["build"], {"number": 1, "label": "a2d78b49", "path": EXE, "commit": "a2d78b49",
                                      "built_at": b["built_at"]})
        self.assertEqual([a["action"] for a in i["activity"]], ["created", "build"])
        self.assertEqual(i["activity"][-1]["detail"]["label"], "a2d78b49")
        self.assertEqual(i["comment_count"], 0)
        self.assertIsNone(self.store.get_issue("MG-3")["build"])
        # The same build again stamps nothing and logs nothing.
        again = self.store.set_build("mygame", EXE, commit="a2d78b49")
        self.assertEqual((again["unchanged"], again["stamped"], again["build"]["number"]), (True, [], 1))
        self.assertEqual(len(self.store.get_issue("MG-1")["activity"]), 2)
        # A new build restamps, one compact entry per issue naming the build it replaced.
        self.clock.advance(hours=1)
        res = self.store.set_build("mygame", EXE, commit="b7c1d2e3", label="nightly 12", built_at="2026-09-29T20:00:00Z")
        self.assertEqual((res["build"]["number"], res["build"]["label"], res["build"]["built_at"]),
                         (2, "nightly 12", "2026-09-29T20:00:00.000Z"))
        i = self.store.get_issue("MG-2")
        self.assertEqual(i["build"]["label"], "nightly 12")
        self.assertEqual(i["activity"][-1]["detail"]["previous"], "a2d78b49")
        self.assertEqual(i["updated_at"], self.store.now())
        self.assertEqual(self.store.get_project("mygame")["build"]["label"], "nightly 12")

    def test_reaching_to_check_takes_the_current_build(self):
        self.store.set_build("mygame", "0.9.3")
        i = self.check()
        self.assertEqual(i["build"]["label"], "0.9.3")
        self.assertEqual(i["activity"][0]["detail"], {"status": "to_check", "build": "0.9.3"})
        self.store.create_issue("mygame", {"title": "bug", "command": CMD})
        i = self.store.set_status("MG-2", "to_check", actor="claude")
        self.assertEqual(i["build"]["path"], "0.9.3")
        self.assertEqual(i["activity"][-1]["detail"], {"from": "reported", "to": "to_check", "build": "0.9.3"})
        # The stamp stays when the owner fails it: it says which build it failed in.
        self.store.add_comment("MG-2", "owner", "still broken", "failed")
        self.store.set_build("mygame", "0.9.4")
        self.assertEqual(self.store.get_issue("MG-2")["build"]["label"], "0.9.3")
        # The plan's last step queues automatic verification; explicit handover takes the current build.
        self.store.set_plan("MG-2", ["rework"])
        i = self.store.update_step("MG-2", 1, "done", commit="abc123")
        self.assertEqual((i["status"], i["build"]["label"]), ("auto_check", "0.9.3"))
        i = self.store.set_status("MG-2", "to_check", actor="claude")
        self.assertEqual(i["build"]["label"], "0.9.4")

    def test_a_cleared_build_blocks_the_handover(self):
        self.store.set_build("mygame", EXE, commit="a2d78b49")
        self.store.create_issue("mygame", {"title": "bug", "command": CMD})
        p = self.store.clear_build("mygame")
        self.assertEqual((p["build"], p["builds_enabled"]), (None, True))
        issue = self.store.get_issue("MG-1", full=False)
        self.assertIn("no current build", handover_problem(issue, self.store.get_project("mygame")))
        # The plan finishing queues automatic verification without handing it to the owner.
        self.store.set_plan("MG-1", ["fix"])
        self.assertEqual(self.store.update_step("MG-1", 1, "done")["status"], "auto_check")
        res = self.store.set_build("mygame", EXE, commit="c0ffee00")
        self.assertEqual((res["build"]["number"], res["stamped"]), (2, []))
        self.assertIsNone(handover_problem(self.store.get_issue("MG-1", full=False), self.store.get_project("mygame")))
        # Off: the project stops using builds.
        p = self.store.clear_build("mygame", off=True)
        self.assertEqual((p["build"], p["builds_enabled"]), (None, False))
        self.assertIsNone(handover_problem(issue, self.store.get_project("mygame")))

    def test_validation_and_labels(self):
        with self.assertRaises(Invalid):
            self.store.set_build("mygame", "  ")
        with self.assertRaises(Invalid):
            self.store.set_build("mygame", EXE, built_at="yesterday")
        with self.assertRaises(Invalid):
            self.store.set_build("mygame", EXE, label="x" * 81)
        self.assertEqual(default_build_label("1.4.0-rc2", "", 3), "1.4.0-rc2")
        self.assertEqual(default_build_label("C:\\builds\\game", "0123456789abcdef", 3), "0123456789ab")
        self.assertEqual(default_build_label(EXE, "", 3), "build 3")
        self.assertIsNone(self.store.get_project("mygame")["build"])

    def test_merged_issues_are_not_stamped(self):
        self.check("a")
        self.check("b")
        self.store.merge_issues("MG-1", ["MG-2"])
        self.assertEqual(self.store.set_build("mygame", "0.9.3")["stamped"], ["MG-1"])


class BuildMigrationTests(TempDirCase):
    def test_a_version_4_desk_gains_builds_off(self):
        with Store(self.tmp) as s:
            s.create_project("mygame", "MyGame", "MG")
            s.create_issue("mygame", {"title": "a", "status": "to_check", "command": CMD})
        conn = sqlite3.connect(self.tmp / "desk.sqlite")
        conn.execute("ALTER TABLE projects DROP COLUMN build")
        conn.execute("ALTER TABLE issues DROP COLUMN build")
        conn.execute("UPDATE meta SET value='4' WHERE key='schema'")
        conn.commit()
        conn.close()
        with Store(self.tmp) as s:
            self.assertEqual(s._read("SELECT value FROM meta WHERE key='schema'")[0]["value"], "6")
            self.assertEqual((s.get_project("mygame")["builds_enabled"], s.get_issue("MG-1")["build"]), (False, None))
            self.assertEqual(s.set_build("mygame", "0.1")["stamped"], ["MG-1"])


class CliBuildTests(StoreCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--data", str(self.tmp), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_set_show_clear_and_the_status_check(self):
        self.store.create_issue("mygame", {"title": "a", "status": "to_check", "command": CMD})
        code, out = self.run_cli("build", "--project", "mygame")
        self.assertIn("does not use builds", out)
        code, out = self.run_cli("build", "set", "--project", "mygame", "--path", EXE, "--commit", "a2d78b49",
                                 "--label", "rc1", "--author", "claude")
        self.assertEqual(code, 0)
        self.assertIn("Stamped on 1 to_check issue: MG-1", out)
        code, out = self.run_cli("build", "show", "--project", "mygame", "--json")
        self.assertEqual(json.loads(out)["build"]["label"], "rc1")
        code, out = self.run_cli("show", "MG-1")
        self.assertIn(f"build: rc1  {EXE}  (commit a2d78b49", out)
        self.store.create_issue("mygame", {"title": "b", "command": CMD})
        code, out = self.run_cli("status", "MG-2", "to_check", "--author", "claude")
        self.assertEqual((code, out.strip()), (0, "MG-2 is now to_check (build rc1)"))
        self.run_cli("status", "MG-2", "in_progress", "--author", "claude")
        code, out = self.run_cli("build", "clear", "--project", "mygame")
        self.assertIn("no current build", out)
        code, out = self.run_cli("status", "MG-2", "to_check", "--author", "claude")
        self.assertEqual(code, 2)
        self.assertIn("no current build", out)
        self.assertEqual(self.run_cli("build", "set", "--project", "mygame")[0], 1)
        code, out = self.run_cli("build", "clear", "--off", "--project", "mygame")
        self.assertIn("does not use builds", out)
        self.assertEqual(self.run_cli("status", "MG-2", "to_check", "--author", "claude")[0], 0)

    def test_session_start_names_the_build(self):
        repo = self.tmp / "game"
        repo.mkdir()
        save_link(self.tmp, repo, "mygame")
        self.assertEqual(build_lines(self.tmp, repo), [])
        self.store.set_build("mygame", EXE, commit="a2d78b49")
        self.assertIn(f"a2d78b49  {EXE}", build_lines(self.tmp, repo)[0])
        self.store.clear_build("mygame")
        self.assertIn("none current", build_lines(self.tmp, repo)[0])


class McpBuildTests(McpCase):
    def test_set_build_and_where_it_shows(self):
        err, created = self.call(1, "create_issue", {"title": "Lava tips", "command": CMD})
        self.assertNotIn("build", created)
        err, res = self.call(2, "set_build", {"path": EXE, "commit": "a2d78b49"})
        self.assertFalse(err, res)
        self.assertEqual((res["stamped"], res["build"]["label"], res["build"]["set_by"]), (["MG-1"], "a2d78b49", "claude"))
        err, listing = self.call(3, "list_issues", {})
        self.assertEqual(listing["issues"][0]["build"], "a2d78b49")
        err, projects = self.call(4, "list_projects", {})
        self.assertEqual(projects["projects"][0]["build"]["path"], EXE)
        err, handoff = self.call(5, "get_handoff", {})
        self.assertEqual(handoff["build"]["commit"], "a2d78b49")
        err, msg = self.call(6, "set_build", {})
        self.assertTrue(err)

    def test_set_status_needs_a_current_build_once_builds_are_used(self):
        with Store(self.tmp) as s:
            s.create_issue("mygame", {"title": "bug", "command": CMD})
            s.set_build("mygame", "0.9.3")
            s.clear_build("mygame")
        err, msg = self.call(1, "set_status", {"id": "MG-1", "status": "to_check"})
        self.assertTrue(err)
        self.assertIn("no current build", msg)
        self.call(2, "set_build", {"path": "0.9.4"})
        err, res = self.call(3, "set_status", {"id": "MG-1", "status": "to_check"})
        self.assertFalse(err, res)
        self.assertEqual((res["status"], res["build"]), ("to_check", "0.9.4"))


class ApiBuildTests(ApiCase):
    def test_build_routes(self):
        self.req("POST", "/api/projects", {"slug": "mygame", "name": "MyGame", "prefix": "MG"})
        self.req("POST", "/api/projects/mygame/issues", {"title": "x", "status": "to_check", "command": CMD})
        status, res, _ = self.req("GET", "/api/projects/mygame/build")
        self.assertEqual((status, res["build"], res["builds_enabled"]), (200, None, False))
        status, res, _ = self.req("POST", "/api/projects/mygame/build", {"path": EXE, "commit": "a2d78b49", "author": "ci"})
        self.assertEqual((status, res["stamped"], res["build"]["set_by"]), (200, ["MG-1"], "ci"))
        status, p, _ = self.req("GET", "/api/projects/mygame")
        self.assertEqual(p["build"]["path"], EXE)
        status, issue, _ = self.req("GET", "/api/issues/MG-1")
        self.assertEqual(issue["build"]["commit"], "a2d78b49")
        status, res, _ = self.req("POST", "/api/projects/mygame/build", {"commit": "x"})
        self.assertEqual(status, 400)
        status, res, _ = self.req("DELETE", "/api/projects/mygame/build")
        self.assertEqual((res["build"], res["builds_enabled"]), (None, True))
        status, res, _ = self.req("DELETE", "/api/projects/mygame/build?off=1")
        self.assertFalse(res["builds_enabled"])


if __name__ == "__main__":
    unittest.main()
