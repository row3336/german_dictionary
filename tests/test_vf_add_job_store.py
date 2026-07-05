"""spec #2 step 3a — persisted VF-add job store (vf_add_jobs / vf_add_items)."""

import os
import sqlite3
import tempfile
import unittest

from bot.common.db_safety import SCHEMA_VERSION
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
        self.assertEqual(SCHEMA_VERSION, 11)

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

    def test_status_and_active_jobs(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        self.assertEqual([j["id"] for j in JS.active_jobs(self.db)], [jid])
        JS.set_job_status(self.db, jid, "paused")
        self.assertIn(jid, [j["id"] for j in JS.active_jobs(self.db)])  # paused still active
        JS.set_job_status(self.db, jid, "done")
        self.assertEqual(JS.active_jobs(self.db), [])  # done is not active


if __name__ == "__main__":
    unittest.main()
