"""Whole-desk backup and restore: snapshot while open, checks before restoring, never overwriting."""

import contextlib
import os
import io
import json
import sqlite3
import unittest
import zipfile

from helpers import PNG, TempDirCase

from pair_desk import backup
from pair_desk.backup import BackupError, create_backup, read_manifest, restore_backup
from pair_desk.cli import main
from pair_desk.context import save_link
from pair_desk.store import Store


class BackupTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.data = self.tmp / "data"
        self.store = Store(self.data)
        s = self.store
        s.create_project("mygame", "MyGame", "MG")
        s.create_issue("mygame", {"title": "Worker threads stall", "kind": "bug"})
        s.add_comment("MG-1", "owner", "Still slow near the lighthouse")
        self.att = s.add_attachment("MG-1", "shot.png", "image/png", PNG)
        save_link(self.data, self.tmp / "repo-that-moved", "mygame")
        self.zip = self.tmp / "out" / "desk.zip"

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_backup_while_open_and_restore_into_an_empty_folder(self):
        res = create_backup(self.data, self.zip)  # the store stays open, as a running desk would
        self.assertEqual((res["counts"]["projects"], res["counts"]["issues"], res["counts"]["comments"]), (1, 1, 1))
        self.assertEqual((res["attachment_files"], res["missing"]), (1, []))
        with zipfile.ZipFile(self.zip) as z:
            names = set(z.namelist())
            self.assertEqual(z.getinfo(next(n for n in names if n.endswith(".png"))).compress_type, zipfile.ZIP_STORED)
            db = self.tmp / "peek.sqlite"
            db.write_bytes(z.read("desk.sqlite"))
        self.assertTrue({"manifest.json", "desk.sqlite", "projects.json"} <= names)
        conn = sqlite3.connect(db)
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='issue_search'").fetchone())
        conn.close()

        target = self.tmp / "new-machine"
        out = restore_backup(self.zip, target)
        self.assertIsNone(out["previous"])
        self.assertEqual(out["missing_links"], [str(self.tmp / "repo-that-moved")])
        with Store(target) as restored:
            issue = restored.get_issue("MG-1")
            self.assertEqual(issue["comments"][0]["text"], "Still slow near the lighthouse")
            meta, path = restored.get_attachment(self.att["id"])
            self.assertEqual(path.read_bytes(), PNG)
            self.assertTrue(restored.search_ready)  # the index is rebuilt on open
            self.assertEqual([i["id"] for i in restored.list_issues("mygame", {"q": "lighthouse"})["issues"]], ["MG-1"])
        self.assertEqual(list(self.tmp.glob("new-machine.restoring-*")), [])

    def test_an_existing_desk_is_moved_aside_never_overwritten(self):
        create_backup(self.data, self.zip)
        other = self.tmp / "other"
        with Store(other) as s:
            s.create_project("keepme", "Keep me", "KM")
        with self.assertRaisesRegex(BackupError, "--replace"):
            restore_backup(self.zip, other)
        out = restore_backup(self.zip, other, replace=True)
        self.assertTrue(out["previous"].startswith(str(other) + ".before-restore-"))
        with Store(out["previous"]) as kept:
            self.assertEqual([p["slug"] for p in kept.list_projects()], ["keepme"])
        with Store(other) as now:
            self.assertEqual([p["slug"] for p in now.list_projects()], ["mygame"])

    def test_a_running_detached_desk_blocks_replace(self):
        create_backup(self.data, self.zip)
        (self.data / "server.pid").write_text(str(os.getpid()), encoding="ascii")
        with self.assertRaisesRegex(BackupError, "running"):
            restore_backup(self.zip, self.data, replace=True)

    def _rewrite(self, change, name="bad"):
        """Copy the backup with `change(name, data) -> (name, data) | None` applied to each entry."""
        bad = self.tmp / f"{name}.zip"
        with zipfile.ZipFile(self.zip) as src, zipfile.ZipFile(bad, "w") as dst:
            for name in src.namelist():
                out = change(name, src.read(name))
                if out:
                    dst.writestr(*out)
        return bad

    def test_damaged_or_unsafe_backups_change_nothing(self):
        create_backup(self.data, self.zip)
        damaged = self._rewrite(lambda n, d: (n, d[:-1] + b"x") if n.endswith(".png") else (n, d), "damaged")
        unsafe = self._rewrite(lambda n, d: ("../evil.txt", d) if n == "projects.json" else (n, d), "unsafe")
        missing = self._rewrite(lambda n, d: None if n == "desk.sqlite" else (n, d), "missing")
        for bad, msg in ((damaged, "checksum"), (unsafe, "unsafe"), (missing, "missing")):
            target = self.tmp / f"target-{msg}"
            with self.subTest(msg), self.assertRaisesRegex(BackupError, msg):
                restore_backup(bad, target)
            self.assertFalse(target.exists())
            self.assertEqual(list(self.tmp.glob(target.name + ".restoring-*")), [])
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_newer_backups_and_other_zips_are_refused(self):
        create_backup(self.data, self.zip)
        newer = self._rewrite(lambda n, d: (n, json.dumps({**json.loads(d), "schema": "99"}).encode())
                              if n == "manifest.json" else (n, d))
        with self.assertRaisesRegex(BackupError, "newer"):
            read_manifest(newer)
        plain = self.tmp / "plain.zip"
        with zipfile.ZipFile(plain, "w") as z:
            z.writestr("hello.txt", "hi")
        with self.assertRaisesRegex(BackupError, "not a Pair Desk backup"):
            read_manifest(plain)

    def test_backup_refuses_to_overwrite_and_reports_missing_files(self):
        create_backup(self.data, self.zip)
        with self.assertRaisesRegex(BackupError, "already exists"):
            create_backup(self.data, self.zip)
        path = self.store.get_attachment(self.att["id"])[1]
        path.unlink()
        res = create_backup(self.data, self.tmp / "second.zip")
        self.assertEqual((res["attachment_files"], len(res["missing"])), (0, 1))

    def test_default_path_is_in_the_backups_folder(self):
        res = create_backup(self.data)
        self.assertEqual(backup.BACKUPS_DIR, "backups")
        self.assertTrue(res["path"].startswith(str(self.data / "backups")))

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--data", str(self.data), "backup", "--out", str(self.zip)]), 0)
            self.assertEqual(main(["--data", str(self.tmp / "x"), "restore", str(self.zip), "--check"]), 0)
            self.assertEqual(main(["--data", str(self.tmp / "x"), "restore", str(self.zip)]), 0)
            self.assertEqual(main(["--data", str(self.tmp / "x"), "restore", str(self.zip)]), 1)  # needs --replace
        text = out.getvalue()
        self.assertIn("Backed up 1 projects, 1 issues, 1 comments and 1 attachment files", text)
        self.assertIn("this version can restore it", text)
        self.assertIn("Restored 1 projects", text)


if __name__ == "__main__":
    unittest.main()
