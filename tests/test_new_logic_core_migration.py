"""Retained synthetic fixtures for v15/v16/v17 -> v18 and rollback."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.common import db_safety
from bot.storage import init_db
from bot.study import repository as repo, schema_v18
from tests.test_reflexive_db19 import old_schema

STAND = Path("/Users/wf/py/_agent_work/888_german_dictionary/k12_impl_20261023/regression_migration")


class Migration18Test(unittest.TestCase):
    def make_db(self, version=17):
        STAND.mkdir(parents=True, exist_ok=True)
        db = str(Path(tempfile.mkdtemp(prefix=f"migration-{version}-", dir=STAND)) / "synthetic.sqlite")
        init_db(db)
        old_schema(db, version)
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO users(user_id,status) VALUES (1,'approved')")
            conn.executemany("INSERT INTO words(id,type,german,infinitive) VALUES (?,'verb',?,?)",
                             [("a", "haben", "haben"), ("b", "sein", "sein"), ("c", "werden", "werden")])
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES ('batch',1)")
            conn.executemany("""INSERT INTO learning_items
                (user_id,word_id,batch_id,preview_done,step_index,next_due,status)
                VALUES (1,?,'batch',?,?,?,?)""",
                [("a", 0, 0, "2026-10-20T18:00:00", "active"),
                 ("b", 1, 2, "2026-10-22T09:00:00", "active"),
                 ("c", 0, 0, "2026-10-21T11:00:00", "completed")])
            conn.executemany("""INSERT INTO progress
                (user_id,word_id,mode,interval,repetitions,ease,due,updated_at)
                VALUES (1,?,?,?,?,?,?,'old-exact-time')""",
                [("a", "ru_de", 9, 2, 2.65, "2026-10-29"),
                 ("b", "de_ru", 30, 8, 3.1, "2026-11-19"),
                 ("c", "uk_de", 40, 1, 2.9, "2026-11-29")])
            conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES (1,'a','ru_de',0)")
            legacy = {"snapshot": {"user_id": 1, "word_id": "a", "modes": ["ru_de"],
                                    "progress": {"ru_de": None}, "learning_item": None,
                                    "event_counts": {"ru_de": 0}}}
            conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES (1,'quiz:ru_de',?)",
                         (json.dumps(legacy),))
        return db

    def contents(self, db, tables):
        with repo.transaction(db) as conn:
            result = {}
            for table in tables:
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                           if r[1] not in {"reflexive_case", "study_generation"}]
                result[table] = [tuple(row) for row in conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid")]
            return result

    def test_17_to_18_preserves_exact_historical_rows_and_seeds_only_known_admissions(self):
        db = self.make_db()
        before = self.contents(db, ["words", "progress", "quiz_events", "transaction_log", "conjugation_progress"])
        learning_before = self.contents(db, ["learning_items"])["learning_items"]
        report = db_safety._apply_migration_dispatcher(db)
        self.assertEqual(report["from_version"], 17)
        self.assertEqual(report["to_version"], 19)
        self.assertEqual(set(report["steps"]), {"study_v18", "reflexive_v19"})
        self.assertEqual(self.contents(db, before), before)
        with repo.transaction(db) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM learning_items ORDER BY word_id")]
            self.assertEqual([r["first_due_pending"] for r in rows], [1, 0, 0])
            self.assertEqual([r["recovery_remaining"] for r in rows], [0, 0, 0])
            self.assertEqual([tuple(r)[:len(learning_before[0])] for r in
                              conn.execute("SELECT * FROM learning_items ORDER BY word_id")], learning_before)
            self.assertEqual([tuple(r)[:3] for r in conn.execute("SELECT * FROM conjugation_admissions")],
                             [(1, "ru_de", "a")])
        self.assertTrue(db_safety.run_healthcheck(db)["ok"])
        self.assertFalse(db_safety._apply_migration_dispatcher(db)["migrated"])
        self.assertEqual(self.contents(db, before), before)

    def test_v15_v16_v17_chain_observes_literal_17(self):
        for source in (15, 16, 17):
            with self.subTest(source=source):
                db = self.make_db(source)
                seen = []
                real = schema_v18.migrate

                def observe(conn, *args, **kwargs):
                    seen.append(conn.execute("PRAGMA user_version").fetchone()[0])
                    return real(conn, *args, **kwargs)

                with patch.object(schema_v18, "migrate", side_effect=observe):
                    report = db_safety._apply_migration_dispatcher(db)
                self.assertEqual(seen, [17])
                self.assertEqual(list(report["steps"]),
                                 (["phrase_modes_v16"] if source == 15 else []) +
                                 (["remove_weak_v17"] if source <= 16 else []) + ["study_v18", "reflexive_v19"])
                self.assertTrue(report["healthcheck"]["ok"])

    def test_v18_failure_rolls_back_schema_marker_and_old_data(self):
        db = self.make_db()
        before = self.contents(db, ["words", "progress", "quiz_events", "transaction_log", "learning_items"])
        real = schema_v18.migrate

        def fail_after_ddl(conn):
            real(conn)
            raise RuntimeError("injected schema18 fault")

        with patch.object(schema_v18, "migrate", side_effect=fail_after_ddl):
            with self.assertRaisesRegex(RuntimeError, "schema18 fault"):
                db_safety._apply_migration_dispatcher(db)
        self.assertEqual(db_safety.get_user_version(db), 17)
        self.assertEqual(self.contents(db, before), before)
        with repo.transaction(db) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='study_actions'").fetchone())
            self.assertNotIn("recovery_remaining", {r[1] for r in conn.execute("PRAGMA table_info(learning_items)")})

    def test_v18_fault_also_rolls_back_prior_legacy_steps(self):
        db = self.make_db(15)
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES (1,'a','weak_test',1,0,2.5,'2026-10-20')")
        before = self.contents(db, ["progress", "quiz_events", "transaction_log"])
        with patch.object(schema_v18, "migrate", side_effect=RuntimeError("fault18")):
            with self.assertRaisesRegex(RuntimeError, "fault18"):
                db_safety._apply_migration_dispatcher(db)
        self.assertEqual(db_safety.get_user_version(db), 15)
        self.assertEqual(self.contents(db, before), before)

    def test_startup_blocks_old_marker_and_future_schema(self):
        db = self.make_db()
        with self.assertRaises(db_safety.DestructiveMigrationRequired):
            db_safety.prepare_storage_safely(db, str(Path(db).parent / "backups"))
        with repo.transaction(db) as conn:
            conn.execute("PRAGMA user_version=20")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            db_safety._apply_migration_dispatcher(db)
        with self.assertRaisesRegex(RuntimeError, "newer"):
            init_db(db)

    def test_fresh_schema_matches_migrated_objects_and_v18_health_requires_objects(self):
        db = self.make_db()
        db_safety._apply_migration_dispatcher(db)
        fresh = str(Path(tempfile.mkdtemp(prefix="fresh-", dir=STAND)) / "synthetic.sqlite")
        db_safety.prepare_storage_safely(fresh, str(Path(fresh).parent / "backups"))
        with repo.transaction(db) as old, repo.transaction(fresh) as new:
            for table in ("learning_items", "study_actions", "study_deliveries", "conjugation_admissions",
                          "conjugation_blocks", "conjugation_passes", "conjugation_answers"):
                self.assertEqual([tuple(r)[1:] for r in old.execute(f"PRAGMA table_info({table})")],
                                 [tuple(r)[1:] for r in new.execute(f"PRAGMA table_info({table})")])
            new.execute("DROP TABLE study_actions")
        self.assertFalse(db_safety.run_healthcheck(fresh)["ok"])

    def test_legacy_undo_normalizes_learning_flags_without_resurrecting_batch(self):
        db = self.make_db()
        db_safety._apply_migration_dispatcher(db)
        with repo.transaction(db) as conn:
            row = dict(conn.execute("SELECT * FROM learning_items WHERE word_id='a'").fetchone())
            del row["recovery_remaining"], row["first_due_pending"]
            snapshot = {"user_id": 1, "word_id": "a", "modes": [], "learning_item": row}
            conn.execute("UPDATE learning_items SET first_due_pending=0,recovery_remaining=3 WHERE word_id='a'")
            repo.restore_snapshot(conn, snapshot)
            restored = conn.execute("SELECT recovery_remaining,first_due_pending FROM learning_items WHERE word_id='a'").fetchone()
            self.assertEqual(tuple(restored), (0, 1))
            conn.execute("DELETE FROM learning_batches WHERE id='batch'")
            repo.restore_snapshot(conn, snapshot)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM learning_items").fetchone()[0], 0)


    def test_legacy_phrase_and_weak_transformations_keep_their_expected_diff(self):
        from datetime import date
        db = self.make_db(15)
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO words(id,type,german) VALUES ('p','phrase','Guten Tag')")
            conn.executemany("""INSERT INTO progress
                (user_id,word_id,mode,interval,repetitions,ease,due)
                VALUES (1,?,?,12,7,2.9,'2026-11-01')""", [("p", "ru_de"), ("a", "weak_test")])
            conn.executemany("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES (1,?,?,0)",
                             [("p", "ru_de"), ("p", "learning"), ("a", "weak_test")])
            conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES (1,'quiz:weak_test','{}')")
            conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES (1,'weak_drill_gap','3')")
        report = db_safety._apply_migration_dispatcher(db, today=date(2026, 10, 20))
        self.assertEqual(report["steps"]["phrase_modes_v16"]["deleted_events"], 2)
        self.assertEqual(report["steps"]["remove_weak_v17"]["deleted_events"], 1)
        with repo.transaction(db) as conn:
            phrase = conn.execute("SELECT mode,interval,repetitions,ease,due FROM progress WHERE word_id='p'").fetchone()
            self.assertEqual(tuple(phrase), ("phrase_ru_de", 1, 1, 2.5, "2026-10-21"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM progress WHERE mode GLOB 'weak_*'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events WHERE word_id='p'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_settings WHERE key='weak_drill_gap'").fetchone()[0], 0)
        self.assertTrue(report["healthcheck"]["ok"])

    def test_changed_fingerprint_refuses_migration_before_ddl(self):
        db = self.make_db()
        fingerprint = db_safety.database_fingerprint(db)
        with repo.transaction(db) as conn:
            conn.execute("UPDATE words SET german='changed' WHERE id='a'")
        with self.assertRaisesRegex(RuntimeError, "source DB changed"):
            db_safety._apply_migration_dispatcher(db, expected_fingerprint=fingerprint)
        self.assertEqual(db_safety.get_user_version(db), 17)
        with repo.transaction(db) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='study_actions'").fetchone())


if __name__ == "__main__":
    unittest.main()
