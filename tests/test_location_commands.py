"""Location commands: an issue lists one command per place, `location.command` mirrors the first."""
import contextlib
import io
import json
import sqlite3
import unittest

from helpers import StoreCase, TempDirCase
from test_api import ApiCase
from test_mcp_cli import McpCase

from pair_desk.cli import main
from pair_desk.store import (Invalid, Store, handover_problem, location_commands, merge_location,
                             normalize_commands, normalize_location)

CAPITAL = "/goto capital"
KEEP = "/goto dungeon ruined-keep"
GLACIER = "/goto biome glacier; /time 12:00"


class NormalizeTests(unittest.TestCase):
    def test_list_and_first_command_stay_in_sync(self):
        loc = normalize_location({"commands": [{"command": f" {CAPITAL} ", "label": " the  capital "},
                                               KEEP, {"command": ""}, {"command": CAPITAL}]})
        self.assertEqual(loc, {"command": CAPITAL, "commands": [{"command": CAPITAL, "label": "the capital"},
                                                                {"command": KEEP}]})
        self.assertEqual(normalize_location({"command": KEEP}), {"command": KEEP, "commands": [{"command": KEEP}]})
        self.assertEqual(normalize_location({"commands": []}), {})
        self.assertEqual(normalize_location({"commands": [{"command": "  "}], "place": "x"}), {"place": "x"})

    def test_a_lone_command_edits_the_first_entry(self):
        stored = normalize_location({"commands": [{"command": CAPITAL, "label": "the capital"}, {"command": KEEP}]})
        # A client that knows only `command` changed it: the first entry takes it and keeps its label.
        edited = normalize_location({**stored, "command": "/goto capital seed 4291"})
        self.assertEqual(edited["commands"], [{"command": "/goto capital seed 4291", "label": "the capital"},
                                              {"command": KEEP}])
        self.assertEqual(edited["command"], "/goto capital seed 4291")
        # A client that reordered the list and sent the old first command along: the list wins.
        reordered = normalize_location({"command": CAPITAL, "commands": [{"command": KEEP}, {"command": CAPITAL}]})
        self.assertEqual([c["command"] for c in reordered["commands"]], [KEEP, CAPITAL])
        self.assertEqual(reordered["command"], KEEP)

    def test_validation(self):
        with self.assertRaises(Invalid):
            normalize_commands(5)
        with self.assertRaises(Invalid):
            normalize_commands([{"command": "/goto 1 2", "where": "x"}])
        with self.assertRaises(Invalid):
            normalize_commands([{"command": "/goto 1 2", "label": "x" * 81}])
        with self.assertRaises(Invalid):
            normalize_commands([f"/goto {n} 0" for n in range(21)])
        with self.assertRaises(Invalid):
            normalize_commands([["/goto 1 2"]])

    def test_commands_an_older_desk_moved_into_extra_come_back(self):
        loc = normalize_location({"command": CAPITAL, "extra": {"biome": "x", "commands": [{"command": CAPITAL}, {"command": KEEP}]}})
        self.assertEqual(loc["commands"], [{"command": CAPITAL}, {"command": KEEP}])
        self.assertEqual(loc["extra"], {"biome": "x"})

    def test_seed_goes_on_every_command(self):
        loc = normalize_location({"seed": 4291, "commands": [{"command": CAPITAL, "label": "the capital"},
                                                            {"command": "/spawn kind canine 20 2"}, {"command": "/goto 1 2 seed 7"}]})
        self.assertEqual(location_commands(loc), [
            {"command": "/goto capital seed 4291", "label": "the capital"},
            {"command": "/spawn kind canine 20 2"}, {"command": "/goto 1 2 seed 7"}])
        self.assertEqual(location_commands(loc, seeded=False)[0]["command"], CAPITAL)
        self.assertEqual(location_commands({"command": "/goto 1 2", "seed": 5}), [{"command": "/goto 1 2 seed 5"}])
        self.assertEqual(location_commands({}), [])

    def test_merge_location(self):
        current = normalize_location({"place": "p", "commands": [CAPITAL, KEEP]})
        merged = normalize_location(merge_location(current, {"commands": [GLACIER]}))
        self.assertEqual((merged["command"], merged["commands"], merged["place"]), (GLACIER, [{"command": GLACIER}], "p"))
        merged = normalize_location(merge_location(current, {"command": GLACIER}))
        self.assertEqual([c["command"] for c in merged["commands"]], [GLACIER, KEEP])

    def test_handover_needs_at_least_one_command(self):
        plan = {"steps": [], "verification": ""}
        self.assertIn("no location command", handover_problem({"plan": plan, "location": {"place": "x"}}))
        self.assertIsNone(handover_problem({"plan": plan, "location": {"commands": [{"command": CAPITAL}]}}))
        self.assertIsNone(handover_problem({"plan": plan, "location": json.dumps({"command": CAPITAL})}))


class StoreCommandTests(StoreCase):
    def test_create_update_and_activity(self):
        i = self.store.create_issue("mygame", {"title": "keep", "commands": [
            {"command": CAPITAL, "label": "the capital"}, {"command": KEEP, "label": "the keep"}]})
        self.assertEqual(i["location"]["command"], CAPITAL)
        self.assertEqual([c["label"] for c in i["location"]["commands"]], ["the capital", "the keep"])
        # A bare list edit keeps the rest of the location.
        self.store.update_issue("MG-1", {"location": {**i["location"], "place": "Capital"}})
        i = self.store.update_issue("MG-1", {"commands": [{"command": KEEP}, {"command": GLACIER, "label": "ice"}]})
        self.assertEqual((i["location"]["command"], i["location"]["place"]), (KEEP, "Capital"))
        self.assertEqual(i["activity"][-1]["action"], "edited")
        # The single `command` addresses the first entry: replace it, or remove it with empty text.
        i = self.store.update_issue("MG-1", {"command": CAPITAL})
        self.assertEqual([c["command"] for c in i["location"]["commands"]], [CAPITAL, GLACIER])
        i = self.store.update_issue("MG-1", {"command": ""})
        self.assertEqual(i["location"]["commands"], [{"command": GLACIER, "label": "ice"}])
        self.assertEqual(i["location"]["command"], GLACIER)
        i = self.store.update_issue("MG-1", {"commands": []})
        self.assertNotIn("command", i["location"])
        self.assertNotIn("commands", i["location"])
        # Both, in sync (a client that sends the list and its first command): the list as given.
        i = self.store.update_issue("MG-1", {"commands": [KEEP, CAPITAL], "command": KEEP})
        self.assertEqual([c["command"] for c in i["location"]["commands"]], [KEEP, CAPITAL])

    def test_a_default_seed_applies_to_the_list(self):
        self.store.update_project("mygame", {"default_seed": 4291})
        i = self.store.create_issue("mygame", {"title": "x", "kind": "check", "source": "agent", "commands": [CAPITAL, KEEP]})
        self.assertEqual(i["location"]["seed"], 4291)
        self.assertEqual([c["command"] for c in location_commands(i["location"])],
                         ["/goto capital seed 4291", "/goto dungeon ruined-keep seed 4291"])

    def test_a_single_command_issue_reads_as_a_one_entry_list(self):
        self.store.create_issue("mygame", {"title": "x", "command": CAPITAL})
        # A row an older desk wrote: only `command`.
        with self.store._tx() as c:
            c.execute("UPDATE issues SET location=? WHERE number=1", (json.dumps({"command": KEEP, "seed": 3}),))
        loc = self.store.get_issue("MG-1")["location"]
        self.assertEqual(loc, {"command": KEEP, "commands": [{"command": KEEP}], "seed": 3})
        self.assertEqual(self.store.list_issues("mygame")["issues"][0]["location"]["commands"], [{"command": KEEP}])


class MigrationTests(TempDirCase):
    def test_version_3_rows_get_the_list_and_lose_nothing(self):
        with Store(self.tmp) as s:
            s.create_project("mygame", "MyGame", "MG")
            s.create_issue("mygame", {"title": "a", "command": CAPITAL, "location": {"place": "P", "seed": 9, "biome": "b"}})
            s.create_issue("mygame", {"title": "b"})
            s.create_issue("mygame", {"title": "c", "commands": [CAPITAL, KEEP]})
        conn = sqlite3.connect(self.tmp / "desk.sqlite")
        # Turn it back into a version 3 desk: locations with only `command`.
        conn.execute("UPDATE issues SET location=? WHERE number=1",
                     (json.dumps({"command": CAPITAL, "place": "P", "seed": 9, "extra": {"biome": "b"}}),))
        conn.execute("UPDATE meta SET value='3' WHERE key='schema'")
        conn.commit()
        conn.close()
        with Store(self.tmp) as s:
            rows = {r["number"]: json.loads(r["location"]) for r in s._read("SELECT number, location FROM issues")}
            self.assertEqual(rows[1], {"command": CAPITAL, "place": "P", "seed": 9, "extra": {"biome": "b"},
                                       "commands": [{"command": CAPITAL}]})
            self.assertEqual(rows[2], {})
            self.assertEqual([c["command"] for c in rows[3]["commands"]], [CAPITAL, KEEP])
            self.assertEqual(s._read("SELECT value FROM meta WHERE key='schema'")[0]["value"], "6")


class ApiCommandTests(ApiCase):
    def test_api_round_trip(self):
        self.req("POST", "/api/projects", {"slug": "mygame", "name": "MyGame", "prefix": "MG"})
        status, issue, _ = self.req("POST", "/api/projects/mygame/issues", {
            "title": "x", "commands": [{"command": CAPITAL, "label": "the capital"}, KEEP]})
        self.assertEqual(status, 201)
        self.assertEqual(issue["location"]["command"], CAPITAL)
        status, issue, _ = self.req("PATCH", "/api/issues/MG-1", {"location": {"commands": [
            {"command": GLACIER, "label": "the glacier"}, {"command": CAPITAL}], "seed": 4291}})
        self.assertEqual(status, 200)
        status, got, _ = self.req("GET", "/api/issues/MG-1")
        self.assertEqual(got["location"]["commands"], [{"command": GLACIER, "label": "the glacier"}, {"command": CAPITAL}])
        self.assertEqual(got["location"]["command"], GLACIER)
        status, err, _ = self.req("PATCH", "/api/issues/MG-1", {"commands": [{"command": "x", "label": "y" * 200}]})
        self.assertEqual(status, 400)


class McpCommandTests(McpCase):
    def test_set_location_create_and_get(self):
        err, created = self.call(1, "create_issue", {"title": "keep", "commands": [
            {"command": CAPITAL, "label": "the capital"}, {"command": KEEP, "label": "the keep"}]})
        self.assertFalse(err, created)
        self.assertEqual(created["command"], CAPITAL)
        self.assertEqual([c["label"] for c in created["commands"]], ["the capital", "the keep"])
        err, res = self.call(2, "set_location", {"id": "MG-1", "commands": [{"command": GLACIER, "label": "ice"}],
                                                 "location": {"seed": 4291}})
        self.assertFalse(err, res)
        self.assertEqual((res["location"]["command"], res["location"]["seed"]), (GLACIER, 4291))
        self.assertEqual(res["location"]["commands"], [{"command": GLACIER, "label": "ice"}])
        err, res = self.call(3, "set_location", {"id": "MG-1", "location": {"command": KEEP}})
        self.assertEqual(res["location"]["commands"], [{"command": KEEP, "label": "ice"}])
        err, issue = self.call(4, "get_issue", {"id": "MG-1"})
        self.assertEqual(issue["location"]["commands"], [{"command": KEEP, "label": "ice"}])
        err, msg = self.call(5, "set_location", {"id": "MG-1"})
        self.assertTrue(err)
        err, listing = self.call(6, "list_issues", {})
        self.assertEqual(listing["issues"][0]["command"], "/goto dungeon ruined-keep seed 4291")


class CliCommandTests(StoreCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--data", str(self.tmp), *argv])
        return code, buf.getvalue()

    def test_repeated_command_and_label(self):
        code, out = self.run_cli("add", "--project", "mygame", "--title", "keep", "--command", CAPITAL,
                                 "--command", KEEP, "--label", "the capital", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["location"]["commands"], [{"command": CAPITAL, "label": "the capital"},
                                                                   {"command": KEEP}])
        # Replacing the list keeps the labels of commands it still holds.
        code, out = self.run_cli("edit", "MG-1", "--command", KEEP, "--command", CAPITAL, "--command", GLACIER,
                                 "--label", "the keep", "--seed", "4291", "--json")
        loc = json.loads(out)["location"]
        self.assertEqual(loc["commands"], [{"command": KEEP, "label": "the keep"},
                                           {"command": CAPITAL, "label": "the capital"}, {"command": GLACIER}])
        self.assertEqual(loc["seed"], 4291)
        # --at N replaces one command; one past the end appends.
        code, out = self.run_cli("edit", "MG-1", "--at", "3", "--command", "/goto biome meadow", "--label", "meadow", "--json")
        self.assertEqual(json.loads(out)["location"]["commands"][2], {"command": "/goto biome meadow", "label": "meadow"})
        code, out = self.run_cli("edit", "MG-1", "--at", "4", "--command", "/goto back", "--json")
        self.assertEqual(len(json.loads(out)["location"]["commands"]), 4)
        self.assertEqual(self.run_cli("edit", "MG-1", "--at", "9", "--command", "/goto x")[0], 1)
        self.assertEqual(self.run_cli("edit", "MG-1", "--label", "x")[0], 1)
        self.assertEqual(self.run_cli("edit", "MG-1", "--command", "/a", "--label", "a", "--label", "b")[0], 1)
        code, out = self.run_cli("show", "MG-1")
        self.assertIn("command 1: /goto dungeon ruined-keep seed 4291   (the keep)", out)
        self.assertIn("command 4: /goto back", out)
        code, out = self.run_cli("send", "--issue", "MG-1", "--at", "2")
        self.assertIn("/goto capital seed 4291", out)
        self.assertEqual(self.run_cli("send", "--issue", "MG-1", "--at", "5")[0], 1)


if __name__ == "__main__":
    unittest.main()
