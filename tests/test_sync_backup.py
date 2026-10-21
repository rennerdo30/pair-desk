"""Restoring a desk preserves shared history without cloning a device's write range."""
from helpers import StoreCase

from pair_desk.backup import create_backup, restore_backup
from pair_desk.store import Store


class SyncBackupTests(StoreCase):
    def test_restore_requires_new_device_binding_and_preserves_recovery_state(self):
        self.store.create_issue("mygame", {"title": "preserve me"})
        with self.store._tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sync_projects (project_id INTEGER PRIMARY KEY, "
                         "uuid TEXT UNIQUE NOT NULL, shared INTEGER NOT NULL DEFAULT 0, "
                         "range_start INTEGER, range_end INTEGER)")
            conn.execute("INSERT INTO sync_projects VALUES (1, 'shared-project', 1, 1001, 2000)")
            conn.execute("CREATE TABLE git_sync (slug TEXT PRIMARY KEY, enabled INTEGER, status TEXT, "
                         "binding TEXT, device_uuid TEXT, baseline TEXT, pending TEXT, conflicts TEXT)")
            conn.execute("INSERT INTO git_sync VALUES ('mygame', 1, 'synced', 'old-pc', 'old-device', "
                         "'baseline', 'pending-with-old-device', 'conflicts')")
        archive = self.tmp / "backup.zip"
        create_backup(self.tmp, archive)
        restored_dir = self.tmp / "restored"
        restore_backup(archive, restored_dir)
        with Store(restored_dir) as restored:
            row = dict(restored.conn.execute("SELECT * FROM git_sync").fetchone())
            self.assertEqual(row["enabled"], 0)
            self.assertEqual(row["status"], "rebind-required")
            self.assertIsNone(row["binding"])
            self.assertIsNone(row["device_uuid"])
            self.assertEqual((row["baseline"], row["pending"], row["conflicts"]),
                             ("baseline", "pending-with-old-device", "conflicts"))
            allocation = restored.conn.execute("SELECT shared, range_start, range_end FROM sync_projects").fetchone()
            self.assertEqual(tuple(allocation), (1, None, None))
            self.assertEqual(restored.get_issue("MG-1")["title"], "preserve me")
        self.assertEqual(self.store.conn.execute("SELECT enabled FROM git_sync").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT range_start FROM sync_projects").fetchone()[0], 1001)

