from test_api import ApiCase


class TransferApiTests(ApiCase):
    def test_zip_export_preview_import_and_local_confirmation_guard(self):
        self.store.create_project("source", "Source", "SC")
        self.store.create_project("target", "Target", "TG")
        self.store.create_issue("source", {"title": "Export me", "body": "complete description"})
        code, blob, response = self.req("POST", "/api/projects/source/topics/export", {"ids": ["SC-1"]})
        self.assertEqual(code, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/zip")
        path = "/api/projects/target/topics/"
        self.assertEqual(self.req("POST", path + "import", raw=blob)[0], 403)
        headers = {"X-Pair-Desk-Token": self.server.token, "Content-Type": "application/zip"}
        self.assertEqual(self.req("POST", path + "import", raw=blob, headers={**headers, "Origin": "http://foreign.example"})[0], 403)
        code, preview, _ = self.req("POST", path + "preview", raw=blob, headers=headers)
        self.assertEqual((code, preview["topics"]), (200, 1))
        self.assertEqual(self.store.get_project("target")["issue_count"], 0)
        code, result, _ = self.req("POST", path + "import", raw=blob, headers=headers)
        self.assertEqual((code, result["created"]), (200, ["TG-1"]))
        self.assertEqual(self.store.get_issue("TG-1")["body"], "complete description")
        self.assertEqual(self.req("POST", path + "import", raw=blob, headers=headers)[0], 409)
        self.assertEqual(self.req("POST", path + "preview", raw=b"not a ZIP", headers=headers)[0], 400)
