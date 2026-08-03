import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.common.db_safety import SCHEMA_VERSION
from bot.storage import init_db, set_setting, set_user_setting
from scripts.reconcile_book_topics import (
    DESCRIPTIONS,
    MERGES,
    ReconciliationError,
    reconcile_database,
)


class ReconcileBookTopicsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "words.db"
        init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("DELETE FROM topics")
            conn.execute("DELETE FROM words")
            for name in sorted(set(DESCRIPTIONS) | set(MERGES)):
                conn.execute(
                    "INSERT INTO topics(name, description, level) VALUES (?, '', '')", (name,)
                )
            for topic, qty in {"01": 2, "1": 1, "02": 3, "4": 2, "04": 4, "6": 3}.items():
                for index in range(qty):
                    conn.execute(
                        "INSERT INTO words(id, german, type, russian, topic) VALUES (?, ?, 'nomen', 'x', ?)",
                        (f"{topic}-{index}", f"W{topic}-{index}", topic),
                    )
            conn.execute(
                "INSERT INTO vf_add_jobs(user_id, topic, status) VALUES (1, '02', 'done')"
            )
        set_setting(self.db, "topic_filter", "01+2+04")
        set_user_setting(self.db, 7, "topic_filter", "02,4,01")

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT * FROM words ORDER BY id").fetchall(), conn.execute(
                "SELECT * FROM topics ORDER BY name"
            ).fetchall()

    def test_dry_run_does_not_mutate(self):
        before = self._snapshot()
        report = reconcile_database(self.db)
        self.assertFalse(report["applied"])
        self.assertEqual(report["word_count_before"], report["word_count_after"])
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.root / "backups").exists())

    def test_apply_merges_metadata_and_references(self):
        report = reconcile_database(self.db, apply=True)
        self.assertTrue(report["applied"])
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(report["topic_count_before"], 16)
        self.assertEqual(report["topic_count_after"], 13)
        self.assertEqual(report["word_counts_after"]["1"], 3)
        self.assertEqual(report["word_counts_after"]["4"], 5)
        self.assertEqual(report["word_counts_after"]["6"], 7)
        with sqlite3.connect(self.db) as conn:
            topics = {
                name: (description, level)
                for name, description, level in conn.execute(
                    "SELECT name, description, level FROM topics"
                )
            }
            self.assertEqual(set(topics), set(DESCRIPTIONS))
            self.assertEqual(topics["012"], ("Endlich Frühling", "A1"))
            self.assertEqual(topics["7"], ("Arbeit und Beruf", "A1"))
            self.assertEqual(topics["nom"], ("Nomen+", "A1"))
            self.assertEqual(topics["verb"], ("Verben+", "A1"))
            self.assertEqual(
                conn.execute("SELECT topic FROM vf_add_jobs").fetchone()[0], "4"
            )
            self.assertEqual(
                conn.execute("SELECT value FROM app_settings WHERE key='topic_filter'").fetchone()[0],
                "1+2+6",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM user_settings WHERE user_id=7 AND key='topic_filter'"
                ).fetchone()[0],
                "4+1",
            )

    def test_second_apply_is_idempotent(self):
        reconcile_database(self.db, apply=True)
        report = reconcile_database(self.db, apply=True)
        self.assertEqual(sum(item["words"] for item in report["moved"].values()), 0)
        self.assertEqual(report["topic_count_before"], 13)
        self.assertEqual(report["topic_count_after"], 13)

    def test_refuses_non_a1_level(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='B1' WHERE name='03'")
        with self.assertRaisesRegex(ReconciliationError, "unexpected level"):
            reconcile_database(self.db, apply=True)
        self.assertFalse((self.root / "backups").exists())

    def test_refuses_conflicting_description(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE topics SET description='Other' WHERE name='7'")
        with self.assertRaisesRegex(ReconciliationError, "conflicts"):
            reconcile_database(self.db)


if __name__ == "__main__":
    unittest.main()
