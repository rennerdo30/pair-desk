"""Project counts reflect records, independently of shared number allocations."""
from helpers import StoreCase
from pair_desk.store import Store
from pair_desk.sync_data import apply_snapshot, configure_identity, reserve_numbers, snapshot


class SyncCountTests(StoreCase):
    def assert_counts(self, store, expected):
        self.assertEqual(store.get_project("mygame")["issue_count"], expected)
        self.assertEqual(store.list_projects()[0]["issue_count"], expected)
        self.assertEqual(store.project_overview("mygame")["issue_count"], expected)

    def test_join_import_and_replenish_count_records(self):
        self.store.create_issue("mygame", {"title": "Existing issue"})
        document = snapshot(self.store, "mygame")
        joined = Store(self.tmp / "joined", clock=self.clock)
        self.addCleanup(joined.close)
        joined.create_project("mygame", "MyGame", "MG")
        configure_identity(joined, "mygame", document["project"]["uuid"])
        reserve_numbers(joined, "mygame", 1001, 2000)
        self.assert_counts(joined, 0)
        apply_snapshot(joined, "mygame", document)
        self.assert_counts(joined, 1)
        joined.create_issue("mygame", {"title": "Joined device issue"})
        self.assert_counts(joined, 2)
        reserve_numbers(joined, "mygame", 3001, 4000)
        self.assert_counts(joined, 2)
        joined.create_issue("mygame", {"title": "New reservation issue"})
        self.assert_counts(joined, 3)
