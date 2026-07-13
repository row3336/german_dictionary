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

from bot.common.db_safety import (
    SCHEMA_VERSION,
    apply_migrations_safely,
    get_user_version,
    run_healthcheck,
    set_user_version,
)
from bot.storage import get_topic_description, init_db, list_topics_with_desc


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
                    "komparativ", "superlativ", "updated_at", "vf_checked_at"):
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


class V1ToV2MigrationTest(unittest.TestCase):
    """P2.1 — adding user_settings is a safe additive v1 -> v2 migration."""

    def test_v1_to_v2_adds_user_settings(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        # simulate a real v1 DB: user_settings absent, version marker at 1
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE user_settings")
        conn.commit()
        conn.close()
        set_user_version(db, 1)
        self.assertNotIn("user_settings", _schema(db))

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 1)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 2)
        self.assertIn("user_settings", _schema(db))
        self.assertEqual({"user_id", "key", "value"}, _schema(db)["user_settings"])
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

    def test_v2_to_v3_adds_user_word_media(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        # simulate a real v2 DB: user_word_media absent, version marker at 2
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE user_word_media")
        conn.commit()
        conn.close()
        set_user_version(db, 2)
        self.assertNotIn("user_word_media", _schema(db))

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 2)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 3)
        self.assertIn("user_word_media", _schema(db))
        self.assertEqual(
            {"user_id", "word_id", "image", "example", "russian", "ukrainian", "english", "updated_at"},
            _schema(db)["user_word_media"],
        )
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

    def test_v3_to_v4_adds_error_reports_and_feedback(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        # simulate a real v3 DB: P6 tables absent, version marker at 3
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE error_reports")
        conn.execute("DROP TABLE feedback")
        conn.commit()
        conn.close()
        set_user_version(db, 3)
        schema = _schema(db)
        self.assertNotIn("error_reports", schema)
        self.assertNotIn("feedback", schema)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 3)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 4)
        schema = _schema(db)
        self.assertEqual(
            {"id", "word_id", "user_id", "mode", "state", "last_action",
             "comment", "status", "created_at", "reviewed_at"},
            schema["error_reports"],
        )
        self.assertEqual(
            {"id", "user_id", "text", "status", "created_at", "reviewed_at"},
            schema["feedback"],
        )
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)


class FormStatsActiveMigrationTest(unittest.TestCase):
    """#18 — adding form_stats.*_active is a safe additive migration that
    backfills the weak-episode flag for words already in an episode."""

    def test_v4_to_v5_adds_active_columns_and_backfills(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        # Build a v4-style DB: words + a form_stats WITHOUT the *_active columns.
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
            CREATE TABLE form_stats (
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                article_errors INTEGER NOT NULL DEFAULT 0,
                article_streak_wrong INTEGER NOT NULL DEFAULT 0,
                article_streak_correct INTEGER NOT NULL DEFAULT 0,
                article_review_stage INTEGER NOT NULL DEFAULT 0,
                article_next_review TEXT DEFAULT NULL,
                plural_errors INTEGER NOT NULL DEFAULT 0,
                plural_streak_wrong INTEGER NOT NULL DEFAULT 0,
                plural_streak_correct INTEGER NOT NULL DEFAULT 0,
                plural_review_stage INTEGER NOT NULL DEFAULT 0,
                plural_next_review TEXT DEFAULT NULL,
                prateritum_errors INTEGER NOT NULL DEFAULT 0,
                prateritum_streak_wrong INTEGER NOT NULL DEFAULT 0,
                prateritum_streak_correct INTEGER NOT NULL DEFAULT 0,
                prateritum_review_stage INTEGER NOT NULL DEFAULT 0,
                prateritum_next_review TEXT DEFAULT NULL,
                partizip2_errors INTEGER NOT NULL DEFAULT 0,
                partizip2_streak_wrong INTEGER NOT NULL DEFAULT 0,
                partizip2_streak_correct INTEGER NOT NULL DEFAULT 0,
                partizip2_review_stage INTEGER NOT NULL DEFAULT 0,
                partizip2_next_review TEXT DEFAULT NULL,
                PRIMARY KEY (user_id, word_id)
            );
            """
        )
        conn.execute("INSERT INTO words (id, type, german) VALUES ('w_active', 'nomen', 'Tisch')")
        conn.execute("INSERT INTO words (id, type, german) VALUES ('w_idle', 'nomen', 'Lampe')")
        # w_active: legacy-active episode (2 article errors) → flag must backfill to 1.
        conn.execute(
            "INSERT INTO form_stats (user_id, word_id, article_errors) VALUES (1, 'w_active', 2)"
        )
        # w_idle: not weak → flag stays 0.
        conn.execute(
            "INSERT INTO form_stats (user_id, word_id, article_errors) VALUES (1, 'w_idle', 0)"
        )
        conn.commit()
        conn.close()
        set_user_version(db, 4)
        self.assertNotIn("article_active", _schema(db)["form_stats"])

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 4)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 5)
        cols = _schema(db)["form_stats"]
        for field in ("article", "plural", "prateritum", "partizip2"):
            self.assertIn(f"{field}_active", cols)

        conn = sqlite3.connect(db)
        try:
            active = conn.execute(
                "SELECT article_active FROM form_stats WHERE word_id='w_active'"
            ).fetchone()[0]
            idle = conn.execute(
                "SELECT article_active FROM form_stats WHERE word_id='w_idle'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(active, 1)  # already-active episode preserved
        self.assertEqual(idle, 0)
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)


class TopicsRegistryMigrationTest(unittest.TestCase):
    """spec #8 — v5 -> v6 adds the topics registry, seeded DISTINCT from words.topic."""

    def _seed_v5(self, d):
        db = os.path.join(d, "words.db")
        init_db(db)
        conn = sqlite3.connect(db)
        # distinct topics '01', '7', 'verb'; '7' twice to prove DISTINCT collapses it
        conn.executescript(
            """
            INSERT INTO words (id, type, topic) VALUES ('w1', 'nomen', '01');
            INSERT INTO words (id, type, topic) VALUES ('w2', 'verb', '7');
            INSERT INTO words (id, type, topic) VALUES ('w3', 'verb', '7');
            INSERT INTO words (id, type, topic) VALUES ('w4', 'nomen', 'verb');
            """
        )
        conn.execute("DROP TABLE topics")
        conn.commit()
        conn.close()
        set_user_version(db, 5)
        self.assertNotIn("topics", _schema(db))
        return db

    def test_v5_to_v6_creates_and_seeds_topics(self):
        d = tempfile.mkdtemp()
        db = self._seed_v5(d)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 5)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 6)
        self.assertIn("topics", _schema(db))
        # migrating to the current version also adds topics.level (v14)
        self.assertEqual({"name", "description", "level"}, _schema(db)["topics"])
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT name, description FROM topics ORDER BY name").fetchall()
        finally:
            conn.close()
        # seeded = DISTINCT topics in use, empty descriptions; '7' not duplicated
        self.assertEqual(rows, [("01", ""), ("7", ""), ("verb", "")])

    def test_backup_taken_on_topics_migration(self):
        d = tempfile.mkdtemp()
        db = self._seed_v5(d)
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIsNotNone(report["backup"])
        self.assertTrue(os.path.exists(report["backup"]))

    def test_seed_idempotent_and_preserves_descriptions(self):
        d = tempfile.mkdtemp()
        db = self._seed_v5(d)
        apply_migrations_safely(db, os.path.join(d, "backups"))

        # curate a description, then re-run init_db (defensive re-seed on every call)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE topics SET description = 'Zahlen' WHERE name = '01'")
        conn.commit()
        conn.close()
        init_db(db)

        conn = sqlite3.connect(db)
        try:
            desc = conn.execute("SELECT description FROM topics WHERE name='01'").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(desc, "Zahlen")  # not clobbered by re-seed
        self.assertEqual(count, 3)  # no duplicate rows

    def test_fresh_db_has_topics_table(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertTrue(report["fresh"])
        self.assertIn("topics", _schema(db))
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

    def test_new_topic_in_words_auto_seeded_on_next_init(self):
        d = tempfile.mkdtemp()
        db = self._seed_v5(d)
        apply_migrations_safely(db, os.path.join(d, "backups"))
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO words (id, type, topic) VALUES ('w5', 'nomen', '05')")
        conn.commit()
        conn.close()
        init_db(db)
        self.assertEqual(get_topic_description(db, "05"), "")
        names = {n for n, _ in list_topics_with_desc(db)}
        self.assertIn("05", names)


class PrepositionColumnsMigrationTest(unittest.TestCase):
    """spec #1 — v6 -> v7 adds words.preposition_type + words.case_type (ADD-only)."""

    def _seed_v6(self, d):
        db = os.path.join(d, "words.db")
        init_db(db)  # full current schema, then peel the two v7 columns back off
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            INSERT INTO words (id, type, german, russian, topic)
                VALUES ('w1', 'praeposition', 'in', 'в', '1');
            INSERT INTO words (id, type, german, russian, topic)
                VALUES ('w2', 'nomen', 'Haus', 'дом', '1');
            ALTER TABLE words DROP COLUMN preposition_type;
            ALTER TABLE words DROP COLUMN case_type;
            """
        )
        conn.commit()
        conn.close()
        set_user_version(db, 6)
        cols = _schema(db)["words"]
        self.assertNotIn("preposition_type", cols)
        self.assertNotIn("case_type", cols)
        return db

    def test_v6_to_v7_adds_preposition_columns_keeps_data(self):
        d = tempfile.mkdtemp()
        db = self._seed_v6(d)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 6)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 7)
        cols = _schema(db)["words"]
        self.assertIn("preposition_type", cols)
        self.assertIn("case_type", cols)
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT id, preposition_type, case_type FROM words ORDER BY id"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        finally:
            conn.close()
        # existing rows default to '' for both new columns; nothing lost
        self.assertEqual(rows, [("w1", "", ""), ("w2", "", "")])
        self.assertEqual(count, 2)

    def test_backup_taken_on_preposition_migration(self):
        d = tempfile.mkdtemp()
        db = self._seed_v6(d)
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIsNotNone(report["backup"])
        self.assertTrue(os.path.exists(report["backup"]))

    def test_fresh_db_has_preposition_columns(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        apply_migrations_safely(db, os.path.join(d, "backups"))
        cols = _schema(db)["words"]
        self.assertIn("preposition_type", cols)
        self.assertIn("case_type", cols)


class ConjugationColumnsMigrationTest(unittest.TestCase):
    """spec #3 — v7 -> v8 adds words.conjugation_praesens/_praeteritum +
    conjugation_progress table (ADD-only)."""

    def _seed_v7(self, d):
        db = os.path.join(d, "words.db")
        init_db(db)  # full current schema, then peel the two v8 columns/table back off
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            INSERT INTO words (id, type, german, infinitive, russian, topic)
                VALUES ('w1', 'verb', 'gehen', 'gehen', 'идти', '1');
            INSERT INTO words (id, type, german, russian, topic)
                VALUES ('w2', 'nomen', 'Haus', 'дом', '1');
            ALTER TABLE words DROP COLUMN conjugation_praesens;
            ALTER TABLE words DROP COLUMN conjugation_praeteritum;
            DROP TABLE conjugation_progress;
            """
        )
        conn.commit()
        conn.close()
        set_user_version(db, 7)
        cols = _schema(db)["words"]
        self.assertNotIn("conjugation_praesens", cols)
        self.assertNotIn("conjugation_praeteritum", cols)
        self.assertNotIn("conjugation_progress", _schema(db))
        return db

    def test_v7_to_v8_adds_conjugation_columns_and_table_keeps_data(self):
        d = tempfile.mkdtemp()
        db = self._seed_v7(d)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 7)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 8)
        schema = _schema(db)
        self.assertIn("conjugation_praesens", schema["words"])
        self.assertIn("conjugation_praeteritum", schema["words"])
        self.assertIn("conjugation_progress", schema)
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT id, conjugation_praesens, conjugation_praeteritum FROM words ORDER BY id"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        finally:
            conn.close()
        # existing rows default to '' for both new columns; nothing lost
        self.assertEqual(rows, [("w1", "", ""), ("w2", "", "")])
        self.assertEqual(count, 2)

    def test_backup_taken_on_conjugation_migration(self):
        d = tempfile.mkdtemp()
        db = self._seed_v7(d)
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIsNotNone(report["backup"])
        self.assertTrue(os.path.exists(report["backup"]))

    def test_migration_idempotent(self):
        d = tempfile.mkdtemp()
        db = self._seed_v7(d)
        apply_migrations_safely(db, os.path.join(d, "backups"))
        # second run: nothing to migrate, healthcheck still ok
        report2 = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertFalse(report2["migrated"])
        self.assertTrue(report2["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

    def test_fresh_db_has_conjugation_columns_and_table(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        apply_migrations_safely(db, os.path.join(d, "backups"))
        schema = _schema(db)
        self.assertIn("conjugation_praesens", schema["words"])
        self.assertIn("conjugation_praeteritum", schema["words"])
        self.assertIn("conjugation_progress", schema)


class UserWordMediaTranslationMigrationTest(unittest.TestCase):
    """spec #7 — v8 -> v9 adds user_word_media.russian/ukrainian/english (ADD-only)."""

    def _seed_v8(self, d):
        db = os.path.join(d, "words.db")
        init_db(db)  # full current schema, then peel the three v9 columns back off
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            INSERT INTO words (id, type, german, russian, topic) VALUES ('w1', 'nomen', 'Haus', 'дом', '1');
            INSERT INTO user_word_media (user_id, word_id, example) VALUES (7, 'w1', 'mein Beispiel');
            ALTER TABLE user_word_media DROP COLUMN russian;
            ALTER TABLE user_word_media DROP COLUMN ukrainian;
            ALTER TABLE user_word_media DROP COLUMN english;
            """
        )
        conn.commit()
        conn.close()
        set_user_version(db, 8)
        cols = _schema(db)["user_word_media"]
        self.assertNotIn("russian", cols)
        return db

    def test_v8_to_v9_adds_translation_columns_keeps_data(self):
        d = tempfile.mkdtemp()
        db = self._seed_v8(d)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 8)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 9)
        cols = _schema(db)["user_word_media"]
        self.assertTrue({"russian", "ukrainian", "english"} <= cols)
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT example, russian, ukrainian, english FROM user_word_media WHERE user_id=7 AND word_id='w1'"
            ).fetchone()
        finally:
            conn.close()
        # existing personal example preserved; new translation columns default ''
        self.assertEqual(row, ("mein Beispiel", "", "", ""))

    def test_backup_and_idempotent(self):
        d = tempfile.mkdtemp()
        db = self._seed_v8(d)
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIsNotNone(report["backup"])
        self.assertTrue(os.path.exists(report["backup"]))
        report2 = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertFalse(report2["migrated"])
        self.assertTrue(report2["healthcheck"]["ok"])

    def test_fresh_db_has_translation_columns(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertTrue({"russian", "ukrainian", "english"} <= _schema(db)["user_word_media"])


class CefrLevelColumnMigrationTest(unittest.TestCase):
    """word-level CEFR — v11 -> v12 adds words.cefr_level (ADD-only, DEFAULT '')."""

    def _seed_v11(self, d):
        db = os.path.join(d, "words.db")
        init_db(db)  # full current schema, then peel the v12 column back off
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            INSERT INTO words (id, type, german, russian, topic)
                VALUES ('w1', 'nomen', 'Haus', 'дом', '1');
            INSERT INTO words (id, type, german, russian, topic)
                VALUES ('w2', 'verb', 'gehen', 'идти', '1');
            ALTER TABLE words DROP COLUMN cefr_level;
            """
        )
        conn.commit()
        conn.close()
        set_user_version(db, 11)
        self.assertNotIn("cefr_level", _schema(db)["words"])
        return db

    def test_v11_to_v12_adds_cefr_level_keeps_data(self):
        d = tempfile.mkdtemp()
        db = self._seed_v11(d)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 11)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 12)
        self.assertIn("cefr_level", _schema(db)["words"])
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT id, cefr_level FROM words ORDER BY id").fetchall()
            count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        finally:
            conn.close()
        # existing rows default to '' (unknown level); nothing lost
        self.assertEqual(rows, [("w1", ""), ("w2", "")])
        self.assertEqual(count, 2)

    def test_backup_and_idempotent(self):
        d = tempfile.mkdtemp()
        db = self._seed_v11(d)
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIsNotNone(report["backup"])
        self.assertTrue(os.path.exists(report["backup"]))
        report2 = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertFalse(report2["migrated"])
        self.assertTrue(report2["healthcheck"]["ok"])

    def test_fresh_db_has_cefr_level_column(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIn("cefr_level", _schema(db)["words"])


class TopicLevelMigrationTest(unittest.TestCase):
    """v13 -> v14 adds topics.level (per-topic book-level). Column-only, DEFAULT '';
    no blanket UPDATE, so pre-existing levels are never clobbered on a re-run."""

    def _seed_v13(self, d):
        db = os.path.join(d, "words.db")
        init_db(db)  # full current schema, then peel the v14 column back off
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            INSERT INTO words (id, type, german, russian, topic) VALUES ('w1','nomen','Haus','дом','1');
            INSERT INTO words (id, type, german, russian, topic) VALUES ('w2','verb','reisen','ехать','2');
            ALTER TABLE topics DROP COLUMN level;
            """
        )
        conn.commit()
        conn.close()
        set_user_version(db, 13)
        self.assertNotIn("level", _schema(db)["topics"])
        return db

    def test_v13_to_v14_adds_level_keeps_data(self):
        d = tempfile.mkdtemp()
        db = self._seed_v13(d)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))

        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 13)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 14)
        self.assertIn("level", _schema(db)["topics"])
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)

        # existing topics migrated with an EMPTY level (unassigned), not a forced A1
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT name, level FROM topics ORDER BY name").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("1", ""), ("2", "")])

    def test_migration_does_not_clobber_existing_levels(self):
        # a DB already carrying a real B2 level must keep it across a re-run of init_db
        d = tempfile.mkdtemp()
        db = self._seed_v13(d)
        apply_migrations_safely(db, os.path.join(d, "backups"))
        conn = sqlite3.connect(db)
        conn.execute("UPDATE topics SET level = 'B2' WHERE name = '2'")
        conn.commit()
        conn.close()
        init_db(db)  # defensive re-run must not reset the level
        conn = sqlite3.connect(db)
        try:
            level = conn.execute("SELECT level FROM topics WHERE name='2'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(level, "B2")

    def test_fresh_db_has_topic_level_column(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertIn("level", _schema(db)["topics"])


if __name__ == "__main__":
    unittest.main()
