import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import bot.common.db_safety as db_safety
from bot.common.db_safety import (
    SCHEMA_VERSION,
    DestructiveMigrationRequired,
    apply_destructive_migrations_safely,
    dry_run_migration,
    get_user_version,
    prepare_storage_safely,
    set_user_version,
)
from bot.storage import init_db, select_due, update_progress


class PhraseMigrationV16Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "words.db")
        self.backups = os.path.join(self.tmp.name, "backups")
        init_db(self.db)
        set_user_version(self.db, 15)
        self.migration_day = date(2026, 8, 29)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO words (id, type, german, russian) VALUES (?, ?, ?, ?)",
                [
                    ("word", "nomen", "Haus", "дом"),
                    ("p_progress", "phrase", "Guten Morgen", "доброе утро"),
                    ("p_event", "phrase", "Bis bald", "до встречи"),
                    ("p_active", "phrase", "Wie geht es?", "как дела?"),
                    ("p_done", "phrase", "Vielen Dank", "спасибо"),
                ],
            )
            conn.executemany(
                """
                INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("word", "ru_de", 9, 4, 2.7, "2026-09-10"),
                    ("p_progress", "ru_de", 12, 7, 2.9, "2026-09-20"),
                    ("p_done", "de_ru", 5, 3, 2.6, "2026-09-03"),
                ],
            )
            conn.execute(
                "INSERT INTO learning_batches (id, user_id, target_qty, status) VALUES ('batch', 1, 5, 'active')"
            )
            conn.executemany(
                """
                INSERT INTO learning_items
                    (user_id, word_id, batch_id, step_index, preview_done, next_due, status)
                VALUES (1, ?, 'batch', 0, 0, '2026-08-29 00:00:00', ?)
                """,
                [("p_active", "active"), ("p_done", "completed")],
            )
            conn.executemany(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct) VALUES (1, ?, ?, ?)",
                [
                    ("word", "ru_de", 1),
                    ("p_progress", "learning", 1),
                    ("p_progress", "ru_de", 0),
                    ("p_event", "de_ru", 1),
                    # Learning-only activity does not make an active phrase affected.
                    ("p_active", "learning", 1),
                    ("p_done", "learning", 1),
                ],
            )
            rollback = {
                "snapshot": {
                    "user_id": 1,
                    "word_id": "p_progress",
                    "modes": ["ru_de"],
                    "progress": {"ru_de": {"word_id": "p_progress", "mode": "ru_de"}},
                    "event_counts": {"ru_de": 1},
                }
            }
            conn.execute(
                "INSERT INTO transaction_log (user_id, action, payload) VALUES (1, 'quiz:ru_de', ?)",
                (json.dumps(rollback),),
            )
            conn.execute(
                "INSERT INTO transaction_log (user_id, action, payload) VALUES (1, 'note', ?)",
                (json.dumps({"note": "p_progress"}),),
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, params).fetchall()

    def test_startup_blocks_pending_destructive_step(self):
        before = self._rows("SELECT * FROM progress ORDER BY word_id, mode")
        with self.assertRaises(DestructiveMigrationRequired):
            prepare_storage_safely(self.db, self.backups)
        self.assertEqual(get_user_version(self.db), 15)
        self.assertEqual(before, self._rows("SELECT * FROM progress ORDER BY word_id, mode"))
        self.assertFalse(Path(self.backups).exists())

    def test_dry_run_is_non_mutating_and_reports_exact_reset(self):
        before = self._rows("SELECT word_id, mode, interval, repetitions, ease, due FROM progress ORDER BY word_id, mode")
        report = dry_run_migration(self.db, today=self.migration_day)
        self.assertTrue(report["ok"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["steps"]["phrase_modes_v16"]["affected_phrases"], 3)
        self.assertEqual(report["steps"]["phrase_modes_v16"]["replaced_progress"], 2)
        self.assertEqual(report["steps"]["phrase_modes_v16"]["deleted_events"], 4)
        self.assertEqual(get_user_version(self.db), 15)
        self.assertEqual(before, self._rows("SELECT word_id, mode, interval, repetitions, ease, due FROM progress ORDER BY word_id, mode"))
        self.assertFalse(Path(self.backups).exists())

    def test_explicit_dispatcher_resets_only_affected_phrase_history(self):
        report = apply_destructive_migrations_safely(
            self.db, self.backups, today=self.migration_day
        )
        self.assertEqual(SCHEMA_VERSION, 16)
        self.assertEqual(get_user_version(self.db), 16)
        self.assertTrue(report["dry_run"]["ok"])
        snapshot = Path(report["backup"])
        self.assertTrue(snapshot.is_file())
        self.assertTrue(snapshot.name.startswith("snapshot-premigrate-v15-"))

        progress = self._rows(
            "SELECT word_id, mode, interval, repetitions, ease, due FROM progress ORDER BY word_id, mode"
        )
        self.assertIn(("word", "ru_de", 9, 4, 2.7, "2026-09-10"), progress)
        expected_due = (self.migration_day + timedelta(days=1)).isoformat()
        self.assertIn(("p_progress", "phrase_ru_de", 1, 1, 2.5, expected_due), progress)
        self.assertIn(("p_done", "phrase_de_ru", 1, 1, 2.5, expected_due), progress)
        self.assertFalse(any(row[0].startswith("p_") and row[1] in {"ru_de", "de_ru"} for row in progress))
        due = select_due(
            self.db,
            user_id=1,
            mode="phrase_ru_de",
            today=self.migration_day + timedelta(days=1),
        )
        self.assertIn("p_progress", [row["id"] for row in due])

        events = self._rows("SELECT word_id, mode FROM quiz_events ORDER BY id")
        self.assertIn(("word", "ru_de"), events)
        self.assertIn(("p_active", "learning"), events)
        self.assertNotIn(("p_progress", "learning"), events)
        self.assertNotIn(("p_done", "learning"), events)
        self.assertNotIn(("p_event", "de_ru"), events)

        learning = dict(self._rows("SELECT word_id, status FROM learning_items"))
        self.assertEqual(learning, {"p_active": "active", "p_done": "completed"})
        payloads = [json.loads(row[0]) for row in self._rows("SELECT payload FROM transaction_log ORDER BY id")]
        self.assertEqual(payloads, [{"note": "p_progress"}])

    def test_rerun_is_noop_and_does_not_overwrite_new_progress(self):
        apply_destructive_migrations_safely(self.db, self.backups, today=self.migration_day)
        update_progress(self.db, 1, "p_progress", "phrase_ru_de", quality=5)
        before = self._rows(
            "SELECT interval, repetitions, ease, due FROM progress WHERE word_id='p_progress' AND mode='phrase_ru_de'"
        )
        self.assertEqual(before[0][0:2], (3, 2))
        again = apply_destructive_migrations_safely(self.db, self.backups, today=self.migration_day)
        self.assertFalse(again["migrated"])
        self.assertIsNone(again["backup"])
        self.assertEqual(before, self._rows(
            "SELECT interval, repetitions, ease, due FROM progress WHERE word_id='p_progress' AND mode='phrase_ru_de'"
        ))

    def test_dry_run_result_is_rejected_if_source_changes_after_snapshot(self):
        real_backup = db_safety.backup_db_before_destructive_migrate

        def backup_then_change(*args, **kwargs):
            snapshot = real_backup(*args, **kwargs)
            with closing(sqlite3.connect(self.db)) as conn:
                conn.execute(
                    "INSERT INTO transaction_log (user_id, action, payload) VALUES (1, 'late', '{}')"
                )
                conn.commit()
            return snapshot

        before = self._rows("SELECT * FROM progress ORDER BY word_id, mode")
        with (
            patch(
                "bot.common.db_safety.backup_db_before_destructive_migrate",
                side_effect=backup_then_change,
            ),
            self.assertRaisesRegex(RuntimeError, "source DB changed"),
        ):
            apply_destructive_migrations_safely(self.db, self.backups, today=self.migration_day)
        self.assertEqual(get_user_version(self.db), 15)
        self.assertEqual(before, self._rows("SELECT * FROM progress ORDER BY word_id, mode"))

    def test_reset_and_version_bump_roll_back_together_on_failure(self):
        original_step = db_safety._migrate_phrase_modes_v16

        def migrate_then_fail(conn, today=None):
            original_step(conn, today=today)
            raise RuntimeError("injected failure")

        before_progress = self._rows("SELECT * FROM progress ORDER BY word_id, mode")
        before_events = self._rows("SELECT * FROM quiz_events ORDER BY id")
        with (
            patch("bot.common.db_safety._migrate_phrase_modes_v16", side_effect=migrate_then_fail),
            self.assertRaisesRegex(RuntimeError, "injected failure"),
        ):
            db_safety._apply_migration_dispatcher(self.db, today=self.migration_day)
        self.assertEqual(get_user_version(self.db), 15)
        self.assertEqual(before_progress, self._rows("SELECT * FROM progress ORDER BY word_id, mode"))
        self.assertEqual(before_events, self._rows("SELECT * FROM quiz_events ORDER BY id"))

    def test_count_validator_failure_rolls_back_data_and_version(self):
        before_progress = self._rows("SELECT * FROM progress ORDER BY word_id, mode")
        before_events = self._rows("SELECT * FROM quiz_events ORDER BY id")
        with (
            patch(
                "bot.common.db_safety._validate_destructive_counts",
                side_effect=RuntimeError("injected count mismatch"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected count mismatch"),
        ):
            db_safety._apply_migration_dispatcher(self.db, today=self.migration_day)
        self.assertEqual(get_user_version(self.db), 15)
        self.assertEqual(before_progress, self._rows("SELECT * FROM progress ORDER BY word_id, mode"))
        self.assertEqual(before_events, self._rows("SELECT * FROM quiz_events ORDER BY id"))

    def test_persistent_snapshot_name_is_outside_normal_cleanup_globs(self):
        report = apply_destructive_migrations_safely(
            self.db, self.backups, today=self.migration_day
        )
        name = Path(report["backup"]).name
        self.assertFalse(Path(name).match("words-*.db"))
        self.assertFalse(Path(name).match("bot-logs-*.txt"))

    def test_v14_normalizes_to_exact_v15_before_v16_step(self):
        set_user_version(self.db, 14)
        observed_versions = []
        original_step = db_safety._migrate_phrase_modes_v16

        def observe_version(conn, today=None):
            observed_versions.append(int(conn.execute("PRAGMA user_version").fetchone()[0]))
            return original_step(conn, today=today)

        with patch("bot.common.db_safety._migrate_phrase_modes_v16", side_effect=observe_version):
            report = apply_destructive_migrations_safely(
                self.db, self.backups, today=self.migration_day
            )

        # Once on the dry-run copy and once on live, both after the additive step.
        self.assertEqual(observed_versions, [15, 15])
        self.assertEqual(report["from_version"], 14)
        self.assertEqual(get_user_version(self.db), 16)
        progress = self._rows(
            "SELECT word_id, mode FROM progress WHERE word_id='p_progress' ORDER BY mode"
        )
        self.assertEqual(progress, [("p_progress", "phrase_ru_de")])


if __name__ == "__main__":
    unittest.main()
