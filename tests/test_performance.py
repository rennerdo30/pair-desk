"""Query, cache, WAL concurrency and conditional HTTP regression tests."""
import concurrent.futures
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import PNG, PNG_B64, StoreCase
from test_api import ApiCase
from pair_desk.mcp import McpServer, _compact
from pair_desk.store import Store, PERFORMANCE_INDEXES


class QueryTests(StoreCase):
    def test_older_sqlite_cte_syntax_keeps_search_pagination_and_facets(self):
        s = self.store
        s.create_issue("mygame", {"title": "needle first", "status": "open", "priority": "p1"})
        s.create_issue("mygame", {"title": "needle second", "status": "closed", "priority": "p2"})
        filters = {"q": "needle", "status": "open", "sort": "backlog", "limit": 1}
        with s._snapshot():
            expected = s.list_issues("mygame", filters)
        with patch("pair_desk.store.sqlite3.sqlite_version_info", (3, 31, 0)), s._snapshot():
            actual = s.list_issues("mygame", filters)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["counts"]["status"], {"open": 1, "closed": 1})

    def test_optional_summary_keeps_metadata_and_default_contract(self):
        s = self.store
        s.create_issue("mygame", {"title": "one", "body": "full description", "command": "/goto 1 2"})
        s.set_plan("MG-1", ["first", "second"], "verify")
        s.add_comment("MG-1", "agent", "history")
        full = s.list_issues("mygame")
        summary = s.list_issues("mygame", {"summary": "1"})
        self.assertEqual(summary["counts"], full["counts"])
        self.assertEqual(summary["total"], full["total"])
        self.assertEqual(summary["issues"][0], {k: v for k, v in full["issues"][0].items() if k not in ("body", "plan")})
        self.assertEqual(full["issues"][0]["body"], "full description")
        self.assertEqual(s.get_issue("MG-1")["plan"]["verification"], "verify")

    def test_facets_keep_other_filters_and_ignore_their_own(self):
        s = self.store
        s.create_issue("mygame", {"title": "one", "status": "open", "priority": "p1", "area": "Terrain",
                                  "kind": "task", "size": "S", "milestone": "Alpha", "location": {"seed": 42}})
        s.create_issue("mygame", {"title": "two", "status": "closed", "priority": "p1", "area": "Terrain",
                                  "kind": "bug", "size": "M", "milestone": "Beta", "location": {"seed": 7}})
        s.create_issue("mygame", {"title": "three", "status": "open", "priority": "p2", "area": "Water",
                                  "kind": "task", "size": "L", "milestone": "Alpha", "location": {"seed": 42}})
        res = s.list_issues("mygame", {"status": "open", "priority": "p1", "area": "TERRAIN"})
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["counts"]["status"], {"open": 1, "closed": 1})
        self.assertEqual(res["counts"]["priority"], {"p1": 1})
        self.assertEqual(res["counts"]["area"], {"Terrain": 1})
        self.assertEqual(res["counts"]["seed"], {"42": 1})
        self.assertEqual(res["counts"]["size"], {"S": 1})
        res = s.list_issues("mygame", {"size": "s", "milestone": "ALPHA", "seed": 42})
        self.assertEqual(res["counts"]["size"], {"S": 1, "L": 1})
        self.assertEqual(res["counts"]["milestone"], {"Alpha": 1})

    def test_search_and_facets_include_comments_literals_and_multiple_terms(self):
        s = self.store
        s.create_issue("mygame", {"title": "needle", "status": "open", "area": "terrain", "tags": ["lava"]})
        s.add_comment("MG-1", "agent", "token 100%_ \\ literal")
        s.create_issue("mygame", {"title": "needle token", "status": "closed", "area": "water", "source": "game"})
        s.create_issue("mygame", {"title": "needle token other", "tags": ["lavatory"]})
        self.assertEqual(s.list_issues("mygame", {"q": "100%_"})["total"], 1)
        self.assertEqual(s.list_issues("mygame", {"q": "\\ literal"})["total"], 1)
        res = s.list_issues("mygame", {"q": "needle token", "status": "open", "tag": "LAVA"})
        self.assertEqual([i["id"] for i in res["issues"]], ["MG-1"])
        self.assertEqual(res["counts"]["status"], {"open": 1})
        res = s.list_issues("mygame", {"q": "needle token", "source": "game"})
        self.assertEqual(res["counts"]["area"], {"water": 1})
        self.assertEqual(s.list_issues("mygame", {"q": "mg-1"})["total"], 1)

    def test_page_metadata_and_merges_preserve_counts(self):
        s = self.store
        for title in ("target", "source", "child", "other"):
            s.create_issue("mygame", {"title": title, "status": "open"})
        s.link_parent("MG-3", "MG-1")
        s.set_status("MG-3", "closed")
        s.add_comment("MG-2", "owner", "failed", "failed", [{"filename": "shot.png", "data_base64": PNG_B64}])
        s.add_comment("MG-1", "agent", "later plain comment")
        s.merge_issues("MG-1", ["MG-2"])
        res = s.list_issues("mygame", {"sort": "number", "limit": 1, "offset": 3})
        row = res["issues"][0]
        self.assertEqual(row["id"], "MG-1")
        self.assertEqual((row["comment_count"], row["attachment_count"], row["last_verdict"]), (2, 1, "failed"))
        self.assertEqual((row["child_count"], row["child_done"]), (1, 1))
        self.assertEqual(res["counts"]["merged"], 1)
        self.assertEqual(s.list_issues("mygame", {"merged": True, "status": "open"})["total"], 0)
        self.assertEqual(s.list_issues("mygame", {"merged": True, "status": "open"})["counts"]["merged"], 1)
        s.unmerge("MG-2")
        row = s.list_issues("mygame", {"sort": "number", "limit": 1, "offset": 3})["issues"][0]
        self.assertEqual((row["comment_count"], row["attachment_count"], row["last_verdict"]), (1, 0, None))

    def test_all_sorts_and_pagination_against_independent_keys(self):
        from pair_desk.store import TRIAGE_ORDER
        s = self.store
        for n in range(18):
            self.clock.advance(seconds=1)
            s.create_issue("mygame", {"title": str(n), "priority": ("p0", "p1", "p2")[n % 3],
                                     "status": ("open", "reported", "closed")[n % 3],
                                     "size": ("S", "M", "L", "")[n % 4], "area": ("", "Water", "terrain")[n % 3]})
        rows = s.list_issues("mygame", {"sort": "number"})["issues"]
        keys = {
            "triage": lambda i: (TRIAGE_ORDER.index(i["status"]), i["priority"], -i["number"]),
            "updated": lambda i: -i["number"], "created": lambda i: -i["number"],
            "oldest": lambda i: i["number"], "number": lambda i: -i["number"],
            "priority": lambda i: (i["priority"], -i["number"]),
            "backlog": lambda i: (i["priority"], ("S", "M", "L", "").index(i["size"]),
                                   i["area"] == "", i["area"].lower(), i["number"]),
        }
        for sort, key in keys.items():
            with self.subTest(sort=sort):
                expected = sorted(rows, key=key)[3:10]
                got = s.list_issues("mygame", {"sort": sort, "offset": 3, "limit": 7})
                self.assertEqual(got["total"], 18)
                self.assertEqual([r["id"] for r in got["issues"]], [r["id"] for r in expected])


class CacheTests(StoreCase):
    def test_read_only_current_schema_works_before_optional_indexes_exist(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        for sql in PERFORMANCE_INDEXES:
            s.conn.execute("DROP INDEX " + sql.split()[5])
        s.close()
        connect = sqlite3.connect
        def read_only(path, *args, **kwargs):
            if not kwargs.get("uri"):
                path = Path(path).resolve().as_uri() + "?mode=ro"
                kwargs["uri"] = True
            return connect(path, *args, **kwargs)
        with patch("pair_desk.store.sqlite3.connect", side_effect=read_only):
            with Store(self.tmp, clock=self.clock) as ro:
                self.assertEqual(ro.list_issues("mygame")["total"], 1)
                self.assertEqual(ro.get_issue("MG-1")["title"], "one")
                with self.assertRaises(sqlite3.OperationalError):
                    ro.add_comment("MG-1", "agent", "read only")
        with Store(self.tmp, clock=self.clock) as reopened:
            self.assertEqual(reopened.get_issue("MG-1")["comment_count"], 0)

    def test_contended_writer_retries_and_keeps_normal_statement_timeout(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        with Store(self.tmp, clock=self.clock) as other:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                with s._tx():
                    waiting = pool.submit(other.add_comment, "MG-1", "agent", "contended")
                    # The contender cannot commit until this transaction releases reservation.
                    with self.assertRaises(concurrent.futures.TimeoutError):
                        waiting.result(timeout=.05)
                result = waiting.result(timeout=5)
                self.assertEqual(result["comment"]["text"], "contended")
                self.assertEqual(other.conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)

    def test_contended_writer_deadline_leaves_connection_reusable(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        with Store(self.tmp, clock=self.clock) as other:
            other.WRITE_TIMEOUT = .03
            with s._tx():
                with self.assertRaises(sqlite3.OperationalError):
                    other.add_comment("MG-1", "agent", "blocked")
            self.assertEqual(other.conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(other.add_comment("MG-1", "agent", "retry")["comment"]["text"], "retry")

    def test_detail_and_mcp_caches_expire_commands_without_commits(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        s.queue_command("mygame", "/goto 1 2", "MG-1")
        mcp = McpServer(self.tmp)
        # Use the same controllable clock in both stores.
        mcp.store.clock = self.clock
        try:
            def get():
                return json.loads(mcp.call_tool("get_issue", {"id": "MG-1"})["content"][0]["text"])
            self.assertEqual(s.get_issue("MG-1")["commands"][0]["state"], "pending")
            self.assertEqual(get()["commands"][0]["state"], "pending")
            version = s._revision_conn.execute("PRAGMA data_version").fetchone()[0]
            self.clock.advance(minutes=10)
            self.assertEqual(s.get_issue("MG-1")["commands"][0]["state"], "expired")
            self.assertEqual(get()["commands"][0]["state"], "expired")
            self.assertEqual(s._revision_conn.execute("PRAGMA data_version").fetchone()[0], version)
            self.clock.advance(minutes=-1)
            self.assertEqual(s.get_issue("MG-1")["commands"][0]["state"], "pending")
        finally:
            mcp.store.close()

    def test_external_same_second_commits_invalidate_lists_and_details(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        before = s.list_issues("mygame")
        before["issues"][0]["title"] = "caller mutation"
        self.assertEqual(s.list_issues("mygame")["issues"][0]["title"], "one")
        s.get_issue("MG-1")
        s.project_overview("mygame")
        with Store(self.tmp, clock=self.clock) as other:
            other.add_comment("MG-1", "agent", "new searchable comment")
            other.update_issue("MG-1", {"title": "two", "area": "water"})
        self.assertEqual(s.list_issues("mygame")["issues"][0]["title"], "two")
        self.assertEqual(s.list_issues("mygame", {"q": "searchable"})["total"], 1)
        self.assertEqual(s.get_issue("MG-1")["comments"][0]["text"], "new searchable comment")
        self.assertEqual(s.project_overview("mygame")["areas"], ["water"])

    def test_detail_snapshot_is_consistent_and_never_cached_across_commit(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        with Store(self.tmp, clock=self.clock) as other:
            read = s._read
            fired = False
            def intercept(sql, params=()):
                nonlocal fired
                result = read(sql, params)
                if not fired:
                    fired = True
                    other.update_issue("MG-1", {"title": "two"})
                    other.add_comment("MG-1", "agent", "new")
                return result
            with patch.object(s, "_read", side_effect=intercept):
                # Explicit full has its own key, so this does not reuse create_issue's read.
                old = s.get_issue("MG-1", full=True)
            self.assertTrue(fired)
            self.assertEqual((old["title"], old["comment_count"]), ("one", 0))
            new = s.get_issue("MG-1")
            self.assertEqual((new["title"], new["comment_count"]), ("two", 1))

    def test_reader_does_not_wait_for_uncommitted_writer(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        entered, release = threading.Event(), threading.Event()
        def write():
            with s._tx() as c:
                c.execute("UPDATE issues SET title='two'")
                self.assertEqual(s._read("SELECT title FROM issues")[0][0], "two")
                entered.set()
                release.wait(10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(write)
            try:
                self.assertTrue(entered.wait(5))
                reader = pool.submit(s.get_issue, "MG-1")
                self.assertEqual(reader.result(timeout=5)["title"], "one")
            finally:
                release.set()
            writer.result(timeout=5)
        self.assertEqual(s.get_issue("MG-1")["title"], "two")

    def test_single_flight_bounded_cache_and_error_recovery(self):
        s = self.store
        barrier = threading.Barrier(20)
        calls = []
        def load():
            calls.append(1)
            return {"ok": True}
        def worker(_):
            barrier.wait()
            return s.cached_json(("same",), load)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            self.assertEqual(len(set(pool.map(worker, range(20)))), 1)
        self.assertEqual(len(calls), 1)
        s.CACHE_BYTES = 64
        s.CACHE_ENTRIES = 2
        for n in range(8):
            s.cached_json(("key", n), lambda: {"n": n})
        self.assertLessEqual(s._cache_bytes, 64)
        self.assertLessEqual(len(s._cache), 2)
        with self.assertRaises(ValueError):
            s.cached_json(("error",), lambda: (_ for _ in ()).throw(ValueError("bad")))
        self.assertEqual(json.loads(s.cached_json(("error",), load)), {"ok": True})

    def test_nested_http_and_store_cache_flights_do_not_hold_readers(self):
        s = self.store
        s.MAX_READERS = 1
        s.create_issue("mygame", {"title": "one"})
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            release.wait(5)
            return s.list_issues("mygame")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(s.cached_json, ("http",), load)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(s.list_issues, "mygame")
                self.assertEqual(second.result(timeout=5)["total"], 1)
            finally:
                release.set()
            self.assertEqual(json.loads(first.result(timeout=5))["total"], 1)

    def test_new_indexes_are_additive_and_reopening_does_not_write(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        before = s.export_project("mygame")
        revision = s.read_revision()
        with Store(self.tmp, clock=self.clock) as other:
            self.assertEqual(other.export_project("mygame"), before)
            names = {r["name"] for r in other._read("SELECT name FROM sqlite_master WHERE type='index'")}
            for sql in PERFORMANCE_INDEXES:
                self.assertIn(sql.split()[5], names)
            self.assertEqual(other._read("PRAGMA integrity_check")[0][0], "ok")
            self.assertEqual(other._read("PRAGMA foreign_key_check"), [])
        self.assertEqual(s.read_revision(), revision)

    def test_compact_mcp_comment_matches_full_response(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        s.add_comment("MG-1", "agent", "historical")
        mcp = McpServer(self.tmp)
        try:
            got = mcp.t_comment({"id": "MG-1", "text": "new", "author": "agent"})
            full = s.get_issue("MG-1")
            self.assertEqual(got["comment"], full["comments"][-1])
            self.assertEqual(got["issue"], _compact(full, False))
        finally:
            mcp.store.close()

    def test_mcp_read_text_cache_invalidates_after_external_comment(self):
        s = self.store
        s.create_issue("mygame", {"title": "one"})
        mcp = McpServer(self.tmp)
        try:
            def call(name, args):
                res = mcp.call_tool(name, args)
                self.assertFalse(res.get("isError"))
                return json.loads(res["content"][0]["text"])
            self.assertEqual(call("get_issue", {"id": "MG-1"})["comment_count"], 0)
            self.assertEqual(call("list_issues", {"project": "mygame"})["issues"][0]["comment_count"], 0)
            s.add_comment("MG-1", "agent", "external")
            self.assertEqual(call("get_issue", {"id": "MG-1"})["comment_count"], 1)
            self.assertEqual(call("list_issues", {"project": "mygame"})["issues"][0]["comment_count"], 1)
        finally:
            mcp.store.close()


class HttpCacheTests(ApiCase):
    def test_detail_etag_changes_when_command_expires(self):
        self.store.create_project("mygame", "MyGame", "MG")
        self.store.create_issue("mygame", {"title": "one"})
        self.store.queue_command("mygame", "/goto 1 2", "MG-1")
        _, first, res = self.req("GET", "/api/issues/MG-1")
        self.assertEqual(first["commands"][0]["state"], "pending")
        self.clock.advance(minutes=10)
        status, changed, _ = self.req("GET", "/api/issues/MG-1", headers={"If-None-Match": res.getheader("ETag")})
        self.assertEqual(status, 200)
        self.assertEqual(changed["commands"][0]["state"], "expired")

    def test_detail_cache_tracks_build_files_without_database_commit(self):
        self.store.create_project("mygame", "MyGame", "MG")
        self.store.create_issue("mygame", {"title": "one", "status": "to_check"})
        player = self.tmp / "player.exe"
        player.write_bytes(b"fixture")
        self.store.set_build("mygame", str(player))
        status, first, res = self.req("GET", "/api/issues/MG-1")
        self.assertEqual(status, 200)
        self.assertTrue(first["build"]["local"]["exists"])
        etag = res.getheader("ETag")
        player.unlink()
        status, changed, _ = self.req("GET", "/api/issues/MG-1", headers={"If-None-Match": etag})
        self.assertEqual(status, 200)
        self.assertFalse(changed["build"]["local"]["exists"])

    def test_list_etag_revalidates_after_external_write(self):
        self.store.create_project("mygame", "MyGame", "MG")
        self.store.create_issue("mygame", {"title": "one"})
        path = "/api/projects/mygame/issues"
        status, first, res = self.req("GET", path)
        etag = res.getheader("ETag")
        self.assertEqual(status, 200)
        self.assertTrue(etag)
        status, body, res = self.req("GET", path, headers={"If-None-Match": etag})
        self.assertEqual((status, body), (304, b""))
        self.assertIsNone(res.getheader("Content-Length"))
        self.assertEqual(res.getheader("Vary"), "Origin")
        with Store(self.tmp, clock=self.clock) as other:
            other.update_issue("MG-1", {"title": "two"})
        status, changed, res = self.req("GET", path, headers={"If-None-Match": etag})
        self.assertEqual(status, 200)
        self.assertEqual(changed["issues"][0]["title"], "two")
        self.assertNotEqual(res.getheader("ETag"), etag)

    def test_static_etag_head_and_security_headers(self):
        status, body, res = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        etag = res.getheader("ETag")
        status, data, res = self.req("HEAD", "/", headers={"If-None-Match": "W/" + etag})
        self.assertEqual((status, data), (304, b""))
        self.assertTrue(res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")

    def test_attachment_streaming_and_head_do_not_read_entire_file(self):
        self.store.create_project("mygame", "MyGame", "MG")
        issue = self.store.create_issue("mygame", {"title": "one", "attachments": [{"filename": "shot.png", "data_base64": PNG_B64}]})
        path = issue["attachments"][0]["url"]
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("unbounded read")):
            status, data, res = self.req("GET", path)
            self.assertEqual((status, data), (200, PNG))
            self.assertEqual(res.getheader("Content-Length"), str(len(PNG)))
            status, data, res = self.req("HEAD", path)
            self.assertEqual((status, data), (200, b""))
            self.assertEqual(res.getheader("Content-Length"), str(len(PNG)))


if __name__ == "__main__":
    unittest.main()
