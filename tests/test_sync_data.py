"""Portable sync acceptance checks, using synthetic temporary desks only."""
import copy
import json
import uuid
from unittest.mock import patch

from helpers import PNG, StoreCase
from pair_desk.store import Conflict, Invalid, Store
from pair_desk.sync_data import (apply_snapshot, clear_number_reservation,
                                 configure_identity, merge, reserve_numbers,
                                 snapshot, validate)


class SyncDataTests(StoreCase):
    def desk(self, name):
        store = Store(self.tmp / name, clock=self.clock)
        self.addCleanup(store.close)
        store.create_project("mygame", "MyGame", "MG")
        return store

    def test_fresh_roundtrip_all_entities_and_retry(self):
        s = self.store
        s.update_project("mygame", {"default_seed": 42, "lead": "codex"})
        s.create_issue("mygame", {"title": "Target", "source": "agent"})
        s.create_issue("mygame", {"title": "Source"})
        s.create_issue("mygame", {"title": "Child"})
        s.link_parent("MG-3", "MG-2")
        s.set_plan("MG-1", ["Check"], "Focused check")
        verdict = s.add_comment("MG-2", "owner", "Historical verdict", verdict="passed")
        s.add_attachment("MG-2", "shot.png", "image/png", PNG, comment_id=verdict["comment"]["id"])
        s.set_handoff("mygame", "## State\nFirst")
        s.set_handoff("mygame", "## State\nSecond")
        s.set_build("mygame", "E:/synthetic/game.exe", "abc")
        s.queue_command("mygame", "/test", "MG-1")
        s.merge_issues("MG-1", ["MG-2"])
        doc = json.loads(json.dumps(snapshot(s, "mygame"), sort_keys=True))
        validate(doc)
        second = self.desk("second")
        configure_identity(second, "mygame", doc["project"]["uuid"])
        apply_snapshot(second, "mygame", doc)
        doc = snapshot(second, "mygame")
        fresh = self.desk("third")
        configure_identity(fresh, "mygame", doc["project"]["uuid"])
        apply_snapshot(fresh, "mygame", doc)
        self.assertEqual(snapshot(fresh, "mygame"), doc)
        self.assertEqual(apply_snapshot(fresh, "mygame", doc), {"inserted": 0, "updated": 0, "deleted": 0})
        self.assertEqual(snapshot(fresh, "mygame"), doc)
        self.assertEqual(fresh.list_commands("mygame"), [])
        self.assertIsNone(fresh.get_project("mygame")["lead"])
        self.assertTrue(fresh.get_build("mygame")["build"]["sync_remote"])
        fresh.unmerge("MG-2")
        self.assertEqual(fresh.get_issue("MG-3")["parent"], "MG-2")
        restored = fresh.get_issue("MG-2")
        self.assertEqual(restored["comments"][0]["verdict"], "passed")
        self.assertEqual(len(restored["attachments"]), 1)

    def branches(self):
        self.store.create_issue("mygame", {"title": "Original", "body": "Body"})
        base = snapshot(self.store, "mygame")
        configure_identity(self.store, "mygame", base["project"]["uuid"])
        remote = self.desk("remote")
        configure_identity(remote, "mygame", base["project"]["uuid"])
        apply_snapshot(remote, "mygame", base)
        return base, remote, next(iter(base["records"]["issues"]))

    def test_distinct_fields_and_concurrent_comments(self):
        base, remote, key = self.branches()
        self.clock.advance(seconds=1)
        self.store.update_issue("MG-1", {"title": "Local title"})
        self.store.add_comment("MG-1", "agent", "Local comment")
        self.clock.advance(seconds=1)
        remote.update_issue("MG-1", {"body": "Remote body"})
        remote.add_comment("MG-1", "owner", "Remote comment")
        local = snapshot(self.store, "mygame")
        other = snapshot(remote, "mygame")
        result, conflicts = merge(base, local, other)
        self.assertEqual(conflicts, [])
        self.assertEqual(result["records"]["issues"][key]["title"], "Local title")
        self.assertEqual(result["records"]["issues"][key]["body"], "Remote body")
        self.assertEqual(len(result["records"]["comments"]), 2)
        apply_snapshot(self.store, "mygame", result, expected=local)
        self.assertEqual(snapshot(self.store, "mygame"), result)

    def test_same_field_conflict_preserves_inputs_and_full_path(self):
        base, remote, key = self.branches()
        self.store.update_issue("MG-1", {"title": "Local"})
        remote.update_issue("MG-1", {"title": "Remote"})
        local, other = snapshot(self.store, "mygame"), snapshot(remote, "mygame")
        unchanged = copy.deepcopy((base, local, other))
        result, conflicts = merge(base, local, other)
        self.assertEqual((base, local, other), unchanged)
        self.assertEqual(conflicts, [{"path": ["records", "issues", key, "title"],
                                    "base": "Original", "local": "Local", "remote": "Remote", "kind": "field"}])
        self.assertEqual(result["records"]["issues"][key]["title"], "Local")

    def test_delete_edit_and_delete_new_comment_conflict_preserves_graph(self):
        for edit in ("field", "comment"):
            with self.subTest(edit=edit):
                base, remote, key = self.branches() if edit == "field" else (base, remote, key)
                if edit == "field":
                    self.store.delete_issue("MG-1")
                    remote.update_issue("MG-1", {"body": "Edited"})
                else:
                    remote.add_comment("MG-1", "owner", "Added after deletion")
                local, other = snapshot(self.store, "mygame"), snapshot(remote, "mygame")
                result, conflicts = merge(base, local, other)
                self.assertEqual(result["records"]["issues"][key], {"deleted": True})
                self.assertEqual(conflicts[0]["path"], ["records"])
                self.assertEqual(conflicts[0]["remote"], other["records"])
                self.assertEqual(conflicts[0]["kind"], "deletion-edit")

    def test_explicit_deletion_roundtrip_and_row_id_reuse(self):
        base, remote, first = self.branches()
        self.store.add_attachment("MG-1", "first.png", "image/png", PNG)
        before = snapshot(self.store, "mygame")
        attachment = self.store.get_issue("MG-1")["attachments"][0]["id"]
        attachment_uuid = next(iter(before["records"]["attachments"]))
        self.store.delete_attachment(attachment)
        self.store.add_attachment("MG-1", "second.png", "image/png", PNG)
        after = snapshot(self.store, "mygame")
        self.assertEqual(after["records"]["attachments"][attachment_uuid], {"deleted": True})
        attached_events = [r for r in after["records"]["activity"].values() if r.get("action") == "attached"]
        self.assertEqual(attached_events[0]["detail"]["attachment"], attachment_uuid)
        self.assertNotEqual(attached_events[1]["detail"]["attachment"], attachment_uuid)
        apply_snapshot(remote, "mygame", after)
        self.assertEqual(snapshot(remote, "mygame"), after)
        self.store.delete_issue("MG-1")
        deleted = snapshot(self.store, "mygame")
        apply_snapshot(remote, "mygame", deleted)
        self.assertEqual(snapshot(remote, "mygame"), deleted)

    def test_intervening_other_connection_write_rejects_with_no_import(self):
        base, remote, key = self.branches()
        remote.update_issue("MG-1", {"body": "Remote"})
        other = Store(self.tmp, clock=self.clock)
        self.addCleanup(other.close)
        other.update_issue("MG-1", {"title": "Intervening"})
        before = snapshot(self.store, "mygame")
        with self.assertRaises(Conflict):
            apply_snapshot(self.store, "mygame", snapshot(remote, "mygame"), expected=base)
        self.assertEqual(snapshot(self.store, "mygame"), before)

    def test_checkpoint_is_same_transaction_and_callback_failure_rolls_back(self):
        base, remote, key = self.branches()
        remote.add_attachment("MG-1", "new.png", "image/png", PNG)
        incoming = snapshot(remote, "mygame")
        paths = set(self.store.attachments_dir.rglob("*"))
        def checkpoint(c):
            self.assertIs(c, self.store.conn)
            self.assertTrue(c.in_transaction)
            c.execute("CREATE TABLE checkpoint(value TEXT)")
            c.execute("INSERT INTO checkpoint VALUES ('accepted')")
            raise RuntimeError("simulate checkpoint failure")
        with self.assertRaisesRegex(RuntimeError, "checkpoint"):
            apply_snapshot(self.store, "mygame", incoming, expected=base, on_commit=checkpoint)
        self.assertEqual(snapshot(self.store, "mygame"), base)
        self.assertEqual(set(self.store.attachments_dir.rglob("*")), paths)
        self.assertIsNone(self.store.conn.execute("SELECT name FROM sqlite_master WHERE name='checkpoint'").fetchone())
        apply_snapshot(self.store, "mygame", incoming, expected=base,
                       on_commit=lambda c: c.execute("INSERT INTO meta(key,value) VALUES ('sync-test','accepted')"))
        self.assertEqual(self.store.conn.execute("SELECT value FROM meta WHERE key='sync-test'").fetchone()[0], "accepted")

    def test_malformed_documents_do_not_partially_import(self):
        base, remote, key = self.branches()
        remote.add_attachment("MG-1", "new.png", "image/png", PNG)
        valid = snapshot(remote, "mygame")
        att = next(iter(valid["records"]["attachments"]))
        mutations = [
            lambda d: d.update(complete=False),
            lambda d: d["records"]["issues"][key].update(parent_id=key),
            lambda d: d["records"]["issues"][key].update(parent_id=str(uuid.uuid4())),
            lambda d: d["records"]["issues"][key].update(status="invented"),
            lambda d: d["records"]["issues"][key].update(number=True),
            lambda d: d["records"]["issues"][key].update(plan={"steps": [{"text": "x", "state": "bad"}], "verification": ""}),
            lambda d: d["records"]["attachments"][att].update(stored_path="../../bad"),
            lambda d: d["records"]["attachments"][att].update(filename="../bad.png"),
            lambda d: d["records"]["attachments"][att].update(mime="image/jpeg"),
            lambda d: d["records"]["attachments"][att].update(sha256="0" * 64),
            lambda d: d["records"]["attachments"][att].update(size=1),
            lambda d: d["records"]["attachments"][att].update(data_base64="%%%"),
            lambda d: d["records"]["issues"].pop(key),
        ]
        for mutate in mutations:
            broken = copy.deepcopy(valid)
            mutate(broken)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(Invalid):
                apply_snapshot(self.store, "mygame", broken)
            self.assertEqual(snapshot(self.store, "mygame"), base)
            self.assertEqual(list(self.store.attachments_dir.rglob("*.blob")), [])

    def test_handoff_causality_conflict_cycles_and_source_versions(self):
        base, remote, key = self.branches()
        self.store.set_handoff("mygame", "## State\nBase")
        base = snapshot(self.store, "mygame")
        apply_snapshot(remote, "mygame", base)
        self.store.set_handoff("mygame", "## State\nLocal")
        remote.set_handoff("mygame", "## State\nRemote")
        local, other = snapshot(self.store, "mygame"), snapshot(remote, "mygame")
        result, conflicts = merge(base, local, other)
        self.assertEqual(conflicts[0]["path"], ["handoff_head"])
        self.assertEqual(len(result["records"]["handoffs"]), 3)
        validate(result)
        apply_snapshot(self.store, "mygame", result)
        self.assertEqual(snapshot(self.store, "mygame"), result)
        self.assertEqual(self.store.get_handoff("mygame")["markdown"], "## State\nLocal\n")
        broken = copy.deepcopy(result)
        keys = list(broken["records"]["handoffs"])
        broken["records"]["handoffs"][keys[0]]["parents"] = [keys[1]]
        broken["records"]["handoffs"][keys[1]]["parents"] = [keys[0]]
        with self.assertRaisesRegex(Invalid, "cycle"):
            validate(broken)

    def test_plan_and_current_build_conflict_atomically(self):
        base, remote, key = self.branches()
        self.store.set_plan("MG-1", ["Local"], "Check local")
        remote.set_plan("MG-1", ["Remote"], "Check remote")
        self.store.set_build("mygame", "local-v1")
        remote.set_build("mygame", "remote-v1")
        local, other = snapshot(self.store, "mygame"), snapshot(remote, "mygame")
        result, conflicts = merge(base, local, other)
        paths = [c["path"] for c in conflicts]
        self.assertIn(["records", "issues", key, "plan"], paths)
        self.assertIn(["project", "build"], paths)
        self.assertEqual(result["project"]["build"], local["project"]["build"])

    def test_shared_blocks_exhaustion_idempotent_install_import_and_disable(self):
        base, remote, key = self.branches()
        with self.assertRaisesRegex(Conflict, "block"):
            self.store.create_issue("mygame", {"title": "Missing block"})
        reserve_numbers(self.store, "mygame", 1001, 2000)
        reserve_numbers(remote, "mygame", 2001, 3000)
        self.assertEqual(self.store.create_issue("mygame", {"title": "Local"})["id"], "MG-1001")
        self.assertEqual(remote.create_issue("mygame", {"title": "Remote"})["id"], "MG-2001")
        reserve_numbers(self.store, "mygame", 1001, 2000)
        self.assertEqual(self.store.conn.execute("SELECT next_number FROM projects").fetchone()[0], 1002)
        local = snapshot(self.store, "mygame")
        combined, conflicts = merge(base, local, snapshot(remote, "mygame"))
        self.assertEqual(conflicts, [])
        apply_snapshot(self.store, "mygame", combined, expected=local)
        self.assertEqual(self.store.conn.execute("SELECT next_number FROM projects").fetchone()[0], 1002)
        with self.store._tx() as c:
            c.execute("UPDATE projects SET next_number=2000")
        self.assertEqual(self.store.create_issue("mygame", {"title": "Last"})["id"], "MG-2000")
        with self.assertRaisesRegex(Conflict, "exhausted"):
            self.store.create_issue("mygame", {"title": "Overflow"})
        clear_number_reservation(self.store, "mygame")
        self.assertEqual(self.store.create_issue("mygame", {"title": "Offline"})["id"], "MG-2002")

    def test_empty_uuid_adoption_populated_mismatch_and_no_untracked_bookkeeping(self):
        self.store.create_issue("mygame", {"title": "Existing"})
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM sync_entities").fetchone()[0], 0)
        with self.assertRaisesRegex(Conflict, "snapshot"):
            configure_identity(self.store, "mygame", str(uuid.uuid4()))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM sync_projects").fetchone()[0], 0)
        initial = snapshot(self.store, "mygame")
        with self.assertRaises(Conflict):
            configure_identity(self.store, "mygame", str(uuid.uuid4()))
        empty = self.desk("empty")
        snapshot(empty, "mygame")
        configure_identity(empty, "mygame", initial["project"]["uuid"])
        apply_snapshot(empty, "mygame", initial)

    def test_unchanged_local_build_capability_retained_imported_build_requires_registration(self):
        base, remote, key = self.branches()
        self.store.set_build("mygame", "E:/synthetic/local.exe")
        local = snapshot(self.store, "mygame")
        apply_snapshot(self.store, "mygame", local)
        self.assertNotIn("sync_remote", self.store.get_build("mygame")["build"])
        apply_snapshot(remote, "mygame", local)
        remote.set_status("MG-1", "to_check")
        self.assertTrue(remote.get_issue("MG-1")["build"]["sync_remote"])
        remote.set_build("mygame", "E:/synthetic/local.exe")
        self.assertNotIn("sync_remote", remote.get_build("mygame")["build"])
        self.assertNotIn("sync_remote", remote.get_issue("MG-1")["build"])

    def test_multiple_merge_unmerge_history_keeps_latest_previous_status(self):
        s = self.store
        s.create_issue("mygame", {"title": "Target"})
        s.create_issue("mygame", {"title": "Source", "status": "open"})
        snapshot(s, "mygame")
        s.merge_issues("MG-1", ["MG-2"])
        s.unmerge("MG-2")
        s.set_status("MG-2", "parked")
        s.merge_issues("MG-1", ["MG-2"])
        doc = json.loads(json.dumps(snapshot(s, "mygame"), sort_keys=True))
        remote = self.desk("merged")
        configure_identity(remote, "mygame", doc["project"]["uuid"])
        apply_snapshot(remote, "mygame", doc)
        remote.unmerge("MG-2")
        self.assertEqual(remote.get_issue("MG-2")["status"], "parked")

    def test_validation_rejects_wrong_tombstone_type_unicode_and_total_cap(self):
        base, remote, key = self.branches()
        broken = copy.deepcopy(base)
        broken["records"]["comments"][str(uuid.uuid4())] = {"deleted": 1}
        with self.assertRaises(Invalid):
            validate(broken)
        broken = copy.deepcopy(base)
        broken["records"]["issues"][key]["title"] = "\ud800"
        with self.assertRaises(Invalid):
            validate(broken)
        with patch("pair_desk.sync_data.MAX_DOCUMENT_BYTES", 1), self.assertRaisesRegex(Invalid, "128 MiB"):
            validate(base)

    def test_issue_number_immutable_and_missing_tombstone_rejected(self):
        base, remote, key = self.branches()
        broken = copy.deepcopy(base)
        broken["records"]["issues"][key]["number"] = 99
        with self.assertRaisesRegex(Invalid, "immutable"):
            apply_snapshot(self.store, "mygame", broken)
        broken = copy.deepcopy(base)
        broken["records"]["activity"].clear()
        validate(broken)
        with self.assertRaisesRegex(Invalid, "omitted"):
            apply_snapshot(self.store, "mygame", broken)
        with self.assertRaisesRegex(Invalid, "tombstone"):
            merge(base, base, broken)
        self.assertEqual(snapshot(self.store, "mygame"), base)

    def test_new_issue_row_id_reuse_does_not_reuse_uuid(self):
        base, remote, key = self.branches()
        old_id = self.store.conn.execute("SELECT id FROM issues").fetchone()[0]
        self.store.delete_issue("MG-1")
        reserve_numbers(self.store, "mygame", 1001, 2000)
        self.store.create_issue("mygame", {"title": "New after delete"})
        new = snapshot(self.store, "mygame")
        self.assertEqual(self.store.conn.execute("SELECT id FROM issues").fetchone()[0], old_id)
        self.assertEqual(new["records"]["issues"][key], {"deleted": True})
        self.assertEqual(len(new["records"]["issues"]), 2)
        self.assertEqual(apply_snapshot(remote, "mygame", new)["deleted"], 2)
        self.assertEqual(snapshot(remote, "mygame"), new)

    def test_shared_reservation_replenishment_never_rewinds(self):
        base, remote, key = self.branches()
        reserve_numbers(self.store, "mygame", 1001, 2000)
        self.store.create_issue("mygame", {"title": "Allocated"})
        with self.assertRaises(Conflict):
            reserve_numbers(self.store, "mygame", 1501, 2500)
        reserve_numbers(self.store, "mygame", 3001, 4000)
        self.assertEqual(self.store.create_issue("mygame", {"title": "Replenished"})["id"], "MG-3001")
        with self.assertRaises(Conflict):
            reserve_numbers(self.store, "mygame", 1001, 2000)

    def test_coupled_relationships_and_concurrent_field_edit_conflict(self):
        base, remote, key = self.branches()
        reserve_numbers(self.store, "mygame", 1001, 2000)
        self.store.create_issue("mygame", {"title": "Parent"})
        base = snapshot(self.store, "mygame")
        apply_snapshot(remote, "mygame", base)
        self.store.link_parent("MG-1", "MG-1001")
        remote.update_issue("MG-1", {"body": "Changed concurrently"})
        local, other = snapshot(self.store, "mygame"), snapshot(remote, "mygame")
        _, conflicts = merge(base, local, other)
        self.assertEqual(conflicts[0]["path"], ["records"])
        self.assertEqual(conflicts[0]["kind"], "coupled-relationships")

    def test_status_conflict_keeps_closed_time_coupled_for_either_choice(self):
        base, remote, key = self.branches()
        self.clock.advance(seconds=1)
        self.store.add_comment("MG-1", "owner", "Local failed", verdict="failed")
        self.clock.advance(seconds=1)
        remote.add_comment("MG-1", "owner", "Remote passed", verdict="passed")
        local, other = snapshot(self.store, "mygame"), snapshot(remote, "mygame")
        candidate, conflicts = merge(base, local, other)
        validate(candidate)
        self.assertEqual(conflicts[0]["path"], ["records", "issues", key])
        self.assertEqual(conflicts[0]["kind"], "lifecycle")
        self.assertEqual(candidate["records"]["issues"][key]["status"], "failed")
        self.assertIsNone(candidate["records"]["issues"][key]["closed_at"])
        for side in ("local", "remote"):
            selected = copy.deepcopy(candidate)
            selected["records"]["issues"][key] = conflicts[0][side]
            validate(selected)
            self.assertEqual(len(selected["records"]["comments"]), 2)
