import io
import json
import zipfile
from unittest.mock import patch

from helpers import PNG, StoreCase
from pair_desk import launch
from pair_desk.store import Conflict, Invalid
from pair_desk.transfer import export_topics, import_topics, read_archive


class TransferTests(StoreCase):
    def fixture(self):
        self.store.create_issue("mygame", {"title": "Parent", "body": "Full body", "tags": ["tag"]})
        self.store.create_issue("mygame", {"title": "Child"})
        self.store.link_parent("MG-2", "MG-1")
        self.store.set_plan("MG-1", ["Keep this plan"], "Check data", "codex")
        self.store.add_comment("MG-1", "owner", "History stays", "failed")
        cm = self.store.get_issue("MG-1")["comments"][0]["id"]
        self.store.add_attachment("MG-1", "shot.png", "image/png", PNG, cm)
        self.store.queue_command("mygame", "/goto example", "MG-1")
        self.store.create_project("destination", "Destination", "DT")
        self.store.create_issue("destination", {"title": "Existing"})
        return export_topics(self.store, "mygame", ["MG-1"])

    def rewrite(self, blob, mutate, extra=None):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            contents = {n: z.read(n) for n in z.namelist()}
        manifest = json.loads(contents["manifest.json"])
        mutate(manifest)
        contents["manifest.json"] = json.dumps(manifest).encode()
        contents.update(extra or {})
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for name, data in contents.items():
                z.writestr(name, data)
        return out.getvalue()

    def test_roundtrip_preserves_history_plan_files_and_relationships(self):
        blob = self.fixture()
        before = self.store.get_issue("DT-1")
        preview = import_topics(self.store, "destination", blob)
        self.assertEqual((preview["topics"], preview["attachments"]), (2, 1))
        self.assertEqual(self.store.get_project("destination")["issue_count"], 1)
        result = import_topics(self.store, "destination", blob, preview=False)
        parent = self.store.get_issue(result["created"][0])
        child = self.store.get_issue(result["created"][1])
        original = self.store.get_issue("MG-1")
        for key in ("body", "tags", "plan", "status", "created_at", "updated_at"):
            self.assertEqual(parent[key], original[key], key)
        self.assertEqual(child["parent"], parent["id"])
        self.assertEqual(len(parent["comments"]), 1, "historical comments must survive import")
        self.assertEqual(parent["comments"][0]["text"], "History stays")
        self.assertEqual(parent["comments"][0]["verdict"], "failed")
        attachment = parent["attachments"][0]
        self.assertEqual(attachment["comment_id"], parent["comments"][0]["id"])
        self.assertEqual(self.store.get_attachment(attachment["id"])[1].read_bytes(), PNG)
        self.assertEqual(parent["activity"][:-1], [{**a, "id": b["id"]} for a, b in zip(original["activity"], parent["activity"])])
        self.assertTrue(parent["activity"][-1]["detail"]["original"]["commands"])
        self.assertEqual(parent["commands"], [])
        self.assertEqual(self.store.get_issue("DT-1"), before)
        self.assertTrue(import_topics(self.store, "destination", blob)["already_imported"])
        with self.assertRaises(Conflict):
            import_topics(self.store, "destination", blob, preview=False)

    def test_unsafe_invalid_and_corrupt_archives_do_not_write(self):
        blob = self.fixture()
        variants = [b"invalid", self.rewrite(blob, lambda m: m.update(version=99)),
                    self.rewrite(blob, lambda m: None, {"../escape": b"bad"}),
                    self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(parent_id=999)),
                    self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(parent_id=m["tables"]["issues"][0]["id"])),
                    self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(status="forged")),
                    self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(status="")),
                    self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(plan='{"steps":1}')),
                    self.rewrite(blob, lambda m: m["tables"]["attachments"][0].update(comment_id=999)),
                    self.rewrite(blob, lambda m: m["files"]["files/1"].update(sha256="wrong"))]
        for bad in variants:
            with self.subTest(size=len(bad)), self.assertRaises(Invalid):
                import_topics(self.store, "destination", bad, preview=False)
        self.assertEqual(self.store.get_project("destination")["issue_count"], 1)

    def test_transaction_failure_rolls_back_rows_numbers_and_files(self):
        blob = self.fixture()
        paths = set(self.store.attachments_dir.rglob("*"))
        with patch.object(self.store, "_log", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                import_topics(self.store, "destination", blob, preview=False)
        self.assertEqual(self.store.get_project("destination")["issue_count"], 1)
        self.assertEqual({p for p in self.store.attachments_dir.rglob("*") if p.is_file()}, {p for p in paths if p.is_file()})
        self.assertEqual(self.store.create_issue("destination", {"title": "Next"})["id"], "DT-2")

    def test_imported_build_is_inert(self):
        blob = self.fixture()
        blob = self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(build=json.dumps({"path": "E:/example.exe", "label": "Imported", "number": 1})))
        result = import_topics(self.store, "destination", blob, preview=False)
        build = self.store.get_issue(result["created"][0])["build"]
        with self.assertRaises(Conflict):
            launch.registered_path(build)

    def test_export_refuses_missing_files_and_other_project_selection(self):
        blob = self.fixture()
        with self.assertRaises(Invalid):
            export_topics(self.store, "destination", ["MG-1"])
        self.store.get_attachment(1)[1].unlink()
        with self.assertRaises(Invalid):
            export_topics(self.store, "mygame", ["MG-1"])

    def test_inline_attachment_links_are_remapped(self):
        blob = self.fixture()
        blob = self.rewrite(blob, lambda m: m["tables"]["issues"][0].update(body="![shot](/api/attachments/1)"))
        result = import_topics(self.store, "destination", blob, preview=False)
        issue = self.store.get_issue(result["created"][0])
        self.assertEqual(issue["body"], f"![shot](/api/attachments/{issue['attachments'][0]['id']})")

    def test_shared_reservation_exhaustion_is_atomic(self):
        blob = self.fixture()
        pid = self.store._project_row("destination")["id"]
        with self.store._tx() as c:
            c.execute("INSERT INTO sync_projects(project_id,uuid,shared,range_start,range_end) VALUES (?,?,1,2,2)", (pid, "test-project"))
        with self.assertRaises(Conflict):
            import_topics(self.store, "destination", blob, preview=False)
        self.assertEqual(self.store.get_project("destination")["issue_count"], 1)

    def test_merged_source_export_does_not_follow_redirect_and_can_unmerge(self):
        self.fixture()
        self.store.create_issue("mygame", {"title": "Duplicate"})
        self.store.create_issue("mygame", {"title": "Duplicate child"})
        self.store.link_parent("MG-4", "MG-3")
        self.store.add_comment("MG-3", "owner", "Source comment")
        self.store.merge_issues("MG-1", ["MG-3"])
        blob = export_topics(self.store, "mygame", ["MG-3"])
        manifest, _ = read_archive(self.store, blob)
        self.assertEqual(len(manifest["tables"]["issues"]), 4)
        result = import_topics(self.store, "destination", blob, preview=False)
        source = result["created"][2]
        self.assertEqual(self.store.get_issue(source, follow=False)["merged_into"], result["created"][0])
        self.store.unmerge(source)
        self.assertEqual(self.store.get_issue(source)["comments"][0]["text"], "Source comment")
        self.assertEqual(self.store.get_issue(result["created"][3])["parent"], source)
