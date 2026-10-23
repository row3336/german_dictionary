"""spec #2 step 3a — persisted VF-add job store (vf_add_jobs / vf_add_items)."""

import os
import sqlite3
import tempfile
import unittest

from bot.common.db_safety import (SCHEMA_VERSION, DestructiveMigrationRequired,
    database_fingerprint, get_user_version, set_user_version)
from bot.storage import init_db
from bot.verb_forms_parsing import job_store as JS


class VfAddJobStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def _tables(self):
        conn = sqlite3.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        return names

    def test_migration_creates_tables_and_bumps_version(self):
        self.assertIn("vf_add_jobs", self._tables())
        self.assertIn("vf_add_items", self._tables())
        self.assertGreaterEqual(SCHEMA_VERSION, 12)  # tables exist since v12

    def test_create_job_and_counts(self):
        jid = JS.create_job(self.db, user_id=7, chat_id=100, topic="05 Essen", words=["gehen", "Haus", "", "  "])
        job = JS.get_job(self.db, jid)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["topic"], "05 Essen")
        self.assertEqual(job["total"], 2)  # blanks dropped
        c = JS.job_counts(self.db, jid)
        self.assertEqual((c["total"], c["pending"], c["done"]), (2, 2, 0))

    def test_pending_flow_and_results(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen", "Haus"])
        first = JS.next_pending_item(self.db, jid)
        self.assertEqual(first["word"], "gehen")  # lowest position
        JS.save_item_result(self.db, first["id"], "done", result={"type": "verb", "infinitive": "gehen"}, flags="")
        second = JS.next_pending_item(self.db, jid)
        self.assertEqual(second["word"], "Haus")
        JS.save_item_result(self.db, second["id"], "error", error="fetch failed")
        self.assertIsNone(JS.next_pending_item(self.db, jid))
        c = JS.job_counts(self.db, jid)
        self.assertEqual((c["done"], c["error"], c["pending"]), (1, 1, 0))
        items = JS.list_items(self.db, jid)
        self.assertEqual(items[0]["result"]["infinitive"], "gehen")  # JSON round-trips
        self.assertEqual(items[1]["error"], "fetch failed")

    def test_explicit_additive_component_adds_type_hint_to_legacy_items_table(self):
        """Explicit additive normalization preserves a legacy row; startup stays guarded."""
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE vf_add_items")
        conn.execute(
            "CREATE TABLE vf_add_items (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, "
            "position INTEGER NOT NULL DEFAULT 0, word TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "result_json TEXT NOT NULL DEFAULT '', flags TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO vf_add_items (job_id, position, word) VALUES (1, 0, 'legacy')")
        conn.commit()
        conn.close()
        set_user_version(self.db, 0)
        before = database_fingerprint(self.db)
        with self.assertRaises(DestructiveMigrationRequired):
            init_db(self.db)
        self.assertEqual(database_fingerprint(self.db), before)
        init_db(self.db, _additive_only=True)
        self.assertEqual(get_user_version(self.db), 0)
        conn = sqlite3.connect(self.db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vf_add_items)")}
        row = conn.execute("SELECT word, type_hint FROM vf_add_items WHERE word='legacy'").fetchone()
        conn.close()
        self.assertIn("type_hint", cols)
        self.assertEqual(row, ("legacy", ""))  # old row preserved, hint defaults ''

    def test_create_job_stores_type_hint_pairs(self):
        # words may be bare strings (no hint) or (word, type_hint) pairs
        jid = JS.create_job(self.db, 7, 100, "t", [("aus", "praeposition"), "Haus"])
        items = {it["word"]: it for it in JS.list_items(self.db, jid)}
        self.assertEqual(items["aus"]["type_hint"], "praeposition")
        self.assertEqual(items["Haus"]["type_hint"], "")  # bare string -> no hint

    def test_create_recheck_job_carries_type_hint(self):
        # recheck triples (lookup, word_id, type) persist the type as the hint
        jid = JS.create_recheck_job(self.db, 7, 100, [("aus", "w1", "praeposition"), ("Haus", "w2")])
        items = {it["word"]: it for it in JS.list_items(self.db, jid)}
        self.assertEqual(items["aus"]["type_hint"], "praeposition")
        self.assertEqual(items["aus"]["word_id"], "w1")
        self.assertEqual(items["Haus"]["type_hint"], "")  # 2-tuple still works

    def test_status_and_active_jobs(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        self.assertEqual([j["id"] for j in JS.active_jobs(self.db)], [jid])
        JS.set_job_status(self.db, jid, "paused")
        self.assertIn(jid, [j["id"] for j in JS.active_jobs(self.db)])  # paused still active
        JS.set_job_status(self.db, jid, "done")
        self.assertEqual(JS.active_jobs(self.db), [])  # done is not active


if __name__ == "__main__":
    unittest.main()
