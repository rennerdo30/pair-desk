"""Ranked full-text search (the issue_search index): matching, order, excerpts, upkeep, fallbacks."""

import sqlite3
import unittest

from helpers import StoreCase
from test_mcp_cli import McpCase

from pair_desk.store import SEARCH_TRIGGER_NAMES, Store, fts_query, search_terms


def ids(res):
    return [i["id"] for i in res["issues"]]


class SearchTests(StoreCase):
    def setUp(self):
        super().setUp()
        s = self.store
        s.create_issue("mygame", {"title": "Worker threads stall the frame", "kind": "bug", "area": "perf"})  # MG-1
        s.create_issue("mygame", {"title": "Villagers idle at noon", "body": "The workers stand still.",
                                  "kind": "bug", "area": "npc"})  # MG-2
        s.create_issue("mygame", {"title": "Dropdown menu cut off", "kind": "bug", "area": "ui",
                                  "location": {"commands": [{"command": "/goto camp", "label": "the camp"}]}})  # MG-3
        s.add_comment("MG-3", "claude", "The worker pool was busy while the menu opened.")
        s.create_issue("mygame", {"title": "Items drop through the floor", "kind": "bug"})  # MG-4

    def search(self, **filters):
        return self.store.list_issues("mygame", filters)

    def test_index_is_ready(self):
        self.assertTrue(self.store.search_ready)

    def test_title_hits_rank_above_body_and_comments(self):
        res = self.search(q="worker")
        self.assertEqual(res["search"], {"mode": "ranked"})
        self.assertEqual(ids(res), ["MG-1", "MG-2", "MG-3"])  # stemmed: workers matches worker
        self.assertEqual([i["match"]["field"] for i in res["issues"]], ["title", "body", "comments"])
        self.assertEqual(res["issues"][0]["match"]["excerpt"], "**Worker** threads stall the frame")
        self.assertEqual(res["total"], 3)

    def test_whole_words_not_substrings(self):
        self.assertEqual(ids(self.search(q="drop")), ["MG-4"])  # not "Dropdown"
        # With no whole-word hit at all, words match as prefixes.
        self.assertEqual(ids(self.search(q="dropdo")), ["MG-3"])
        self.assertEqual(set(ids(self.search(q="drop*"))), {"MG-3", "MG-4"})

    def test_substring_fallback_finds_parts_of_code_names(self):
        self.store.create_issue("mygame", {"title": "WorldStore flush is slow"})
        res = self.search(q="Store")
        self.assertEqual(res["search"], {"mode": "substring"})
        self.assertEqual(ids(res), ["MG-5"])
        self.assertNotIn("match", res["issues"][0])

    def test_every_word_must_match(self):
        self.assertEqual(ids(self.search(q="worker menu")), ["MG-3"])
        self.assertEqual(ids(self.search(q='"worker pool"')), ["MG-3"])
        self.assertEqual(ids(self.search(q='"pool worker"')), [])

    def test_details_cover_id_area_and_location(self):
        self.assertEqual(ids(self.search(q="MG-2")), ["MG-2"])
        self.assertEqual(ids(self.search(q="camp")), ["MG-3"])
        self.assertEqual(self.search(q="camp")["issues"][0]["match"]["field"], "details")
        self.assertEqual(ids(self.search(q="perf")), ["MG-1"])

    def test_boost_area_ranks_that_area_first(self):
        self.assertEqual(ids(self.search(q="worker", boost_area="npc"))[0], "MG-2")
        self.assertEqual(ids(self.search(q="worker", boost_area="nowhere"))[0], "MG-1")

    def test_filters_counts_and_paging_with_ranking(self):
        self.store.set_status("MG-1", "closed", actor="claude")
        res = self.search(q="worker", status="reported,open,to_check")
        self.assertEqual(ids(res), ["MG-2", "MG-3"])
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["counts"]["status"].get("closed"), 1)  # the facet ignores its own filter
        self.assertEqual(ids(self.search(q="worker", limit=1, offset=1)), ["MG-2"])

    def test_explicit_sort_keeps_its_order(self):
        self.clock.advance(minutes=5)
        self.store.update_issue("MG-3", {"priority": "p0"}, actor="claude")
        res = self.search(q="worker", sort="priority")
        self.assertEqual(ids(res)[0], "MG-3")
        self.assertEqual(res["search"], {"mode": "ranked"})

    def test_index_follows_edits_comments_merges_and_deletes(self):
        s = self.store
        s.update_issue("MG-1", {"title": "Job system stalls"}, actor="claude")
        self.assertEqual(ids(self.search(q="threads")), [])
        self.assertEqual(ids(self.search(q="job")), ["MG-1"])
        s.add_comment("MG-4", "owner", "Seen near the lighthouse")
        self.assertEqual(ids(self.search(q="lighthouse")), ["MG-4"])
        s.merge_issues("MG-1", ["MG-4"], actor="claude")  # comments move to the target
        self.assertIn("MG-1", ids(self.search(q="lighthouse")))
        s.unmerge("MG-4", actor="claude")
        self.assertEqual(ids(self.search(q="lighthouse")), ["MG-4"])
        s.delete_issue("MG-4", actor="owner")
        self.assertEqual(ids(self.search(q="lighthouse")), [])

    def test_query_syntax_never_errors(self):
        for q in ['"', 'a AND', 'NEAR(', '*', '-x', '%', 'worker OR', '(', "SAVE_QUIT", "^col:x", "''",
                  "worker\x00", "\x00", "worker _", "-_-"]:
            with self.subTest(q=q):
                self.assertIn("issues", self.search(q=q))
        self.assertEqual(ids(self.search(q="worker\x00")), ["MG-1", "MG-2", "MG-3"])
        self.assertEqual(ids(self.search(q="worker _")), ["MG-1", "MG-2", "MG-3"])

    def test_filtered_out_word_hits_do_not_hide_substring_hits(self):
        s = self.store
        s.create_issue("mygame", {"title": "Store closed thing", "status": "closed"})  # MG-5
        s.create_issue("mygame", {"title": "WorldStore crash", "status": "open"})  # MG-6
        res = self.search(q="store", status="open")
        self.assertEqual((ids(res), res["search"]), (["MG-6"], {"mode": "substring"}))
        res = self.search(q="store", status="closed")
        self.assertEqual((ids(res), res["search"]), (["MG-5"], {"mode": "ranked"}))

    def test_stale_index_is_not_used(self):
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE meta SET value='stale' WHERE key='search_index'")
        conn.commit()
        conn.close()
        res = self.search(q="workers")
        self.assertEqual(res["search"], {"mode": "substring"})
        self.assertEqual(ids(res), ["MG-2"])

    def test_terms_and_query(self):
        self.assertEqual(search_terms('frame "worker pool" drop* -- %'),
                         [("frame", False), ("worker pool", False), ("drop", True)])
        self.assertEqual(search_terms('"frame dr"* _ a\x00b'), [("frame dr", True), ("ab", False)])
        self.assertEqual(fts_query([("say \"hi\"", False), ("dro", True)]), '"say ""hi""" AND "dro"*')
        self.assertEqual(fts_query([("a", False)], prefix_all=True), '"a"*')
        self.assertEqual(fts_query([("frame", False), ("drop", False)]), '("frame" AND "drop") OR "frame drop"')

    def test_words_side_by_side_rank_first(self):
        s = self.store
        s.create_issue("mygame", {"title": "Frame pacing", "body": "The camera bob can drop a little.", "kind": "bug"})
        s.create_issue("mygame", {"title": "Particles", "body": "Every explosion causes a frame drop.", "kind": "bug"})
        res = self.search(q="frame drop")
        self.assertEqual(ids(res), ["MG-6", "MG-5"])  # the phrase beats a title hit on one word
        self.assertEqual(res["total"], 2)

    def test_existing_desk_is_indexed_on_open(self):
        conn = sqlite3.connect(self.store.db_path)
        for name in SEARCH_TRIGGER_NAMES:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE issue_search")
        conn.execute("UPDATE meta SET value='stale' WHERE key='search_index'")
        conn.commit()
        conn.close()
        self.store.close()
        self.store = Store(self.tmp, clock=self.clock)
        self.assertTrue(self.store.search_ready)
        self.assertEqual(ids(self.search(q="worker")), ["MG-1", "MG-2", "MG-3"])

    def test_without_the_index_search_uses_substrings(self):
        self.store.search_ready = False
        res = self.search(q="worker")
        self.assertEqual(res["search"], {"mode": "substring"})
        self.assertEqual(set(ids(res)), {"MG-1", "MG-2", "MG-3"})

    def test_no_query_has_no_search_block(self):
        self.assertNotIn("search", self.search(sort="relevance"))


class SearchToolTests(McpCase):
    def test_search_tool(self):
        with Store(self.tmp) as s:
            s.create_issue("mygame", {"title": "Frame time spikes in the forest", "kind": "bug", "area": "perf"})
            s.create_issue("mygame", {"title": "Tree bark texture", "body": "Spikes on the bark look wrong", "kind": "bug"})
        err, res = self.call(1, "search", {"q": "spike", "limit": 5})
        self.assertFalse(err)
        self.assertEqual((res["mode"], res["total"]), ("ranked", 2))
        self.assertEqual([i["id"] for i in res["issues"]], ["MG-1", "MG-2"])
        self.assertEqual(res["issues"][1]["match"], {"field": "body", "excerpt": "**Spikes** on the bark look wrong"})
        err, msg = self.call(2, "search", {"q": "  "})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
