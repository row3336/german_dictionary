import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import bot.common.db_safety as db_safety
from bot.common.db_safety import (
    SCHEMA_VERSION,
    apply_destructive_migrations_safely,
    database_fingerprint,
    dry_run_migration,
    get_user_version,
    set_user_version,
)
from bot.storage import add_word, init_db, restore_user_word_state
from tests.test_db_safety import _create_additive_fixture


def _has_removed_weak_surface(source):
    # Permit only the exact stdlib import and qualified dictionary constructor.
    # Every other weak token and all removed schema/runtime names stay forbidden.
    source = re.sub(r"(?m)^import weakref[ \t]*$", "", source.lower())
    source = re.sub(r"\bweakref\.weakvaluedictionary\b", "", source)
    return any(token in source for token in ("weak", "form_stats", "review_stage_snapshot"))


def _schema(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        columns = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
        indexes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='quiz_events' AND sql IS NOT NULL"
            )
        }
    return tables, columns, indexes


class FreshSchemaWithoutWeakTest(unittest.TestCase):
    def test_fresh_database_has_no_weak_schema(self):
        directory = tempfile.mkdtemp(prefix="retained-weak-")
        db = os.path.join(directory, "words.db")
        init_db(db)
        tables, columns, indexes = _schema(db)
        with closing(sqlite3.connect(db)) as conn:
            setting_keys = {
                row[0]
                for table in ("user_settings", "app_settings")
                for row in conn.execute(f"SELECT key FROM {table}")
            }

        self.assertNotIn("form_stats", tables)
        self.assertNotIn("review_stage_snapshot", columns["quiz_events"])
        self.assertFalse(any("form_stats" in name for name in indexes))
        self.assertTrue({"weak_drill_gap", "weak_drill_repeats"}.isdisjoint(setting_keys))

    def test_old_snapshot_form_stats_payload_is_ignored(self):
        directory = tempfile.mkdtemp(prefix="retained-weak-")
        db = os.path.join(directory, "words.db")
        init_db(db)
        word_id = add_word(
            db,
            {
                "type": "nomen",
                "article": "das",
                "german": "Haus",
                "plural": "Häuser",
                "russian": "дом",
            },
        )["id"]
        snapshot = {
            "user_id": 7,
            "word_id": word_id,
            "modes": ["ru_de"],
            "progress": {"ru_de": None},
            "form_stats": {
                "user_id": 7,
                "word_id": word_id,
                "article_errors": 2,
            },
            "learning_item": None,
            "event_counts": {"ru_de": 0},
        }
        self.assertTrue(restore_user_word_state(db, snapshot))
        self.assertNotIn("form_stats", _schema(db)[0])

    def test_ru_de_handler_has_no_form_stats_call(self):
        source = (Path(__file__).parents[1] / "bot" / "handlers" / "quiz.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("form_stats", source)
        self.assertNotIn("update_form_stats", source)

    def test_weak_surface_scanner_only_exempts_exact_stdlib_reference(self):
        standard = "import weakref\n_locks = weakref.WeakValueDictionary()\n"
        self.assertFalse(_has_removed_weak_surface(standard))
        for removed in ("weak_quiz", "weakref_alias", "form_stats", "review_stage_snapshot"):
            with self.subTest(removed=removed):
                self.assertTrue(_has_removed_weak_surface(standard + removed))

    def test_runtime_and_ui_have_no_residual_weak_surface(self):
        bot_dir = Path(__file__).parents[1] / "bot"
        allowed = {
            bot_dir / "common" / "db_safety.py",
        }
        offenders = []
        for path in bot_dir.rglob("*.py"):
            if path in allowed or "verb_forms_parsing" in path.parts:
                continue
            source = path.read_text(encoding="utf-8").lower()
            if _has_removed_weak_surface(source):
                offenders.append(str(path.relative_to(bot_dir.parent)))
        self.assertEqual(offenders, [])
        self.assertFalse((bot_dir / "handlers" / "weak.py").exists())


class RemoveWeakMigrationV17Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="retained-fixture-")
        self.db = os.path.join(self.tmp, "words.db")
        self.backups = os.path.join(self.tmp, "backups")
        init_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            # Historical fixtures must not retain the v19 generation index/column.
            conn.execute("DROP INDEX quiz_events_generation")
            conn.execute("ALTER TABLE quiz_events DROP COLUMN study_generation")
            conn.commit()
        set_user_version(self.db, 16)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE quiz_events ADD COLUMN review_stage_snapshot INTEGER")
            conn.execute(
                """
                CREATE TABLE form_stats (
                    user_id INTEGER NOT NULL,
                    word_id TEXT NOT NULL,
                    article_errors INTEGER NOT NULL DEFAULT 0,
                    article_streak_wrong INTEGER NOT NULL DEFAULT 0,
                    article_streak_correct INTEGER NOT NULL DEFAULT 0,
                    article_review_stage INTEGER NOT NULL DEFAULT 0,
                    article_next_review TEXT DEFAULT NULL,
                    article_active INTEGER NOT NULL DEFAULT 0,
                    plural_errors INTEGER NOT NULL DEFAULT 0,
                    plural_streak_wrong INTEGER NOT NULL DEFAULT 0,
                    plural_streak_correct INTEGER NOT NULL DEFAULT 0,
                    plural_review_stage INTEGER NOT NULL DEFAULT 0,
                    plural_next_review TEXT DEFAULT NULL,
                    plural_active INTEGER NOT NULL DEFAULT 0,
                    prateritum_errors INTEGER NOT NULL DEFAULT 0,
                    prateritum_streak_wrong INTEGER NOT NULL DEFAULT 0,
                    prateritum_streak_correct INTEGER NOT NULL DEFAULT 0,
                    prateritum_review_stage INTEGER NOT NULL DEFAULT 0,
                    prateritum_next_review TEXT DEFAULT NULL,
                    prateritum_active INTEGER NOT NULL DEFAULT 0,
                    partizip2_errors INTEGER NOT NULL DEFAULT 0,
                    partizip2_streak_wrong INTEGER NOT NULL DEFAULT 0,
                    partizip2_streak_correct INTEGER NOT NULL DEFAULT 0,
                    partizip2_review_stage INTEGER NOT NULL DEFAULT 0,
                    partizip2_next_review TEXT DEFAULT NULL,
                    partizip2_active INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, word_id),
                    FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
                )
                """
            )
            conn.executemany(
                "INSERT INTO words (id, type, article, german, plural, russian) "
                "VALUES (?, 'nomen', 'das', ?, 'Formen', ?)",
                [
                    ("ordinary", "Haus", "дом"),
                    ("weak", "Wort", "слово"),
                ],
            )
            conn.executemany(
                "INSERT INTO progress "
                "(user_id, word_id, mode, interval, repetitions, ease, due, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (7, "ordinary", "ru_de", 9, 4, 2.7, "2026-09-10", "2026-08-01 10:11:12"),
                    (7, "weak", "weak_article", 3, 2, 2.1, "2026-08-30", "2026-08-02 10:11:12"),
                ],
            )
            conn.executemany(
                "INSERT INTO quiz_events "
                "(id, user_id, word_id, mode, is_correct, prev_is_correct, "
                "review_stage_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (41, 7, "ordinary", "ru_de", 1, 0, 3, "2026-08-03 10:11:12"),
                    (42, 7, "weak", "weak_article", 0, 1, 2, "2026-08-04 10:11:12"),
                ],
            )
            conn.execute(
                "INSERT INTO form_stats (user_id, word_id, article_errors, article_active) "
                "VALUES (7, 'weak', 4, 1)"
            )
            conn.executemany(
                "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                [
                    (7, "weak_drill_gap", "4"),
                    (7, "weak_drill_repeats", "10"),
                    (7, "weak_drill_gap_extra", "keep"),
                    (7, "session_retry_gap", "3"),
                ],
            )
            conn.executemany(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                [
                    ("weak_drill_gap", "4"),
                    ("weak_drill_repeats", "10"),
                    ("weak_drill_repeats_extra", "keep"),
                    ("topic_filter", "all"),
                ],
            )
            self.ordinary_snapshot = {
                "snapshot": {
                    "user_id": 7,
                    "word_id": "ordinary",
                    "modes": ["ru_de"],
                    "progress": {"ru_de": None},
                    "form_stats": {"article_errors": 99},
                    "event_counts": {"ru_de": 1},
                }
            }
            conn.executemany(
                "INSERT INTO transaction_log (id, user_id, action, payload, created_at) "
                "VALUES (?, 7, ?, ?, ?)",
                [
                    (51, "quiz:weak_article", json.dumps({"snapshot": {"form_stats": {}}}), "2026-08-05 10:11:12"),
                    (52, "quiz:ru_de", json.dumps(self.ordinary_snapshot), "2026-08-06 10:11:12"),
                    (53, "note", json.dumps({"text": "weak_article"}), "2026-08-07 10:11:12"),
                ],
            )
            conn.execute(
                "CREATE INDEX idx_quiz_events_correct_created "
                "ON quiz_events(is_correct, created_at)"
            )
            conn.commit()

    def tearDown(self):
        pass  # Retain the synthetic runtime under the runner's explicit TMPDIR.

    def _rows(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, params).fetchall()

    def test_dry_run_is_non_mutating_and_reports_removed_and_preserved_counts(self):
        before = database_fingerprint(self.db)
        report = dry_run_migration(self.db)

        self.assertEqual(SCHEMA_VERSION, 22)
        self.assertTrue(report["ok"])
        self.assertEqual(before, database_fingerprint(self.db))
        step = report["steps"]["remove_weak_v17"]
        self.assertEqual(
            set(step["counts_before"]),
            {
                "form_stats",
                "weak_progress", "non_weak_progress",
                "weak_quiz_events", "non_weak_quiz_events",
                "weak_transaction_log", "non_weak_transaction_log",
                "weak_user_settings", "non_weak_user_settings",
                "weak_app_settings", "non_weak_app_settings",
            },
        )
        self.assertEqual(step["counts_before"]["form_stats"], 1)
        self.assertEqual(step["counts_before"]["weak_quiz_events"], 1)
        self.assertEqual(step["counts_before"]["non_weak_quiz_events"], 1)
        self.assertEqual(step["counts_after"]["form_stats"], 0)
        self.assertEqual(step["counts_after"]["weak_quiz_events"], 0)
        self.assertEqual(step["counts_after"]["non_weak_quiz_events"], 1)

    def test_v16_to_v17_removes_only_weak_data_and_preserves_schema_details(self):
        old_indexes = _schema(self.db)[2]
        report = apply_destructive_migrations_safely(self.db, self.backups)

        self.assertEqual(get_user_version(self.db), SCHEMA_VERSION)
        self.assertEqual(report["from_version"], 16)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertTrue(Path(report["backup"]).name.startswith("snapshot-premigrate-v16-"))
        tables, columns, indexes = _schema(self.db)
        self.assertNotIn("form_stats", tables)
        self.assertEqual(
            columns["quiz_events"],
            ["id", "user_id", "word_id", "mode", "is_correct", "prev_is_correct", "created_at", "study_generation"],
        )
        self.assertEqual(set(indexes), set(old_indexes) | {"quiz_events_generation"})
        self.assertEqual(
            {name: sql for name, sql in indexes.items() if name != "quiz_events_generation"},
            old_indexes,
        )
        self.assertEqual(
            [row[2] for row in self._rows("PRAGMA index_info(quiz_events_generation)")],
            ["user_id", "word_id", "study_generation", "mode", "created_at"],
        )
        self.assertEqual(
            self._rows(
                "SELECT id, user_id, word_id, mode, is_correct, prev_is_correct, created_at, study_generation "
                "FROM quiz_events ORDER BY id"
            ),
            [(41, 7, "ordinary", "ru_de", 1, 0, "2026-08-03 10:11:12", 0)],
        )
        self.assertEqual(
            self._rows(
                "SELECT user_id, word_id, mode, interval, repetitions, ease, due, updated_at "
                "FROM progress ORDER BY word_id, mode"
            ),
            [(7, "ordinary", "ru_de", 9, 4, 2.7, "2026-09-10", "2026-08-01 10:11:12")],
        )
        self.assertEqual(
            self._rows("SELECT id, action, created_at FROM transaction_log ORDER BY id"),
            [
                (52, "quiz:ru_de", "2026-08-06 10:11:12"),
                (53, "note", "2026-08-07 10:11:12"),
            ],
        )
        self.assertEqual(
            json.loads(self._rows("SELECT payload FROM transaction_log WHERE id = 52")[0][0]),
            self.ordinary_snapshot,
        )
        self.assertEqual(
            self._rows("SELECT key, value FROM user_settings ORDER BY key"),
            [("session_retry_gap", "3"), ("weak_drill_gap_extra", "keep")],
        )
        self.assertEqual(
            self._rows("SELECT key, value FROM app_settings ORDER BY key"),
            [("topic_filter", "all"), ("weak_drill_repeats_extra", "keep")],
        )

    def test_v17_step_and_version_roll_back_together(self):
        before = database_fingerprint(self.db)
        original = db_safety._migrate_remove_weak_v17

        def migrate_then_fail(conn):
            original(conn)
            raise RuntimeError("injected v17 failure")

        with (
            patch("bot.common.db_safety._migrate_remove_weak_v17", side_effect=migrate_then_fail),
            self.assertRaisesRegex(RuntimeError, "injected v17 failure"),
        ):
            db_safety._apply_migration_dispatcher(self.db)

        self.assertEqual(get_user_version(self.db), 16)
        self.assertEqual(database_fingerprint(self.db), before)

    def test_repeat_migration_is_noop(self):
        apply_destructive_migrations_safely(self.db, self.backups)
        again = apply_destructive_migrations_safely(self.db, self.backups)
        self.assertFalse(again["migrated"])
        self.assertIsNone(again["backup"])


class MigrationSequenceTest(unittest.TestCase):
    def test_v15_runs_v16_then_v17_in_exact_order(self):
        directory = tempfile.mkdtemp(prefix="retained-weak-")
        db = os.path.join(directory, "words.db")
        _create_additive_fixture(db)
        set_user_version(db, 15)
        observed = []
        original_v16 = db_safety._migrate_phrase_modes_v16
        original_v17 = db_safety._migrate_remove_weak_v17
        def observe_v16(conn, today=None):
            observed.append(("v16", conn.execute("PRAGMA user_version").fetchone()[0]))
            return original_v16(conn, today=today)
        def observe_v17(conn):
            observed.append(("v17", conn.execute("PRAGMA user_version").fetchone()[0]))
            return original_v17(conn)
        with (
            patch("bot.common.db_safety._migrate_phrase_modes_v16", side_effect=observe_v16),
            patch("bot.common.db_safety._migrate_remove_weak_v17", side_effect=observe_v17),
        ):
            apply_destructive_migrations_safely(db, os.path.join(directory, "backups"))

        self.assertEqual(observed, [("v16", 15), ("v17", 16)] * 2)


if __name__ == "__main__":
    unittest.main()
