"""Synced build evidence cannot become a local launch capability."""
import contextlib
import io
import json
import re
from unittest import mock

from helpers import StoreCase
from test_api import ApiCase
from test_build_launch import make_build

from pair_desk import launch
from pair_desk.cli import main


def mark_remote(store):
    with store._tx() as conn:
        row = conn.execute("SELECT id, build FROM projects WHERE slug='mygame'").fetchone()
        state = json.loads(row["build"])
        state["current"]["sync_remote"] = True
        conn.execute("UPDATE projects SET build=? WHERE id=?", (json.dumps(state), row["id"]))
        for issue in conn.execute("SELECT id, build FROM issues WHERE project_id=?", (row["id"],)).fetchall():
            stamp = json.loads(issue["build"])
            stamp["sync_remote"] = True
            conn.execute("UPDATE issues SET build=? WHERE id=?", (json.dumps(stamp), issue["id"]))


class SyncedLaunchApiTests(ApiCase):
    def test_remote_build_path_does_not_grant_launch_or_open(self):
        self.store.create_project("mygame", "My Game", "MG")
        exe = make_build(self.tmp / "builds")
        self.store.create_issue("mygame", {"title": "check", "status": "to_check"})
        self.store.set_build("mygame", str(exe))
        mark_remote(self.store)
        _, page, _ = self.req("GET", "/")
        token = re.search(rb'name="pair-desk-token" content="([^"]+)"', page).group(1).decode()
        headers = {"X-Pair-Desk-Token": token, "Origin": f"http://127.0.0.1:{self.port}"}
        with mock.patch.object(launch, "_spawn") as spawn:
            for path in ("/api/projects/mygame", "/api/issues/MG-1"):
                status, record, _ = self.req("GET", path)
                self.assertEqual(status, 200)
                self.assertFalse(record["build"]["local"]["run"])
                self.assertFalse(record["build"]["local"]["open"])
            for target in ("/api/projects/mygame/build", "/api/issues/MG-1/build"):
                for action in ("open", "run"):
                    status, body, _ = self.req("POST", target + "/" + action, {}, headers=headers)
                    self.assertEqual(status, 409, body)
            spawn.assert_not_called()


class SyncedLaunchCliTests(StoreCase):
    def test_cli_cannot_launch_synced_evidence(self):
        exe = make_build(self.tmp / "builds")
        self.store.set_build("mygame", str(exe))
        mark_remote(self.store)
        with mock.patch.object(launch, "_spawn") as spawn, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            for action in ("open", "run"):
                self.assertEqual(main(["--data", str(self.tmp), "build", action, "--project", "mygame"]), 1)
            spawn.assert_not_called()

