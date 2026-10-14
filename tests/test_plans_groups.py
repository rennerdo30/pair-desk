"""Plans, parent/child groups, merges, grouping proposals, the backlog fields and the handoff (store)."""

import json
import sqlite3
import unittest

from helpers import PNG_B64, StoreCase, TempDirCase

from pair_desk.store import Conflict, Invalid, NotFound, Store, handover_problem


class PlanTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.create_issue("mygame", {"title": "Lava tips", "kind": "task", "status": "in_progress"})

    def test_plan_steps_move_the_status(self):
        s = self.store
        s.create_issue("mygame", {"title": "Rain drips through roofs", "status": "failed",
                                  "location": {"command": "/goto 12 -40 yaw 90"}})
        s.set_plan("MG-2", ["Find the leak", "Seal the eaves"], actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "failed")  # writing a plan is not work yet
        s.update_step("MG-2", 1, "doing", actor="claude")
        i = s.get_issue("MG-2")
        self.assertEqual(i["status"], "in_progress")
        st = [a for a in i["activity"] if a["action"] == "status"][-1]
        self.assertEqual((st["detail"]["from"], st["detail"]["to"], st["detail"]["reason"]),
                         ("failed", "in_progress", "a plan step started"))
        s.update_step("MG-2", 1, "done", commit="abc1234", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "in_progress")
        s.update_step("MG-2", 2, "done", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "auto_check")
        # ticking an already finished plan again leaves the waiting issue alone
        s.update_step("MG-2", 2, "done", commit="def5678", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "auto_check")
        # the owner fails it; the agent starts a new step: back to in_progress, and auto_check when done
        s.set_status("MG-2", "failed")
        s.set_plan("MG-2", ["Find the leak", "Seal the eaves", "Flash the chimney"], actor="claude")
        s.update_step("MG-2", 3, "doing", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "in_progress")
        s.update_step("MG-2", 3, "done", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "auto_check")

    def test_a_finished_plan_without_a_location_enters_auto_check(self):
        s = self.store
        s.create_issue("mygame", {"title": "Somewhere", "status": "open"})
        s.set_plan("MG-2", ["One"], actor="claude")
        s.update_step("MG-2", 1, "done", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "auto_check")
        self.assertIn("no location command", handover_problem(s.get_issue("MG-2")))
        s.update_issue("MG-2", {"location": {"command": "/goto 5 5"}})
        self.assertIsNone(handover_problem(s.get_issue("MG-2")))

    def test_parked_and_closed_issues_do_not_move(self):
        s = self.store
        s.create_issue("mygame", {"title": "Someday", "status": "parked"})
        s.set_plan("MG-2", ["One"], actor="claude")
        s.update_step("MG-2", 1, "done", actor="claude")
        self.assertEqual(s.get_issue("MG-2")["status"], "parked")

    def test_set_plan_and_progress(self):
        i = self.store.set_plan("MG-1", ["Round the tips", {"text": "Clamp to ground", "state": "doing"}, "Stage shot"],
                                "stage shot 12 shows round tips", actor="claude")
        self.assertEqual([st["state"] for st in i["plan"]["steps"]], ["todo", "doing", "todo"])
        self.assertEqual(i["plan"]["verification"], "stage shot 12 shows round tips")
        self.assertEqual(i["plan_progress"], {"done": 0, "total": 3})
        act = i["activity"][-1]
        self.assertEqual((act["action"], act["actor"], act["detail"]["op"]), ("plan", "claude", "set"))
        listed = self.store.list_issues("mygame")["issues"][0]
        self.assertEqual(listed["plan_progress"], {"done": 0, "total": 3})

    def test_update_step_states_commit_note(self):
        self.store.set_plan("MG-1", ["a", "b", "c"])
        i = self.store.update_step("MG-1", 1, state="done", commit="5ea28d6", actor="claude")
        self.assertEqual(i["plan"]["steps"][0], {"text": "a", "state": "done", "commit": "5ea28d6"})
        i = self.store.update_step("MG-1", 3, state="dropped", note="not needed after all")
        self.assertEqual(i["plan_progress"], {"done": 1, "total": 2})  # dropped does not count
        i = self.store.update_step("MG-1", 2, note="owner: check the east flow", actor="owner")
        d = i["activity"][-1]["detail"]
        self.assertEqual((d["op"], d["index"], d["note"]), ("step", 2, "owner: check the east flow"))
        self.assertNotIn("to", d)
        i = self.store.update_step("MG-1", 2, note="")
        self.assertNotIn("note", i["plan"]["steps"][1])
        n = len(i["activity"])
        self.assertEqual(len(self.store.update_step("MG-1", 1, state="done")["activity"]), n)  # no-op, no entry

    def test_replan_keeps_matching_steps(self):
        self.store.set_plan("MG-1", ["a", "b"])
        self.store.update_step("MG-1", 1, state="done", commit="abc")
        i = self.store.set_plan("MG-1", ["a", "b2", "b"])
        self.assertEqual(i["plan"]["steps"][0], {"text": "a", "state": "done", "commit": "abc"})
        self.assertEqual(i["plan"]["verification"], "")
        self.assertEqual(i["activity"][-1]["detail"]["replaced"], 2)

    def test_invalid(self):
        with self.assertRaises(Invalid):
            self.store.update_step("MG-1", 1, state="done")  # no plan yet
        self.store.set_plan("MG-1", ["a"])
        for bad in (0, 2, "x", True):
            with self.assertRaises(Invalid):
                self.store.update_step("MG-1", bad, state="done")
        with self.assertRaises(Invalid):
            self.store.update_step("MG-1", 1, state="finished")
        with self.assertRaises(Invalid):
            self.store.update_step("MG-1", 1)
        with self.assertRaises(Invalid):
            self.store.set_plan("MG-1", "not a list")
        with self.assertRaises(Invalid):
            self.store.set_plan("MG-1", [""])
        with self.assertRaises(Invalid):
            self.store.set_plan("MG-1", ["x"] * 101)


class ParentTests(StoreCase):
    def setUp(self):
        super().setUp()
        for t in ("Water epic", "Shore foam", "Chunk seams", "Unrelated"):
            self.store.create_issue("mygame", {"title": t})

    def test_link_children_and_progress(self):
        self.store.link_parent("MG-2", "MG-1", actor="owner")
        c = self.store.link_parent("MG-3", "MG-1")
        self.assertEqual(c["parent"], "MG-1")
        self.assertEqual(c["parent_info"]["id"], "MG-1")
        p = self.store.get_issue("MG-1")
        self.assertEqual([ch["id"] for ch in p["children"]], ["MG-2", "MG-3"])
        self.assertEqual((p["child_count"], p["child_done"]), (2, 0))
        self.assertEqual([a["detail"] for a in p["activity"] if a["action"] == "child"],
                         [{"op": "added", "child": "MG-2"}, {"op": "added", "child": "MG-3"}])
        self.store.set_status("MG-2", "passed")
        self.store.set_status("MG-3", "closed")
        row = next(i for i in self.store.list_issues("mygame")["issues"] if i["id"] == "MG-1")
        self.assertEqual((row["child_count"], row["child_done"]), (2, 2))
        u = self.store.link_parent("MG-3", None)
        self.assertIsNone(u["parent"])
        self.assertEqual(u["activity"][-1]["detail"], {"parent": None, "previous": "MG-1"})

    def test_refuses_cycles_self_and_other_projects(self):
        self.store.link_parent("MG-2", "MG-1")
        self.store.link_parent("MG-3", "MG-2")
        with self.assertRaises(Invalid):
            self.store.link_parent("MG-1", "MG-3")
        with self.assertRaises(Invalid):
            self.store.link_parent("MG-1", "MG-1")
        self.store.create_project("other", "Other", "OT")
        self.store.create_issue("other", {"title": "x"})
        with self.assertRaises(Invalid):
            self.store.link_parent("MG-4", "OT-1")
        with self.assertRaises(NotFound):
            self.store.link_parent("MG-4", "MG-99")

    def test_delete_parent_unlinks_children(self):
        self.store.link_parent("MG-2", "MG-1")
        self.store.delete_issue("MG-1")
        self.assertIsNone(self.store.get_issue("MG-2")["parent"])


class MergeTests(StoreCase):
    def setUp(self):
        super().setUp()
        s = self.store
        s.create_issue("mygame", {"title": "Boat sinks at the harbor", "body": "first", "source": "game",
                                    "command": "/goto 1 2 3"})
        self.clock.advance(minutes=5)
        s.create_issue("mygame", {"title": "Boat sinks in harbour", "body": "second report", "status": "failed",
                                    "location": {"command": "/goto 1 2 4", "place": "Harbor"},
                                    "attachments": [{"filename": "a.png", "data_base64": PNG_B64}]})
        s.add_comment("MG-2", "owner", "still sinking")
        s.create_issue("mygame", {"title": "child of 2"})
        s.link_parent("MG-3", "MG-2")
        self.clock.advance(minutes=5)

    def test_merge_moves_timeline_and_redirects(self):
        res = self.store.merge_issues("MG-1", ["MG-2"], actor="owner")
        self.assertEqual((res["target"], res["merged"]), ("MG-1", ["MG-2"]))
        t = res["issue"]
        self.assertEqual([(c["text"], c["merged_from"]) for c in t["comments"]], [("still sinking", "MG-2")])
        self.assertEqual([(a["filename"], a["merged_from"]) for a in t["attachments"]], [("a.png", "MG-2")])
        moved = [a for a in t["activity"] if a["merged_from"] == "MG-2"]
        self.assertEqual(moved[0]["action"], "created")
        card = next(a for a in t["activity"] if a["action"] == "merged")
        self.assertEqual((card["detail"]["from"], card["detail"]["body"], card["detail"]["status"]),
                         ("MG-2", "second report", "failed"))
        self.assertEqual(card["detail"]["location"]["place"], "Harbor")
        self.assertEqual([m["id"] for m in t["merged_sources"]], ["MG-2"])
        self.assertEqual([c["id"] for c in t["children"]], ["MG-3"])  # children follow the merge
        src = self.store.get_issue("MG-2", follow=False)
        self.assertEqual((src["status"], src["merged_into"]), ("closed", "MG-1"))
        self.assertEqual(src["comments"], [])
        self.assertEqual(src["activity"][-1]["action"], "merged_into")
        red = self.store.get_issue("MG-2")
        self.assertEqual((red["id"], red["redirected_from"]), ("MG-1", "MG-2"))
        # writes to the old id land on the target
        r = self.store.add_comment("MG-2", "claude", "fixed in abc")
        self.assertEqual(r["issue"]["id"], "MG-1")
        self.assertEqual(self.store.update_issue("mg-2", {"priority": "p0"})["id"], "MG-1")

    def test_merged_filter_lists_merged_issues(self):
        self.assertEqual(self.store.list_issues("mygame", {})["counts"]["merged"], 0)
        self.store.merge_issues("MG-1", ["MG-2"])
        res = self.store.list_issues("mygame", {"merged": "1"})
        self.assertEqual([(i["id"], i["merged_into"]) for i in res["issues"]], [("MG-2", "MG-1")])
        self.assertEqual(res["counts"]["merged"], 1)
        # merged issues are closed, so the count ignores the status filter (the default view hides closed ones)
        self.assertEqual(self.store.list_issues("mygame", {"status": "open,reported"})["counts"]["merged"], 1)
        self.assertEqual(self.store.list_issues("mygame", {"kind": "task"})["counts"]["merged"], 0)

    def test_unmerge_restores(self):
        self.store.merge_issues("MG-1", ["MG-2"])
        self.store.add_comment("MG-1", "owner", "after the merge")
        res = self.store.unmerge("MG-2", actor="owner")
        src = res["issue"]
        self.assertEqual((src["status"], src["merged_into"]), ("failed", None))
        self.assertEqual([c["text"] for c in src["comments"]], ["still sinking"])
        self.assertEqual([a["filename"] for a in src["attachments"]], ["a.png"])
        self.assertEqual(src["activity"][0]["action"], "created")
        self.assertIsNone(src["activity"][0]["merged_from"])
        self.assertEqual([c["id"] for c in src["children"]], ["MG-3"])
        t = self.store.get_issue("MG-1")
        self.assertEqual([c["text"] for c in t["comments"]], ["after the merge"])
        self.assertEqual(t["merged_sources"], [])
        self.assertEqual(t["activity"][-1]["action"], "unmerged")
        with self.assertRaises(Invalid):
            self.store.unmerge("MG-2")

    def test_nested_merge_flattens(self):
        self.store.create_issue("mygame", {"title": "third copy"})
        self.store.add_comment("MG-4", "owner", "from four")
        self.store.merge_issues("MG-2", ["MG-4"])
        self.store.merge_issues("MG-1", ["MG-2"])
        self.assertEqual(self.store.get_issue("MG-4", follow=False)["merged_into"], "MG-1")
        t = self.store.get_issue("MG-1")
        self.assertEqual({c["text"]: c["merged_from"] for c in t["comments"]},
                         {"still sinking": "MG-2", "from four": "MG-4"})
        self.store.unmerge("MG-4")
        self.assertEqual([c["text"] for c in self.store.get_issue("MG-4")["comments"]], ["from four"])

    def test_merge_validation(self):
        with self.assertRaises(Invalid):
            self.store.merge_issues("MG-1", ["MG-1"])
        with self.assertRaises(Invalid):
            self.store.merge_issues("MG-1", [])
        self.store.merge_issues("MG-1", ["MG-2"])
        with self.assertRaises(Conflict):
            self.store.merge_issues("MG-3", ["MG-2"])
        with self.assertRaises(Conflict):
            self.store.delete_issue("MG-1")  # holds a merged issue
        self.store.create_project("other", "Other", "OT")
        self.store.create_issue("other", {"title": "x"})
        with self.assertRaises(Invalid):
            self.store.merge_issues("MG-1", ["OT-1"])

    def test_merge_target_that_is_a_child_of_the_source(self):
        res = self.store.merge_issues("MG-3", ["MG-2"])
        self.assertIsNone(res["issue"]["parent"])


class SuggestTests(StoreCase):
    def test_duplicates_and_related(self):
        s = self.store
        s.create_issue("mygame", {"title": "Lava flows end in square tips", "area": "terrain"})
        s.create_issue("mygame", {"title": "Square lava flow tips", "area": "terrain"})
        s.create_issue("mygame", {"title": "Fox clips through fences", "area": "creatures"})
        s.create_issue("mygame", {"title": "Water seam", "location": {"x": 100, "z": 200, "seed": 7}})
        s.create_issue("mygame", {"title": "Shore foam missing", "location": {"x": 110, "z": 190, "seed": 7}})
        s.create_issue("mygame", {"title": "Old lava tips", "status": "closed"})
        groups = s.suggest_groups("mygame")["groups"]
        self.assertEqual(groups[0]["issues"], ["MG-1", "MG-2"])
        self.assertEqual((groups[0]["action"], groups[0]["target"]), ("merge", "MG-1"))
        near = next(g for g in groups if "MG-4" in g["issues"])
        self.assertEqual(near["issues"], ["MG-4", "MG-5"])
        self.assertTrue(all("MG-3" not in g["issues"] and "MG-6" not in g["issues"] for g in groups))
        # proposals only: nothing changed
        self.assertIsNone(s.get_issue("MG-2")["merged_into"])


class BacklogTests(StoreCase):
    def test_size_milestone_parked(self):
        s = self.store
        s.create_issue("mygame", {"title": "a", "status": "open", "priority": "p1", "size": "l", "area": "water"})
        s.create_issue("mygame", {"title": "b", "status": "open", "priority": "p1", "size": "S", "area": "water"})
        s.create_issue("mygame", {"title": "c", "status": "in_progress", "priority": "p0", "area": "sky"})
        s.create_issue("mygame", {"title": "d", "status": "parked", "milestone": "World 2"})
        s.create_issue("mygame", {"title": "e", "status": "open", "priority": "p1", "size": "S", "area": "air"})
        res = s.list_issues("mygame", {"status": "open,in_progress", "sort": "backlog"})
        self.assertEqual([i["title"] for i in res["issues"]], ["c", "e", "b", "a"])
        self.assertEqual(res["counts"]["size"], {"L": 1, "S": 2})
        self.assertEqual(s.list_issues("mygame", {"size": "none"})["total"], 2)
        self.assertEqual(s.list_issues("mygame", {"milestone": "world 2"})["issues"][0]["status"], "parked")
        i = s.update_issue("MG-1", {"size": "", "milestone": "World 1"})
        self.assertEqual((i["size"], i["milestone"]), ("", "World 1"))
        with self.assertRaises(Invalid):
            s.create_issue("mygame", {"title": "x", "size": "XL"})
        self.assertIsNone(s.set_status("MG-4", "parked")["closed_at"])

    def test_import_takes_backlog_fields(self):
        r = self.store.import_data({"project": "mygame", "issues": [
            {"title": "t", "size": "m", "milestone": "Beta", "status": "parked", "external_ref": "TODO t"}]})
        i = self.store.get_issue(r["created"][0])
        self.assertEqual((i["size"], i["milestone"], i["status"]), ("M", "Beta", "parked"))


class HandoffTests(StoreCase):
    def test_versions_sections_and_diff(self):
        s = self.store
        self.assertEqual(s.get_handoff("mygame")["version"], 0)
        h = s.set_handoff("mygame", "## State\n\nall green\n\n## Next step\n\nlava\n", author="claude")
        self.assertEqual((h["version"], h["sections"]["Next step"], h["author"]), (1, "lava", "claude"))
        self.assertEqual(s.set_handoff("mygame", "## State\n\nall green\n\n## Next step\n\nlava")["version"], 1)
        h = s.update_handoff("mygame", "traps", "- the worktree is dirty", author="codex")
        self.assertEqual(h["version"], 2)
        self.assertEqual(list(h["sections"]), ["State", "Next step", "Traps"])
        h = s.update_handoff("mygame", "where work stopped", "store.py merge")
        self.assertEqual(list(h["sections"]), ["State", "Where work stopped", "Next step", "Traps"])
        h = s.update_handoff("mygame", "Next step", "UI")
        self.assertEqual(h["sections"]["Next step"], "UI")
        self.assertEqual(s.get_handoff("mygame", 1)["sections"]["Next step"], "lava")
        hist = s.handoff_history("mygame")["versions"]
        self.assertEqual([v["version"] for v in hist], [4, 3, 2, 1])
        self.assertEqual(hist[-1]["author"], "claude")
        diff = s.handoff_diff("mygame", 3)["diff"]
        self.assertIn("-lava", diff)
        self.assertIn("+UI", diff)
        with self.assertRaises(Invalid):
            s.set_handoff("mygame", "   ")
        with self.assertRaises(NotFound):
            s.get_handoff("mygame", 99)


class NotifyEventTests(StoreCase):
    def test_owner_events_kinds_and_settings(self):
        s = self.store
        s.create_issue("mygame", {"title": "agent check", "source": "agent", "kind": "check", "status": "to_check"})
        cursor = s.event_cursor()
        steps = [
            lambda: s.create_issue("mygame", {"title": "game report", "source": "game"}),
            lambda: s.add_comment("MG-1", "owner", "looks off"),
            lambda: s.add_comment("MG-1", "claude", "agent talk"),
            lambda: s.add_comment("MG-1", "owner", "", verdict="failed"),
            lambda: s.set_status("MG-2", "open", actor="owner"),
            lambda: s.set_status("MG-2", "in_progress", actor="claude"),
            lambda: s.set_plan("MG-1", ["a"], actor="claude"),
            lambda: s.update_step("MG-1", 1, note="please also check dusk", actor="owner"),
        ]
        for step in steps:
            self.clock.advance(seconds=1)
            step()
        res = s.owner_events("mygame", cursor)
        types = [e["type"] for e in res["events"]]
        self.assertEqual(types, ["plan", "status", "verdict", "comment", "report"])  # newest first
        self.assertEqual(res["events"][1]["to"], "open")
        self.assertEqual(s.owner_events("mygame", res["cursor"])["events"], [])
        self.assertEqual(s.owner_events("mygame", None)["events"], [])
        p = s.update_project("mygame", {"notify": {"comments": False, "status": False}})
        self.assertEqual(p["notify"], {"comments": False, "verdicts": True, "reports": True, "status": False})
        self.assertEqual([e["type"] for e in s.owner_events("mygame", cursor)["events"]], ["verdict", "report"])
        self.assertEqual(s.update_project("mygame", {"notify": "comments,reports"})["notify"],
                         {"comments": True, "verdicts": False, "reports": True, "status": False})
        with self.assertRaises(Invalid):
            s.update_project("mygame", {"notify": {"bogus": True}})

    def test_change_feed(self):
        s = self.store
        cur = s.event_cursor()
        s.create_issue("mygame", {"title": "x"})
        s.add_comment("MG-1", "owner", "hi")
        s.set_handoff("mygame", "## State\n\nok")
        feed = s.change_feed(cur)
        self.assertEqual({(c["type"], c["action"]) for c in feed["changes"]},
                         {("activity", "created"), ("comment", "comment"), ("handoff", "handoff")})
        self.assertEqual(s.change_feed(feed["cursor"])["changes"], [])


# The desk schema as version 2 wrote it (before plans, groups, merges, the backlog and the handoff).
V2_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    prefix TEXT NOT NULL UNIQUE, next_number INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, default_seed INTEGER);
CREATE TABLE issues (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), number INTEGER NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]', location TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL, external_ref TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    closed_at TEXT, UNIQUE (project_id, number));
CREATE TABLE comments (id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    author TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', verdict TEXT, created_at TEXT NOT NULL);
CREATE TABLE attachments (id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    comment_id INTEGER REFERENCES comments(id) ON DELETE SET NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
    size INTEGER NOT NULL, stored_path TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE activity (id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE commands (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
    issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL, command TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, delivered_at TEXT, client TEXT);
INSERT INTO meta VALUES ('schema', '2');
INSERT INTO projects VALUES (1, 'mygame', 'MyGame', 'MG', 3, '2026-09-01T00:00:00.000Z', 1234);
INSERT INTO issues VALUES (1, 1, 1, 'Old report', 'body', 'bug', 'failed', 'p1', 'water', '["sea"]',
    '{"command": "/goto 1 2 3", "seed": 1234}', 'game', '', '2026-09-01T00:00:00.000Z', '2026-09-02T00:00:00.000Z', NULL);
INSERT INTO issues VALUES (2, 1, 2, 'Old check', '', 'check', 'to_check', 'p2', '', '[]', '{}', 'agent', 'TODO x',
    '2026-09-01T00:00:00.000Z', '2026-09-01T00:00:00.000Z', NULL);
INSERT INTO comments VALUES (1, 1, 'owner', 'still broken', 'failed', '2026-09-02T00:00:00.000Z');
INSERT INTO activity VALUES (1, 1, 'game', 'created', '{"status": "reported"}', '2026-09-01T00:00:00.000Z');
INSERT INTO activity VALUES (2, 1, 'owner', 'status', '{"from": "reported", "to": "failed", "reason": "verdict"}', '2026-09-02T00:00:00.000Z');
"""


class MigrationTests(TempDirCase):
    def test_v2_desk_migrates_in_place(self):
        db = self.tmp / "desk.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript(V2_SCHEMA)
        conn.commit()
        conn.close()
        with Store(self.tmp) as s:
            i = s.get_issue("MG-1")
            self.assertEqual((i["title"], i["status"], i["tags"], i["location"]["seed"]),
                             ("Old report", "failed", ["sea"], 1234))
            self.assertEqual((i["size"], i["milestone"], i["plan_progress"], i["parent"]), ("", "", {"done": 0, "total": 0}, None))
            self.assertEqual(i["comments"][0]["verdict"], "failed")
            # Version 4: the single location command became a one-entry command list, stored.
            self.assertEqual(json.loads(s._read("SELECT location FROM issues WHERE number=1")[0]["location"]),
                             {"command": "/goto 1 2 3", "seed": 1234, "commands": [{"command": "/goto 1 2 3"}]})
            self.assertEqual(s._read("SELECT location FROM issues WHERE number=2")[0]["location"], "{}")
            self.assertEqual(s.get_project("mygame")["notify"]["comments"], True)
            s.set_plan("MG-2", ["a"])
            s.link_parent("MG-2", "MG-1")
            s.merge_issues("MG-1", ["MG-2"])
            s.set_handoff("mygame", "## State\n\nmigrated")
            self.assertEqual(s.create_issue("mygame", {"title": "new"})["id"], "MG-3")
        with Store(self.tmp) as s:  # opening again is a no-op
            self.assertEqual(s._read("SELECT value FROM meta WHERE key='schema'")[0]["value"], "6")
            self.assertEqual(s.get_handoff("mygame")["version"], 1)
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(issues)")}
        conn.close()
        self.assertTrue({"plan", "parent_id", "merged_into", "size", "milestone"} <= cols)

    def test_real_desk_copy_when_given(self):
        """Set PAIR_DESK_MIGRATION_SOURCE to a desk.sqlite to migrate a copy of it (the original
        is only read, through SQLite's backup API)."""
        import os
        src = os.environ.get("PAIR_DESK_MIGRATION_SOURCE")
        if not src:
            self.skipTest("PAIR_DESK_MIGRATION_SOURCE not set")
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as a, sqlite3.connect(self.tmp / "desk.sqlite") as b:
            a.backup(b)
            before = {t: a.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("projects", "issues", "comments", "attachments", "activity")}
        with Store(self.tmp) as s:
            after = {t: s._read(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in before}
            self.assertEqual(before, after)
            for p in s.list_projects():
                for i in s.list_issues(p["slug"], {"limit": 5000})["issues"]:
                    s.get_issue(i["id"])


if __name__ == "__main__":
    unittest.main()
