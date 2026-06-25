"""Migration tests + backward-compatibility contract (docs/multiuser.md §0.1/§0.2).

The contract enforces ADD-only migrations: every table/column that the frozen
v1.0-single-user baseline relies on must still exist, so an older (rolled-back)
build can still open a newer DB. Adding tables/columns is fine; dropping or
renaming a baseline one fails the contract.
"""

import os
import sqlite3
import tempfile
import unittest

from bot.common.db_safety import run_healthcheck
from bot.storage import init_db


def _schema(db_path):
    conn = sqlite3.connect(db_path)
    try:
        out = {}
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in rows:
            out[name] = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
        return out
    finally:
        conn.close()


# Frozen schema contract for the v1.0-single-user baseline. DO NOT remove entries
# here when adding multiuser tables/columns — the test only checks that the live
# schema is a SUPERSET of this. Editing/removing an entry means a breaking change.
BASELINE_V1 = {
    "app_settings": {"key", "value"},
    "banned_words": {"normalized_word", "raw_word", "created_at"},
    "form_stats": {
        "user_id", "word_id",
        "article_errors", "article_streak_wrong", "article_streak_correct",
        "article_review_stage", "article_next_review",
        "plural_errors", "plural_streak_wrong", "plural_streak_correct",
        "plural_review_stage", "plural_next_review",
        "prateritum_errors", "prateritum_streak_wrong", "prateritum_streak_correct",
        "prateritum_review_stage", "prateritum_next_review",
        "partizip2_errors", "partizip2_streak_wrong", "partizip2_streak_correct",
        "partizip2_review_stage", "partizip2_next_review",
    },
    "learning_batches": {
        "id", "user_id", "target_qty", "status", "ready_since",
        "last_notified_at", "created_at", "updated_at",
    },
    "learning_items": {
        "user_id", "word_id", "batch_id", "step_index", "preview_done",
        "next_due", "status", "created_at", "updated_at",
    },
    "progress": {"user_id", "word_id", "mode", "interval", "repetitions", "ease", "due", "updated_at"},
    "quiz_events": {
        "id", "user_id", "word_id", "mode", "is_correct", "prev_is_correct",
        "review_stage_snapshot", "created_at",
    },
    "synonym_groups": {"id", "created_at"},
    "transaction_log": {"id", "user_id", "action", "payload", "created_at"},
    "user_levels": {"user_id", "level"},
    "users": {
        "user_id", "username", "first_name", "status",
        "interface_lang", "dict_lang", "created_at", "updated_at",
    },
    "vf_checks": {
        "id", "user_id", "word_id", "word_type", "base_form", "status", "retryable",
        "local_payload", "remote_payload", "diff_payload", "source_hash",
        "approved", "applied", "created_at", "updated_at",
    },
    "word_flags": {"id", "word_id", "user_id", "comment", "status", "created_at", "reviewed_at"},
    "word_synonyms": {"word_id", "group_id"},
    "words": {
        "id", "type", "article", "german", "plural", "infinitive", "prateritum",
        "partizip2", "komparativ", "superlativ", "russian", "ukrainian", "english",
        "image", "example", "topic", "created_at", "updated_at",
    },
}


class BackwardCompatContractTest(unittest.TestCase):
    def test_schema_is_superset_of_v1_baseline(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        actual = _schema(db)
        for table, cols in BASELINE_V1.items():
            self.assertIn(table, actual, f"backward-compat broken: table '{table}' missing")
            missing = cols - actual[table]
            self.assertEqual(
                missing, set(),
                f"backward-compat broken: '{table}' lost columns {missing} (migrations must be ADD-only)",
            )


class MigrationFromOldSchemaTest(unittest.TestCase):
    def test_old_db_gets_late_columns_and_keeps_data(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        # Build a pre-migration DB: words without late columns, users without lang columns.
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE words (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                article TEXT NOT NULL DEFAULT '',
                german TEXT NOT NULL DEFAULT '',
                plural TEXT NOT NULL DEFAULT '',
                infinitive TEXT NOT NULL DEFAULT '',
                prateritum TEXT NOT NULL DEFAULT '',
                partizip2 TEXT NOT NULL DEFAULT '',
                russian TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO words (id, type, article, german, plural, russian) "
            "VALUES ('w1', 'nomen', 'das', 'Haus', 'Häuser', 'дом')"
        )
        conn.commit()
        conn.close()

        init_db(db)  # run the migration

        cols = _schema(db)
        for col in ("ukrainian", "english", "topic", "image", "example",
                    "komparativ", "superlativ", "updated_at"):
            self.assertIn(col, cols["words"], f"late column '{col}' was not added")
        self.assertIn("interface_lang", cols["users"])
        self.assertIn("dict_lang", cols["users"])

        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT german, plural, russian, topic, updated_at, created_at "
                "FROM words WHERE id='w1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "Haus")
        self.assertEqual(row[1], "Häuser")
        self.assertEqual(row[2], "дом")
        self.assertEqual(row[3], "1")          # topic default backfilled
        self.assertEqual(row[4], row[5])       # updated_at backfilled to created_at

        self.assertTrue(run_healthcheck(db)["ok"])


if __name__ == "__main__":
    unittest.main()
