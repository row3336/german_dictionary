import math
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .app_config import CASE_TYPES, CEFR_LEVELS, PREPOSITION_TYPES, get_owner_id, is_owner
from .common.conjugation import parse_conjugation, takes_praeteritum
from .text_utils import cap_word

DEFAULT_EASE = 2.5
MIN_EASE = 1.3
MAX_EASE = 3.5
# A word ENTERS a weak episode when, on a wrong answer, total errors reach
# WEAK_ACTIVE_MIN_ERRORS or it has WEAK_ACTIVE_MIN_STREAK_WRONG wrong in a row.
# Entry only flips the persistent per-form `*_active` flag; once set, the word is
# HELD in the quiz regardless of how the counters move, until it gets
# WEAK_ACTIVE_CLEAR_STREAK correct in a row (which clears the flag and promotes
# it to the day-based review ladder). Separating entry from hold is why the flag
# exists: a single correct must not drop a freshly-entered word before it can
# earn its 3-in-a-row (see docs/multiuser_test_bugs.md #18).
WEAK_ACTIVE_MIN_ERRORS = 3
WEAK_ACTIVE_MIN_STREAK_WRONG = 2
WEAK_ACTIVE_CLEAR_STREAK = 3
# Old (pre-flag) entry rule, kept only to backfill the `*_active` flag for words
# that were already in an episode when the v4->v5 migration ran.
WEAK_LEGACY_ACTIVE_MIN_ERRORS = 2
WEAK_REVIEW_DAYS_STAGE1 = 1
WEAK_REVIEW_DAYS_STAGE2 = 2
WEAK_REVIEW_DAYS_STAGE3 = 5

QUIZ_RU_DE = "ru_de"
QUIZ_DE_RU = "de_ru"
QUIZ_ARTICLE = "article"
INTERFACE_LANGS = ("en", "ru", "uk")
DICT_LANGS = ("ru", "uk", "en")


def default_srs(today=None):
    """Handle default srs."""
    if today is None:
        today = date.today()
    return {
        "interval": 1,
        "repetitions": 0,
        "ease": DEFAULT_EASE,
        "due": today.isoformat(),
    }


@contextmanager
def _connect(db_path):
    """Open a SQLite connection with concurrency-friendly pragmas.

    WAL + busy_timeout let multiple users read/write without "database is locked"
    under the single-process bot: writers no longer block readers, and write
    contention waits (up to busy_timeout) instead of failing immediately.
    synchronous=NORMAL is the safe+faster setting under WAL. journal_mode=WAL is
    persisted in the DB header (set once, harmless to re-assert each connect).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_indexes(conn):
    """Handle ensure indexes."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_words_type ON words(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_due ON progress(user_id, mode, due)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_levels_user ON user_levels(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_word_flags_status_created ON word_flags(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_events_day_user ON quiz_events(created_at, user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_events_user_word_mode ON quiz_events(user_id, word_id, mode, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_form_stats_user_word ON form_stats(user_id, word_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_word_synonyms_group ON word_synonyms(group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_learning_items_user_status_due ON learning_items(user_id, status, next_due)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_learning_items_batch_status ON learning_items(batch_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transaction_log_user_id ON transaction_log(user_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transaction_log_created_at ON transaction_log(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conjugation_progress_user ON conjugation_progress(user_id, word_id)")


def _repair_word_fk_tables(conn):
    """Handle repair word foreign keys."""
    def _fk_targets_words_old(table_name):
        rows = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
        return any(row[2] == "words_old" for row in rows)

    def _table_exists(table_name):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return bool(row)

    def _table_columns(table_name):
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [row[1] for row in rows]

    def _copy_table(old_name, create_sql):
        if not _table_exists(old_name):
            return
        conn.execute(f"ALTER TABLE {old_name} RENAME TO {old_name}_old")
        conn.execute(create_sql)
        new_cols = _table_columns(old_name)
        old_cols = _table_columns(f"{old_name}_old")
        cols = [c for c in new_cols if c in old_cols]
        if cols:
            col_list = ", ".join(cols)
            conn.execute(
                f"INSERT INTO {old_name} ({col_list}) SELECT {col_list} FROM {old_name}_old"
            )
        conn.execute(f"DROP TABLE {old_name}_old")

    def _drop_if_exists(table_name):
        if _table_exists(table_name):
            conn.execute(f"DROP TABLE {table_name}")

    tables = ("progress", "word_flags", "quiz_events", "form_stats", "word_synonyms")
    if not any(_table_exists(t) and _fk_targets_words_old(t) for t in tables):
        return

    conn.execute("PRAGMA foreign_keys = OFF")

    for old_name in ("progress_old", "word_flags_old", "quiz_events_old", "form_stats_old", "word_synonyms_old"):
        _drop_if_exists(old_name)

    _copy_table(
        "progress",
        """
        CREATE TABLE progress (
            user_id INTEGER NOT NULL,
            word_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            interval INTEGER NOT NULL,
            repetitions INTEGER NOT NULL,
            ease REAL NOT NULL,
            due TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, word_id, mode),
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        )
        """,
    )

    _copy_table(
        "word_flags",
        """
        CREATE TABLE word_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        )
        """,
    )

    _copy_table(
        "quiz_events",
        """
        CREATE TABLE quiz_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            word_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            prev_is_correct INTEGER,
            review_stage_snapshot INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        )
        """,
    )

    _copy_table(
        "form_stats",
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
        """,
    )

    _copy_table(
        "word_synonyms",
        """
        CREATE TABLE word_synonyms (
            word_id TEXT PRIMARY KEY,
            group_id INTEGER NOT NULL,
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES synonym_groups(id) ON DELETE CASCADE
        )
        """,
    )

    conn.execute("PRAGMA foreign_keys = ON")


def init_db(db_path):
    """Handle init db."""
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS words (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                article TEXT NOT NULL DEFAULT '',
                german TEXT NOT NULL DEFAULT '',
                plural TEXT NOT NULL DEFAULT '',
                infinitive TEXT NOT NULL DEFAULT '',
                prateritum TEXT NOT NULL DEFAULT '',
                partizip2 TEXT NOT NULL DEFAULT '',
                komparativ TEXT NOT NULL DEFAULT '',
                superlativ TEXT NOT NULL DEFAULT '',
                russian TEXT NOT NULL DEFAULT '',
                ukrainian TEXT NOT NULL DEFAULT '',
                english TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                example TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '1',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS progress (
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                interval INTEGER NOT NULL,
                repetitions INTEGER NOT NULL,
                ease REAL NOT NULL,
                due TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, word_id, mode),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_words_type ON words(type);
            CREATE INDEX IF NOT EXISTS idx_progress_due ON progress(user_id, mode, due);

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                interface_lang TEXT NOT NULL DEFAULT 'en',
                dict_lang TEXT NOT NULL DEFAULT 'ru',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );

            CREATE TABLE IF NOT EXISTS user_levels (
                user_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                PRIMARY KEY (user_id, level),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_word_media (
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                image TEXT NOT NULL DEFAULT '',
                example TEXT NOT NULL DEFAULT '',
                russian TEXT NOT NULL DEFAULT '',
                ukrainian TEXT NOT NULL DEFAULT '',
                english TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, word_id),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS word_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );

            -- P6: user error/fix report on a word card, with a small trace of where
            -- it happened (mode/state/last_action). Modeled on word_flags.
            CREATE TABLE IF NOT EXISTS error_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                last_action TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_error_reports_status_created ON error_reports(status, created_at);

            -- P6: general user feedback about the bot (not word-specific).
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_status_created ON feedback(status, created_at);

            CREATE TABLE IF NOT EXISTS quiz_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                prev_is_correct INTEGER,
                review_stage_snapshot INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS banned_words (
                normalized_word TEXT PRIMARY KEY,
                raw_word TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS form_stats (
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
            );

            CREATE TABLE IF NOT EXISTS synonym_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS word_synonyms (
                word_id TEXT PRIMARY KEY,
                group_id INTEGER NOT NULL,
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES synonym_groups(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS learning_batches (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                target_qty INTEGER NOT NULL DEFAULT 20,
                status TEXT NOT NULL DEFAULT 'active',
                ready_since TEXT DEFAULT NULL,
                last_notified_at TEXT DEFAULT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS learning_items (
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                step_index INTEGER NOT NULL DEFAULT 0,
                preview_done INTEGER NOT NULL DEFAULT 0,
                next_due TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, word_id),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE,
                FOREIGN KEY (batch_id) REFERENCES learning_batches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS transaction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS topics (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS vf_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                word_type TEXT NOT NULL,
                base_form TEXT NOT NULL,
                status TEXT NOT NULL,
                retryable INTEGER NOT NULL DEFAULT 0,
                local_payload TEXT NOT NULL DEFAULT '',
                remote_payload TEXT NOT NULL DEFAULT '',
                diff_payload TEXT NOT NULL DEFAULT '',
                source_hash TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 0,
                applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );

            -- spec #3: passive conjugation drill progress, per user (no FSRS).
            -- seen_count / last_seen_at drive the weighted-rotation order; guests
            -- get their own rows (isolated by user_id).
            CREATE TABLE IF NOT EXISTS conjugation_progress (
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT DEFAULT NULL,
                PRIMARY KEY (user_id, word_id),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );
            """
        )
        _ensure_schema(conn)


def _ensure_schema(conn):
    """Handle ensure schema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vf_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            word_id TEXT NOT NULL,
            word_type TEXT NOT NULL,
            base_form TEXT NOT NULL,
            status TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0,
            local_payload TEXT NOT NULL DEFAULT '',
            remote_payload TEXT NOT NULL DEFAULT '',
            diff_payload TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL DEFAULT '',
            approved INTEGER NOT NULL DEFAULT 0,
            applied INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        )
        """
    )
    word_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(words)").fetchall()
    }
    if "cefr_level" in word_cols:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            CREATE TABLE words_new (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                article TEXT NOT NULL DEFAULT '',
                german TEXT NOT NULL DEFAULT '',
                plural TEXT NOT NULL DEFAULT '',
                infinitive TEXT NOT NULL DEFAULT '',
                prateritum TEXT NOT NULL DEFAULT '',
                partizip2 TEXT NOT NULL DEFAULT '',
                komparativ TEXT NOT NULL DEFAULT '',
                superlativ TEXT NOT NULL DEFAULT '',
                russian TEXT NOT NULL DEFAULT '',
                ukrainian TEXT NOT NULL DEFAULT '',
                english TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                example TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '1',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO words_new (
                id, type, article, german, plural, infinitive, prateritum, partizip2, komparativ, superlativ,
                russian, ukrainian, english, image, example, topic, created_at
            )
            SELECT
                id, type, article, german, plural, infinitive, prateritum, partizip2, '', '',
                russian, ukrainian, english, '', '', '1', created_at
            FROM words;
            DROP TABLE words;
            ALTER TABLE words_new RENAME TO words;
            """
        )
        conn.execute("PRAGMA foreign_keys = ON")
        word_cols = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute("PRAGMA table_info(words)").fetchall()
        }
    if "ukrainian" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN ukrainian TEXT NOT NULL DEFAULT ''")
    if "english" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN english TEXT NOT NULL DEFAULT ''")
    if "topic" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN topic TEXT NOT NULL DEFAULT '1'")
    if "image" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN image TEXT NOT NULL DEFAULT ''")
    if "example" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN example TEXT NOT NULL DEFAULT ''")
    if "komparativ" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN komparativ TEXT NOT NULL DEFAULT ''")
    if "superlativ" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN superlativ TEXT NOT NULL DEFAULT ''")
    if "updated_at" not in word_cols:
        # SQLite forbids a non-constant default on ADD COLUMN, so add with '' then
        # backfill existing rows to their created_at (unedited == created time).
        conn.execute("ALTER TABLE words ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE words SET updated_at = created_at WHERE updated_at = ''")
    # spec #1 (v6 -> v7): preposition semantic category + governed case. Additive,
    # DEFAULT '' so every existing row (and an older/rolled-back build) stays valid;
    # values are a controlled vocabulary validated on write, never free text here.
    if "preposition_type" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN preposition_type TEXT NOT NULL DEFAULT ''")
    if "case_type" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN case_type TEXT NOT NULL DEFAULT ''")
    # spec #3 (v7 -> v8): conjugation blocks stored inline on the word (the block
    # is treated as one entity, no separate table). Additive, DEFAULT '' so every
    # existing row stays valid; content is validated by common.conjugation on write.
    if "conjugation_praesens" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN conjugation_praesens TEXT NOT NULL DEFAULT ''")
    if "conjugation_praeteritum" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN conjugation_praeteritum TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE words SET topic = '1' WHERE trim(coalesce(topic, '')) = ''")
    # spec #8: topics registry. Defensive create (mirrors vf_checks above) + idempotent
    # seed of every DISTINCT topic in use, with empty description. INSERT OR IGNORE keeps
    # existing descriptions (name is PK), so re-runs never clobber curated text and new
    # topics that appear in words are picked up automatically.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS topics (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO topics (name)
        SELECT DISTINCT trim(topic) FROM words WHERE trim(coalesce(topic, '')) != ''
        """
    )
    # spec #2: persisted VF-add batch job (survives restart). A job holds the
    # owner's word list; each item is fetched/parsed one at a time (slow-drip),
    # partial results stored so a restart resumes instead of starting over.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vf_add_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            topic TEXT NOT NULL DEFAULT '',
            total INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vf_add_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            word TEXT NOT NULL,
            word_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT NOT NULL DEFAULT '',
            flags TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # spec #2 (v11): word_id links a recheck item back to the existing word it verifies.
    vf_item_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(vf_add_items)").fetchall()
    }
    if "word_id" not in vf_item_cols:
        conn.execute("ALTER TABLE vf_add_items ADD COLUMN word_id TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vf_add_items_job ON vf_add_items(job_id, status)")

    _repair_word_fk_tables(conn)
    _ensure_indexes(conn)

    user_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "interface_lang" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN interface_lang TEXT NOT NULL DEFAULT 'en'")
    if "dict_lang" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN dict_lang TEXT NOT NULL DEFAULT 'ru'")

    form_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(form_stats)").fetchall()
    }
    for field in ("article", "plural", "prateritum", "partizip2"):
        stage_col = f"{field}_review_stage"
        next_col = f"{field}_next_review"
        active_col = f"{field}_active"
        if stage_col not in form_cols:
            conn.execute(f"ALTER TABLE form_stats ADD COLUMN {stage_col} INTEGER NOT NULL DEFAULT 0")
        if next_col not in form_cols:
            conn.execute(f"ALTER TABLE form_stats ADD COLUMN {next_col} TEXT DEFAULT NULL")
        # v4 -> v5: persistent weak-episode flag. Backfill rows that were already
        # in an episode under the legacy (pre-flag) entry rule so the migration
        # never drops a word that was active when it ran (docs §0.2 item 1).
        if active_col not in form_cols:
            conn.execute(f"ALTER TABLE form_stats ADD COLUMN {active_col} INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                f"UPDATE form_stats SET {active_col} = 1 "
                f"WHERE {field}_errors >= {WEAK_LEGACY_ACTIVE_MIN_ERRORS} "
                f"OR {field}_streak_wrong >= {WEAK_ACTIVE_MIN_STREAK_WRONG}"
            )

    quiz_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(quiz_events)").fetchall()
    }
    if "review_stage_snapshot" not in quiz_cols:
        conn.execute("ALTER TABLE quiz_events ADD COLUMN review_stage_snapshot INTEGER")

    batch_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(learning_batches)").fetchall()
    }
    if batch_cols:
        if "ready_since" not in batch_cols:
            conn.execute("ALTER TABLE learning_batches ADD COLUMN ready_since TEXT DEFAULT NULL")
        if "last_notified_at" not in batch_cols:
            conn.execute("ALTER TABLE learning_batches ADD COLUMN last_notified_at TEXT DEFAULT NULL")
        if "updated_at" not in batch_cols:
            conn.execute("ALTER TABLE learning_batches ADD COLUMN updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")

    learning_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(learning_items)").fetchall()
    }
    if learning_cols and "preview_done" not in learning_cols:
        conn.execute("ALTER TABLE learning_items ADD COLUMN preview_done INTEGER NOT NULL DEFAULT 0")
    if learning_cols and "updated_at" not in learning_cols:
        conn.execute("ALTER TABLE learning_items ADD COLUMN updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")

    # spec #7 (v8 -> v9): per-user personal translation overrides live in the SAME
    # user_word_media row as personal image/example (no new table/rows). A user
    # overrides only their dict_lang's translation; '' = no personal version.
    media_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(user_word_media)").fetchall()
    }
    for _tr_col in ("russian", "ukrainian", "english"):
        if _tr_col not in media_cols:
            conn.execute(f"ALTER TABLE user_word_media ADD COLUMN {_tr_col} TEXT NOT NULL DEFAULT ''")

    # Migrate legacy SM2-named settings keys to FSRS names, preserving values.
    for _old_key, _new_key in (
        ("sm2_report_utc_hour", "fsrs_report_utc_hour"),
        ("sm2_report_date", "fsrs_report_date"),
        ("sm2_peak_notify_date", "fsrs_peak_notify_date"),
        ("sm2_weekly_report_week", "fsrs_weekly_report_week"),
        ("sm2_peak_hour_today", "fsrs_peak_hour_today"),
    ):
        old_row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_old_key,)).fetchone()
        if old_row is None:
            continue
        new_row = conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (_new_key,)).fetchone()
        if new_row is None:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                (_new_key, old_row["value"]),
            )
        conn.execute("DELETE FROM app_settings WHERE key = ?", (_old_key,))


def _normalize_text(text):
    """Handle normalize text."""
    return " ".join(str(text).strip().lower().split())


def _split_variants(text):
    """Handle split variants."""
    raw = _safe_text(text)
    for delim in (",", ";", "/"):
        raw = raw.replace(delim, "|")
    return [part.strip() for part in raw.split("|") if part.strip()]


def _form_variants(text):
    """Handle form variants."""
    values = set()
    for part in _split_variants(text):
        normalized = _normalize_text(part)
        if normalized:
            values.add(normalized)
        tokens = normalized.split()
        if len(tokens) > 1 and tokens[0] in {"hat", "ist"}:
            values.add(" ".join(tokens[1:]))
    return values


def _safe_text(value):
    """Handle safe text."""
    if value is None:
        return ""
    return str(value)


def _vf_source_hash(entry):
    """Build deterministic hash source for VF checks."""
    entry_type = _safe_text(entry.get("type")).strip().lower()
    if entry_type == "nomen":
        raw = "|".join(
            [
                entry_type,
                _safe_text(entry.get("article")).strip().lower(),
                _safe_text(entry.get("german")).strip().lower(),
                _safe_text(entry.get("plural")).strip().lower(),
            ]
        )
    elif entry_type == "verb":
        raw = "|".join(
            [
                entry_type,
                _safe_text(entry.get("infinitive")).strip().lower(),
                _safe_text(entry.get("prateritum")).strip().lower(),
                _safe_text(entry.get("partizip2")).strip().lower(),
            ]
        )
    else:
        raw = entry_type
    return str(abs(hash(raw)))


def _safe_int(value, default=0):
    """Handle safe int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entry_key(entry):
    """Handle entry key."""
    entry_type = _normalize_text(entry.get("type", ""))
    if entry_type == "verb":
        return ("verb", _normalize_text(entry.get("infinitive", "")))
    return (entry_type or "nomen", _normalize_text(entry.get("german", "")))


def _row_to_entry(row):
    """Handle row to entry."""
    entry = {
        "id": row["id"],
        "type": row["type"],
        "article": row["article"] or "",
        "german": row["german"] or "",
        "plural": row["plural"] or "",
        "infinitive": row["infinitive"] or "",
        "prateritum": row["prateritum"] or "",
        "partizip2": row["partizip2"] or "",
        "komparativ": row["komparativ"] if "komparativ" in row.keys() else "",
        "superlativ": row["superlativ"] if "superlativ" in row.keys() else "",
        "russian": row["russian"] or "",
        "ukrainian": row["ukrainian"] or "",
        "english": row["english"] or "",
        "image": row["image"] if "image" in row.keys() else "",
        "example": row["example"] if "example" in row.keys() else "",
        "topic": row["topic"] or "",
        "preposition_type": row["preposition_type"] if "preposition_type" in row.keys() else "",
        "case_type": row["case_type"] if "case_type" in row.keys() else "",
        "conjugation_praesens": row["conjugation_praesens"] if "conjugation_praesens" in row.keys() else "",
        "conjugation_praeteritum": row["conjugation_praeteritum"] if "conjugation_praeteritum" in row.keys() else "",
    }
    if "seen_count" in row.keys():
        entry["seen_count"] = int(row["seen_count"] or 0)
    if "last_seen_at" in row.keys():
        entry["last_seen_at"] = row["last_seen_at"]
    if "review_stage" in row.keys():
        entry["review_stage"] = int(row["review_stage"] or 0)
    if "next_review" in row.keys():
        entry["next_review"] = row["next_review"]
    if "is_active" in row.keys():
        entry["is_active"] = bool(row["is_active"])
    if "wrong_streak" in row.keys():
        entry["wrong_streak"] = int(row["wrong_streak"] or 0)
    if "learning_step" in row.keys():
        entry["learning_step"] = int(row["learning_step"] or 0)
    if "learning_due" in row.keys():
        entry["learning_due"] = row["learning_due"]
    if "learning_batch_id" in row.keys():
        entry["learning_batch_id"] = row["learning_batch_id"] or ""
    if "learning_done_at" in row.keys():
        entry["learning_done_at"] = row["learning_done_at"] or ""
    if "has_non_learning_history" in row.keys():
        entry["has_non_learning_history"] = bool(row["has_non_learning_history"])
    return entry


def load_words(db_path):
    """Handle load words."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, komparativ, superlativ,
            russian, ukrainian, english, topic
            FROM words
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


# --- spec #3: conjugation storage + carousel selection ------------------------

# A verb enters the conjugation carousel once its L1>De FSRS row has at least
# this many repetitions (i.e. it's past the "new word" stage). Selection uses the
# exact L1>De mode string, which is language-dependent, so the caller passes it.
CONJUGATION_MIN_REPETITIONS = 2


def set_word_conjugation(db_path, word_id, praesens, praeteritum=""):
    """Write a verb's conjugation blocks, validating both against the canonical
    format. Empty string clears a block; any non-empty value must be a valid
    6-line block or ValueError is raised (single validator for every write path).
    """
    stored_praesens = parse_conjugation(praesens) if str(praesens or "").strip() else ""
    stored_praeteritum = parse_conjugation(praeteritum) if str(praeteritum or "").strip() else ""
    init_db(db_path)
    with _connect(db_path) as conn:
        # spec #3 invariant enforced at the single write chokepoint: only the closed
        # Präteritum list keeps a Präteritum block. Force-blank it for any other verb,
        # whatever the caller (apply script, VF parser, clarify) supplied.
        if stored_praeteritum:
            row = conn.execute("SELECT infinitive FROM words WHERE id = ?", (word_id,)).fetchone()
            if not takes_praeteritum(row["infinitive"] if row else ""):
                stored_praeteritum = ""
        conn.execute(
            "UPDATE words SET conjugation_praesens = ?, conjugation_praeteritum = ? WHERE id = ?",
            (stored_praesens, stored_praeteritum, word_id),
        )


def next_conjugation_word(db_path, user_id, mode, min_repetitions=CONJUGATION_MIN_REPETITIONS):
    """Return the next verb to show in the conjugation carousel, or None.

    Eligible = a verb with a non-empty Präsens block whose L1>De FSRS row for
    this user has repetitions >= min_repetitions (topic filter is intentionally
    NOT applied — spec #3). Order is a weighted rotation: unseen words first,
    then the one with the highest ``elapsed / (1 + seen_count)`` score, so a verb
    seen many times resurfaces proportionally less often. Endless, no daily cap.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum,
                   w.partizip2, w.komparativ, w.superlativ, w.russian, w.ukrainian, w.english,
                   w.image, w.example, w.topic, w.preposition_type, w.case_type,
                   w.conjugation_praesens, w.conjugation_praeteritum,
                   COALESCE(cp.seen_count, 0) AS seen_count, cp.last_seen_at AS last_seen_at
            FROM words w
            JOIN progress p
              ON p.word_id = w.id AND p.user_id = ? AND p.mode = ? AND p.repetitions >= ?
            LEFT JOIN conjugation_progress cp
              ON cp.word_id = w.id AND cp.user_id = ?
            WHERE w.type = 'verb' AND trim(COALESCE(w.conjugation_praesens, '')) != ''
            ORDER BY (cp.last_seen_at IS NULL) DESC,
                     (julianday('now') - julianday(cp.last_seen_at)) / (1 + COALESCE(cp.seen_count, 0)) DESC,
                     COALESCE(cp.seen_count, 0) ASC,
                     w.id ASC
            LIMIT 1
            """,
            (user_id, mode, min_repetitions, user_id),
        ).fetchone()
    return _row_to_entry(row) if row is not None else None


def record_conjugation_shown(db_path, user_id, word_id):
    """Mark a verb as shown to this user (the "далее" action): bump seen_count
    and stamp last_seen_at. Per-user row, created on first show.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO conjugation_progress (user_id, word_id, seen_count, last_seen_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                seen_count = seen_count + 1,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (user_id, word_id),
        )


def count_words(db_path):
    """Handle count words."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM words").fetchone()
    return int(row["cnt"])


def count_words_for_topics(db_path, topics=None):
    """Number of words matching a topic filter (spec #8).

    topics=None/empty means 'all' (no filter) -> every word. Used to tell 'no words
    under the selected topics at all' apart from 'words exist but none are due'.
    """
    init_db(db_path)
    clean = [_safe_text(x) for x in (topics or []) if _safe_text(x)]
    with _connect(db_path) as conn:
        if not clean:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM words").fetchone()
        else:
            placeholders = ",".join("?" for _ in clean)
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM words WHERE topic IN ({placeholders})", clean
            ).fetchone()
    return int(row["cnt"])


def list_topics(db_path):
    """Handle list topics."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT topic
            FROM words
            WHERE trim(coalesce(topic, '')) != ''
            ORDER BY topic
            """
        ).fetchall()
    return [row["topic"] for row in rows]


def list_topics_with_desc(db_path):
    """Topics registry (spec #8): list of (name, description) for every known topic.

    Superset of topics currently in use (init_db re-seeds DISTINCT words.topic on every
    call), so callers get the full button set plus any curated German descriptions.
    Unsorted here — the shared UI sort helper (_sorted_topics) orders name/buttons.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name, description FROM topics").fetchall()
    return [(row["name"], row["description"]) for row in rows]


def get_topic_description(db_path, name):
    """Curated description for a topic name, or '' if unknown/blank."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT description FROM topics WHERE name = ?", (str(name).strip(),)
        ).fetchone()
    return row["description"] if row else ""


def insert_topic_if_absent(db_path, name, description):
    """Add a new topic with its description; no-op if the name already exists.

    Returns True if a row was inserted (new topic), False if it already existed.
    Existing descriptions are never overwritten here (spec #8: descriptions are edited
    only by the one-off review script, never through the add-flow).
    """
    init_db(db_path)
    name = str(name).strip()
    if not name:
        return False
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO topics (name, description) VALUES (?, ?)",
            (name, str(description or "").strip()),
        )
        return cur.rowcount > 0


def get_statistics_snapshot(db_path, user_id, mode):
    """Handle get statistics snapshot."""
    init_db(db_path)
    user_id = int(user_id)
    mode = _safe_text(mode)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=7)  # next Monday (exclusive)
    ws = week_start.isoformat()
    we = week_end.isoformat()

    with _connect(db_path) as conn:
        total_words = int(conn.execute("SELECT COUNT(*) AS cnt FROM words").fetchone()["cnt"] or 0)
        weekly_words = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM words
                WHERE date(created_at) >= date(?) AND date(created_at) < date(?)
                """,
                (ws, we),
            ).fetchone()["cnt"]
            or 0
        )
        type_rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(LOWER(type)), ''), '-') AS t,
                COUNT(*) AS cnt
            FROM words
            GROUP BY t
            ORDER BY t
            """
        ).fetchall()
        word_types = {row["t"]: int(row["cnt"] or 0) for row in (type_rows or [])}

        streak_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN repetitions >= 5 THEN 1 ELSE 0 END) AS s5p,
                SUM(CASE WHEN repetitions = 4 THEN 1 ELSE 0 END) AS s4,
                SUM(CASE WHEN repetitions = 3 THEN 1 ELSE 0 END) AS s3,
                SUM(CASE WHEN repetitions = 2 THEN 1 ELSE 0 END) AS s2,
                SUM(CASE WHEN repetitions = 1 THEN 1 ELSE 0 END) AS s1
            FROM progress
            WHERE user_id = ? AND mode = ?
            """,
            (user_id, mode),
        ).fetchone()
        streak_5p = int(streak_row["s5p"] or 0) if streak_row else 0
        streak_4 = int(streak_row["s4"] or 0) if streak_row else 0
        streak_3 = int(streak_row["s3"] or 0) if streak_row else 0
        streak_2 = int(streak_row["s2"] or 0) if streak_row else 0
        streak_1 = int(streak_row["s1"] or 0) if streak_row else 0
        streak_3p = streak_3 + streak_4 + streak_5p
        wrong_fsrs = int(
            conn.execute(
                """
                WITH last_events AS (
                    SELECT
                        e.word_id AS word_id,
                        MAX(e.id) AS last_id
                    FROM quiz_events e
                    JOIN words w ON w.id = e.word_id
                    WHERE e.user_id = ?
                      AND e.mode = ?
                    GROUP BY e.word_id
                )
                SELECT COUNT(*) AS cnt
                FROM last_events le
                JOIN quiz_events e ON e.id = le.last_id
                WHERE e.is_correct = 0
                """,
                (user_id, mode),
            ).fetchone()["cnt"]
            or 0
        )

        weak_row = conn.execute(
            """
            SELECT
                SUM(
                    CASE
                        WHEN w.type = 'nomen' AND fs.article_active = 1 THEN 1
                        ELSE 0
                    END
                ) AS article_active,
                SUM(
                    CASE
                        WHEN w.type = 'nomen'
                         AND fs.article_review_stage = 1
                         AND fs.article_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS article_stage1,
                SUM(
                    CASE
                        WHEN w.type = 'nomen'
                         AND fs.article_review_stage = 2
                         AND fs.article_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS article_stage2,
                SUM(
                    CASE
                        WHEN w.type = 'nomen'
                         AND fs.article_review_stage = 3
                         AND fs.article_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS article_stage3,

                SUM(
                    CASE
                        WHEN w.type = 'nomen' AND fs.plural_active = 1 THEN 1
                        ELSE 0
                    END
                ) AS plural_active,
                SUM(
                    CASE
                        WHEN w.type = 'nomen'
                         AND fs.plural_review_stage = 1
                         AND fs.plural_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS plural_stage1,
                SUM(
                    CASE
                        WHEN w.type = 'nomen'
                         AND fs.plural_review_stage = 2
                         AND fs.plural_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS plural_stage2,
                SUM(
                    CASE
                        WHEN w.type = 'nomen'
                         AND fs.plural_review_stage = 3
                         AND fs.plural_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS plural_stage3,

                SUM(
                    CASE
                        WHEN w.type = 'verb' AND fs.partizip2_active = 1 THEN 1
                        ELSE 0
                    END
                ) AS partizip2_active,
                SUM(
                    CASE
                        WHEN w.type = 'verb'
                         AND fs.partizip2_review_stage = 1
                         AND fs.partizip2_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS partizip2_stage1,
                SUM(
                    CASE
                        WHEN w.type = 'verb'
                         AND fs.partizip2_review_stage = 2
                         AND fs.partizip2_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS partizip2_stage2,
                SUM(
                    CASE
                        WHEN w.type = 'verb'
                         AND fs.partizip2_review_stage = 3
                         AND fs.partizip2_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS partizip2_stage3,

                SUM(
                    CASE
                        WHEN w.type = 'verb' AND fs.prateritum_active = 1 THEN 1
                        ELSE 0
                    END
                ) AS prateritum_active,
                SUM(
                    CASE
                        WHEN w.type = 'verb'
                         AND fs.prateritum_review_stage = 1
                         AND fs.prateritum_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS prateritum_stage1,
                SUM(
                    CASE
                        WHEN w.type = 'verb'
                         AND fs.prateritum_review_stage = 2
                         AND fs.prateritum_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS prateritum_stage2,
                SUM(
                    CASE
                        WHEN w.type = 'verb'
                         AND fs.prateritum_review_stage = 3
                         AND fs.prateritum_active = 0 THEN 1
                        ELSE 0
                    END
                ) AS prateritum_stage3
            FROM form_stats fs
            JOIN words w ON w.id = fs.word_id
            WHERE fs.user_id = ?
            """,
            (user_id,),
        ).fetchone()
        weak_article = int(weak_row["article_active"] or 0) if weak_row else 0
        weak_plural = int(weak_row["plural_active"] or 0) if weak_row else 0
        weak_partizip2 = int(weak_row["partizip2_active"] or 0) if weak_row else 0
        weak_prateritum = int(weak_row["prateritum_active"] or 0) if weak_row else 0

        weak_article_stage1 = int(weak_row["article_stage1"] or 0) if weak_row else 0
        weak_article_stage2 = int(weak_row["article_stage2"] or 0) if weak_row else 0
        weak_article_stage3 = int(weak_row["article_stage3"] or 0) if weak_row else 0

        weak_plural_stage1 = int(weak_row["plural_stage1"] or 0) if weak_row else 0
        weak_plural_stage2 = int(weak_row["plural_stage2"] or 0) if weak_row else 0
        weak_plural_stage3 = int(weak_row["plural_stage3"] or 0) if weak_row else 0

        weak_partizip2_stage1 = int(weak_row["partizip2_stage1"] or 0) if weak_row else 0
        weak_partizip2_stage2 = int(weak_row["partizip2_stage2"] or 0) if weak_row else 0
        weak_partizip2_stage3 = int(weak_row["partizip2_stage3"] or 0) if weak_row else 0

        weak_prateritum_stage1 = int(weak_row["prateritum_stage1"] or 0) if weak_row else 0
        weak_prateritum_stage2 = int(weak_row["prateritum_stage2"] or 0) if weak_row else 0
        weak_prateritum_stage3 = int(weak_row["prateritum_stage3"] or 0) if weak_row else 0

        def _today_mode_count(mode_name):
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT e.word_id) AS cnt
                FROM quiz_events e
                WHERE e.user_id = ?
                  AND e.mode = ?
                  AND date(e.created_at) = date(?)
                """,
                (user_id, mode_name, today.isoformat()),
            ).fetchone()
            return int((row["cnt"] if row else 0) or 0)

        def _today_mode_prefix_count(prefix):
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT e.word_id) AS cnt
                FROM quiz_events e
                WHERE e.user_id = ?
                  AND e.mode LIKE ?
                  AND date(e.created_at) = date(?)
                """,
                (user_id, f"{prefix}%", today.isoformat()),
            ).fetchone()
            return int((row["cnt"] if row else 0) or 0)

        # Count today's FSRS events for the user's OWN quiz direction, not a
        # hardcoded ru_de/de_ru: a uk/en-dictionary user answers under uk_de/de_uk
        # etc., so hardcoding ru_de would always show 0 for them. `mode` is the
        # user's L1>De mode (e.g. "uk_de"); derive the De>L1 mode from it.
        de_l1_mode = "de_" + mode[:-3] if mode.endswith("_de") else mode
        today_complete_fsrs_ru_de = _today_mode_count(mode)
        today_complete_fsrs_de_ru = _today_mode_count(de_l1_mode)
        today_complete_pl_sg = _today_mode_prefix_count("pl_sg_")
        today_complete_weak_article = _today_mode_count("weak_article")
        today_complete_weak_plural = _today_mode_count("weak_plural")
        today_complete_weak_partizip2 = _today_mode_count("weak_partizip2")
        today_complete_weak_prateritum = _today_mode_count("weak_prateritum")
        today_complete = today_complete_fsrs_ru_de + today_complete_fsrs_de_ru

        retention_row = conn.execute(
            """
            WITH first_touch AS (
                SELECT
                    word_id,
                    MIN(created_at) AS first_ts
                FROM quiz_events
                WHERE user_id = ? AND mode = ?
                GROUP BY word_id
            ),
            eligible AS (
                SELECT word_id, first_ts
                FROM first_touch
                WHERE julianday('now') - julianday(first_ts) >= 7
            ),
            passed AS (
                SELECT DISTINCT e.word_id
                FROM eligible ft
                JOIN quiz_events e
                  ON e.user_id = ?
                 AND e.mode = ?
                 AND e.word_id = ft.word_id
                 AND e.is_correct = 1
                 AND julianday(e.created_at) - julianday(ft.first_ts) >= 7
            )
            SELECT
                (SELECT COUNT(*) FROM eligible) AS eligible_cnt,
                (SELECT COUNT(*) FROM passed) AS passed_cnt
            """,
            (user_id, mode, user_id, mode),
        ).fetchone()
        retention_eligible = int(retention_row["eligible_cnt"] or 0) if retention_row else 0
        retention_passed = int(retention_row["passed_cnt"] or 0) if retention_row else 0

        new_words_tested = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM words w
                WHERE date(w.created_at) >= date(?)
                  AND date(w.created_at) < date(?)
                  AND EXISTS (
                      SELECT 1
                      FROM quiz_events e
                      WHERE e.user_id = ?
                        AND e.word_id = w.id
                        AND date(e.created_at) >= date(?)
                        AND date(e.created_at) < date(?)
                  )
                """,
                (ws, we, user_id, ws, we),
            ).fetchone()["cnt"]
            or 0
        )

        learning_weekly = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT li.word_id) AS cnt
                FROM learning_items li
                WHERE li.user_id = ?
                  AND li.status IN ('completed', 'skipped')
                  AND date(li.updated_at) >= date(?)
                  AND date(li.updated_at) < date(?)
                """,
                (user_id, ws, we),
            ).fetchone()["cnt"]
            or 0
        )

        weak_resolved = int(
            conn.execute(
                """
                WITH solved_candidates AS (
                    SELECT
                        word_id,
                        mode,
                        SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS c
                    FROM quiz_events
                    WHERE user_id = ?
                      AND mode IN ('weak_article', 'weak_plural', 'weak_partizip2', 'weak_prateritum')
                      AND date(created_at) >= date(?)
                      AND date(created_at) < date(?)
                    GROUP BY word_id, mode
                    HAVING c >= 3
                )
                SELECT COUNT(DISTINCT s.word_id) AS cnt
                FROM solved_candidates s
                JOIN form_stats fs ON fs.user_id = ? AND fs.word_id = s.word_id
                WHERE
                    (s.mode = 'weak_article' AND fs.article_errors = 0 AND fs.article_streak_wrong = 0)
                    OR (s.mode = 'weak_plural' AND fs.plural_errors = 0 AND fs.plural_streak_wrong = 0)
                    OR (s.mode = 'weak_partizip2' AND fs.partizip2_errors = 0 AND fs.partizip2_streak_wrong = 0)
                    OR (s.mode = 'weak_prateritum' AND fs.prateritum_errors = 0 AND fs.prateritum_streak_wrong = 0)
                """,
                (user_id, ws, we, user_id),
            ).fetchone()["cnt"]
            or 0
        )
        never_answered = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM words w
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM quiz_events e
                    WHERE e.user_id = ?
                      AND e.word_id = w.id
                )
                """,
                (user_id,),
            ).fetchone()["cnt"]
            or 0
        )

    retention_7d_pct = round((retention_passed / retention_eligible) * 100, 1) if retention_eligible > 0 else 0.0
    return {
        "total_words": total_words,
        "weekly_words": weekly_words,
        "word_types": word_types,
        "streak_5p": streak_5p,
        "streak_4": streak_4,
        "streak_3": streak_3,
        "streak_3p": streak_3p,
        "streak_2": streak_2,
        "streak_1": streak_1,
        "wrong_fsrs": wrong_fsrs,
        "weak_article": weak_article,
        "weak_article_stage1": weak_article_stage1,
        "weak_article_stage2": weak_article_stage2,
        "weak_article_stage3": weak_article_stage3,
        "weak_plural": weak_plural,
        "weak_plural_stage1": weak_plural_stage1,
        "weak_plural_stage2": weak_plural_stage2,
        "weak_plural_stage3": weak_plural_stage3,
        "weak_partizip2": weak_partizip2,
        "weak_partizip2_stage1": weak_partizip2_stage1,
        "weak_partizip2_stage2": weak_partizip2_stage2,
        "weak_partizip2_stage3": weak_partizip2_stage3,
        "weak_prateritum": weak_prateritum,
        "weak_prateritum_stage1": weak_prateritum_stage1,
        "weak_prateritum_stage2": weak_prateritum_stage2,
        "weak_prateritum_stage3": weak_prateritum_stage3,
        "today_complete": today_complete,
        "today_complete_fsrs_ru_de": today_complete_fsrs_ru_de,
        "today_complete_fsrs_de_ru": today_complete_fsrs_de_ru,
        "today_complete_pl_sg": today_complete_pl_sg,
        "today_complete_weak_article": today_complete_weak_article,
        "today_complete_weak_plural": today_complete_weak_plural,
        "today_complete_weak_partizip2": today_complete_weak_partizip2,
        "today_complete_weak_prateritum": today_complete_weak_prateritum,
        "never_answered": never_answered,
        "retention_7d_pct": retention_7d_pct,
        "new_words_tested_week": new_words_tested,
        "learning_weekly": learning_weekly,
        "weak_resolved_week": weak_resolved,
    }


def get_daily_first_touch_retention(db_path, user_id, target_date=None, modes=None):
    """Handle daily first touch retention for selected quiz modes."""
    init_db(db_path)
    user_id = int(user_id)
    day = target_date or date.today().isoformat()
    selected_modes = tuple(modes or (QUIZ_RU_DE, QUIZ_DE_RU))
    if not selected_modes:
        return {"date": day, "total": 0, "correct": 0, "rate": 0.0}
    placeholders = ",".join("?" for _ in selected_modes)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            WITH day_events AS (
                SELECT id, word_id, is_correct, created_at
                FROM quiz_events
                WHERE user_id = ?
                  AND mode IN ({placeholders})
                  AND date(created_at) = date(?)
            ),
            first_ts AS (
                SELECT word_id, MIN(created_at) AS first_created_at
                FROM day_events
                GROUP BY word_id
            ),
            first_events AS (
                SELECT de.id, de.word_id, de.is_correct
                FROM day_events de
                JOIN first_ts ft
                  ON ft.word_id = de.word_id
                 AND ft.first_created_at = de.created_at
            ),
            picked AS (
                SELECT word_id, MIN(id) AS first_id
                FROM first_events
                GROUP BY word_id
            )
            SELECT
                COUNT(*) AS total_cnt,
                SUM(CASE WHEN fe.is_correct = 1 THEN 1 ELSE 0 END) AS correct_cnt
            FROM picked p
            JOIN first_events fe ON fe.id = p.first_id
            """,
            (user_id, *selected_modes, day),
        ).fetchone()
    total = int((row["total_cnt"] if row else 0) or 0)
    correct = int((row["correct_cnt"] if row else 0) or 0)
    rate = round((correct / total) * 100, 1) if total > 0 else 0.0
    return {"date": day, "total": total, "correct": correct, "rate": rate}


def get_weekly_first_touch_retention(db_path, user_id, week_start=None, modes=None):
    """First-touch retention for a calendar week [Mon..Mon)."""
    init_db(db_path)
    user_id = int(user_id)
    selected_modes = tuple(modes or (QUIZ_RU_DE, QUIZ_DE_RU))
    if not selected_modes:
        return {"week_start": "", "week_end": "", "total": 0, "correct": 0, "rate": 0.0}
    if week_start:
        ws = date.fromisoformat(str(week_start))
    else:
        today = date.today()
        ws = today - timedelta(days=today.weekday())
    we = ws + timedelta(days=7)
    ws_iso = ws.isoformat()
    we_iso = we.isoformat()
    placeholders = ",".join("?" for _ in selected_modes)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            WITH week_events AS (
                SELECT id, word_id, is_correct, created_at
                FROM quiz_events
                WHERE user_id = ?
                  AND mode IN ({placeholders})
                  AND date(created_at) >= date(?)
                  AND date(created_at) < date(?)
            ),
            first_ts AS (
                SELECT word_id, MIN(created_at) AS first_created_at
                FROM week_events
                GROUP BY word_id
            ),
            first_rows AS (
                SELECT e.word_id, e.is_correct, e.id
                FROM week_events e
                JOIN first_ts f
                  ON f.word_id = e.word_id
                 AND f.first_created_at = e.created_at
            ),
            first_pick AS (
                SELECT word_id, is_correct
                FROM first_rows
                WHERE id IN (SELECT MIN(id) FROM first_rows GROUP BY word_id)
            )
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM first_pick
            """,
            (user_id, *selected_modes, ws_iso, we_iso),
        ).fetchone()
    total = int(row["total"] or 0) if row else 0
    correct = int(row["correct"] or 0) if row else 0
    rate = round((correct / total) * 100, 1) if total > 0 else 0.0
    return {"week_start": ws_iso, "week_end": we_iso, "total": total, "correct": correct, "rate": rate}


def count_learning_done_in_week(db_path, user_id, week_start=None):
    """Count distinct words that finished learning (completed/skipped) in a week [Mon..Mon)."""
    init_db(db_path)
    if week_start:
        ws = date.fromisoformat(str(week_start))
    else:
        today = date.today()
        ws = today - timedelta(days=today.weekday())
    we = ws + timedelta(days=7)
    with _connect(db_path) as conn:
        cnt = conn.execute(
            """
            SELECT COUNT(DISTINCT li.word_id) AS cnt
            FROM learning_items li
            WHERE li.user_id = ?
              AND li.status IN ('completed', 'skipped')
              AND date(li.updated_at) >= date(?)
              AND date(li.updated_at) < date(?)
            """,
            (int(user_id), ws.isoformat(), we.isoformat()),
        ).fetchone()["cnt"]
    return int(cnt or 0)


def snapshot_user_word_state(db_path, user_id, word_id, modes=None):
    """Handle snapshot user word state for rollback."""
    init_db(db_path)
    user_id = int(user_id)
    word_id = _safe_text(word_id)
    selected_modes = [str(m) for m in (modes or []) if str(m)]
    with _connect(db_path) as conn:
        progress_rows = {}
        for mode in selected_modes:
            row = conn.execute(
                """
                SELECT user_id, word_id, mode, interval, repetitions, ease, due, updated_at
                FROM progress
                WHERE user_id = ? AND word_id = ? AND mode = ?
                """,
                (user_id, word_id, mode),
            ).fetchone()
            progress_rows[mode] = dict(row) if row else None

        fs_row = conn.execute(
            "SELECT * FROM form_stats WHERE user_id = ? AND word_id = ?",
            (user_id, word_id),
        ).fetchone()
        li_row = conn.execute(
            "SELECT * FROM learning_items WHERE user_id = ? AND word_id = ?",
            (user_id, word_id),
        ).fetchone()

        event_counts = {}
        for mode in selected_modes:
            cnt = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM quiz_events
                WHERE user_id = ? AND word_id = ? AND mode = ?
                """,
                (user_id, word_id, mode),
            ).fetchone()["cnt"]
            event_counts[mode] = int(cnt or 0)

    return {
        "user_id": user_id,
        "word_id": word_id,
        "modes": selected_modes,
        "progress": progress_rows,
        "form_stats": dict(fs_row) if fs_row else None,
        "learning_item": dict(li_row) if li_row else None,
        "event_counts": event_counts,
    }


def restore_user_word_state(db_path, snapshot):
    """Handle restore user word state from rollback snapshot."""
    init_db(db_path)
    if not isinstance(snapshot, dict):
        return False
    user_id = int(snapshot.get("user_id") or 0)
    word_id = _safe_text(snapshot.get("word_id"))
    modes = [str(m) for m in (snapshot.get("modes") or []) if str(m)]
    progress_rows = snapshot.get("progress") or {}
    event_counts = snapshot.get("event_counts") or {}
    with _connect(db_path) as conn:
        for mode in modes:
            row = progress_rows.get(mode)
            if row:
                conn.execute(
                    """
                    INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, word_id, mode) DO UPDATE SET
                        interval = excluded.interval,
                        repetitions = excluded.repetitions,
                        ease = excluded.ease,
                        due = excluded.due,
                        updated_at = excluded.updated_at
                    """,
                    (
                        int(row.get("user_id") or user_id),
                        _safe_text(row.get("word_id") or word_id),
                        _safe_text(row.get("mode") or mode),
                        int(row.get("interval") or 1),
                        int(row.get("repetitions") or 0),
                        float(row.get("ease") or DEFAULT_EASE),
                        _safe_text(row.get("due") or date.today().isoformat()),
                        _safe_text(row.get("updated_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM progress WHERE user_id = ? AND word_id = ? AND mode = ?",
                    (user_id, word_id, mode),
                )

        fs_row = snapshot.get("form_stats")
        if fs_row:
            cols = [c for c in fs_row.keys()]
            placeholders = ",".join("?" for _ in cols)
            update_cols = ",".join(f"{c}=excluded.{c}" for c in cols if c not in {"user_id", "word_id"})
            conn.execute(
                f"""
                INSERT INTO form_stats ({",".join(cols)})
                VALUES ({placeholders})
                ON CONFLICT(user_id, word_id) DO UPDATE SET {update_cols}
                """,
                tuple(fs_row[c] for c in cols),
            )
        else:
            conn.execute(
                "DELETE FROM form_stats WHERE user_id = ? AND word_id = ?",
                (user_id, word_id),
            )

        li_row = snapshot.get("learning_item")
        if li_row:
            cols = [c for c in li_row.keys()]
            placeholders = ",".join("?" for _ in cols)
            update_cols = ",".join(f"{c}=excluded.{c}" for c in cols if c not in {"user_id", "word_id"})
            conn.execute(
                f"""
                INSERT INTO learning_items ({",".join(cols)})
                VALUES ({placeholders})
                ON CONFLICT(user_id, word_id) DO UPDATE SET {update_cols}
                """,
                tuple(li_row[c] for c in cols),
            )
        else:
            conn.execute(
                "DELETE FROM learning_items WHERE user_id = ? AND word_id = ?",
                (user_id, word_id),
            )

        for mode in modes:
            target = int(event_counts.get(mode, 0) or 0)
            current = int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM quiz_events WHERE user_id = ? AND word_id = ? AND mode = ?",
                    (user_id, word_id, mode),
                ).fetchone()["cnt"]
                or 0
            )
            if current > target:
                to_delete = current - target
                ids = conn.execute(
                    """
                    SELECT id
                    FROM quiz_events
                    WHERE user_id = ? AND word_id = ? AND mode = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (user_id, word_id, mode, to_delete),
                ).fetchall()
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"DELETE FROM quiz_events WHERE id IN ({placeholders})",
                        tuple(int(r["id"]) for r in ids),
                    )
    return True


def add_transaction_log(db_path, user_id, action, payload):
    """Handle add transaction log."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO transaction_log (user_id, action, payload)
            VALUES (?, ?, ?)
            """,
            (int(user_id), _safe_text(action), json.dumps(payload, ensure_ascii=False)),
        )


def list_transaction_logs(db_path, user_id, limit=10):
    """Handle list transaction logs."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, action, payload, created_at
            FROM transaction_log
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        result.append(item)
    return result


def delete_transaction_logs(db_path, user_id, ids):
    """Handle delete transaction logs."""
    ids = [int(x) for x in (ids or [])]
    if not ids:
        return
    init_db(db_path)
    placeholders = ",".join("?" for _ in ids)
    with _connect(db_path) as conn:
        conn.execute(
            f"DELETE FROM transaction_log WHERE user_id = ? AND id IN ({placeholders})",
            (int(user_id), *ids),
        )


def cleanup_transaction_logs(db_path, days=7):
    """Handle cleanup transaction logs older than N days."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM transaction_log WHERE datetime(created_at) < datetime('now', ?)",
            (f"-{int(days)} days",),
        )


def _initial_user_status(user_id):
    """Status for a freshly-created users row: 'pending', EXCEPT the owner, who is
    never pending (#11). is_owner() overrides status everywhere, and a pending
    owner row used to surface in the access-requests digest — spamming the owner
    with a request to approve himself. Any upsert that may create the owner's row
    (language/registration) goes through this so the owner row is born 'approved'.
    """
    return "approved" if is_owner(user_id) else "pending"


def register_or_update_user(db_path, user_id, username="", first_name=""):
    """Handle register or update user."""
    init_db(db_path)
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT status FROM users WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (_safe_text(username), _safe_text(first_name), int(user_id)),
            )
            _ensure_default_user_levels(conn, int(user_id))
            return existing["status"]

        status = _initial_user_status(user_id)
        conn.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (int(user_id), _safe_text(username), _safe_text(first_name), status),
        )
        _ensure_default_user_levels(conn, int(user_id))
    return status


def get_user_status(db_path, user_id):
    """Handle get user status."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM users WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    return row["status"]


def set_user_status(db_path, user_id, status):
    """Handle set user status."""
    init_db(db_path)
    status = _safe_text(status).strip().lower()
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("Invalid user status.")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, created_at, updated_at)
            VALUES (?, '', '', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(user_id), status),
        )
        _ensure_default_user_levels(conn, int(user_id))


def _ensure_default_user_levels(conn, user_id):
    """Handle ensure default user levels."""
    rows = conn.execute(
        "SELECT level FROM user_levels WHERE user_id = ?",
        (int(user_id),),
    ).fetchall()
    if rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO user_levels (user_id, level) VALUES (?, ?)",
        [(int(user_id), level) for level in CEFR_LEVELS],
    )


def get_user_levels(db_path, user_id):
    """Handle get user levels."""
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_default_user_levels(conn, int(user_id))
        rows = conn.execute(
            "SELECT level FROM user_levels WHERE user_id = ? ORDER BY level",
            (int(user_id),),
        ).fetchall()
    levels = {row["level"] for row in rows}
    return [level for level in CEFR_LEVELS if level in levels]


def toggle_user_level(db_path, user_id, level):
    """Handle toggle user level."""
    level = _normalize_level(level)
    if not level:
        raise ValueError("Invalid CEFR level.")
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_default_user_levels(conn, int(user_id))
        exists = conn.execute(
            "SELECT 1 FROM user_levels WHERE user_id = ? AND level = ?",
            (int(user_id), level),
        ).fetchone()
        all_rows = conn.execute(
            "SELECT level FROM user_levels WHERE user_id = ?",
            (int(user_id),),
        ).fetchall()
        count = len(all_rows)
        if exists and count <= 1:
            return False, [row["level"] for row in all_rows]
        if exists:
            conn.execute(
                "DELETE FROM user_levels WHERE user_id = ? AND level = ?",
                (int(user_id), level),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO user_levels (user_id, level) VALUES (?, ?)",
                (int(user_id), level),
            )
    return True, get_user_levels(db_path, user_id)


def get_user_info(db_path, user_id):
    """Handle get user info."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at
            FROM users WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": _safe_int(row["user_id"]),
        "username": row["username"] or "",
        "first_name": row["first_name"] or "",
        "status": row["status"] or "pending",
        "interface_lang": row["interface_lang"] or "",
        "dict_lang": row["dict_lang"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def list_pending_users(db_path):
    """Users with status 'pending' (awaiting the owner's approval).

    The owner is always excluded (#11): is_owner overrides status, so the owner is
    never a real applicant — and a stray pending owner row must not show up in the
    access-requests digest/reminder (it would ask the owner to approve himself).
    """
    init_db(db_path)
    owner_id = int(get_owner_id() or 0)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, first_name, created_at
            FROM users WHERE status = 'pending' AND user_id != ?
            ORDER BY created_at
            """,
            (owner_id,),
        ).fetchall()
    return [
        {
            "user_id": _safe_int(row["user_id"]),
            "username": row["username"] or "",
            "first_name": row["first_name"] or "",
            "created_at": row["created_at"] or "",
        }
        for row in rows
    ]


def list_active_users(db_path):
    """User ids that should receive broadcasts (P5): every 'approved' user plus
    the owner (who may not have an 'approved' row). Distinct, owner included,
    sorted; 0/empty ids dropped.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE status = 'approved'",
        ).fetchall()
    ids = {_safe_int(row["user_id"]) for row in rows}
    owner_id = get_owner_id()
    if owner_id:
        ids.add(int(owner_id))
    ids.discard(0)
    return sorted(ids)


def list_user_activity(db_path):
    """Approved users with their activity counters (spec #11 owner [users] view):
    total `quiz_events` (every action, the "big number" already surfaced on the
    owner's own stat screen) and words moved from learning into FSRS
    (`learning_items.status IN ('completed','skipped')` — both count, skipped
    included per the owner's decision 2026-07-03). Sorted by quiz_events count
    descending (most active first); pending/blocked users are excluded. The
    owner shows up here too, as a normal row, if their own users-row is approved.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                u.user_id AS user_id,
                u.username AS username,
                u.first_name AS first_name,
                COALESCE(qe.cnt, 0) AS quiz_events_count,
                COALESCE(li.cnt, 0) AS learning_done_count
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS cnt FROM quiz_events GROUP BY user_id
            ) qe ON qe.user_id = u.user_id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS cnt FROM learning_items
                WHERE status IN ('completed', 'skipped')
                GROUP BY user_id
            ) li ON li.user_id = u.user_id
            WHERE u.status = 'approved'
            ORDER BY quiz_events_count DESC, u.user_id ASC
            """
        ).fetchall()
    return [
        {
            "user_id": _safe_int(row["user_id"]),
            "username": row["username"] or "",
            "first_name": row["first_name"] or "",
            "quiz_events_count": int(row["quiz_events_count"] or 0),
            "learning_done_count": int(row["learning_done_count"] or 0),
        }
        for row in rows
    ]


# --- Personal example/image (P4): a guest's per-word value, owner edits stay in
# the shared `words`. A guest sees ONLY their personal image/example (blank when
# unset) — never the owner's shared values. The owner sees the shared word. ---

# spec #7: which words-translation column a user's dict_lang overrides.
_DICT_TRANSLATION_COLUMN = {"ru": "russian", "uk": "ukrainian", "en": "english"}


def get_user_word_media(db_path, user_id, word_id):
    """This user's personal image/example (+ per-lang translations) for a word; {} if none set."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT image, example, russian, ukrainian, english FROM user_word_media "
            "WHERE user_id = ? AND word_id = ?",
            (int(user_id), str(word_id)),
        ).fetchone()
    if not row:
        return {}
    return {
        "image": row["image"] or "",
        "example": row["example"] or "",
        "russian": row["russian"] or "",
        "ukrainian": row["ukrainian"] or "",
        "english": row["english"] or "",
    }


def set_user_word_translation(db_path, user_id, word_id, dict_lang, value):
    """Upsert this user's personal translation into their dict_lang column (spec #7).

    Writes to the column matching dict_lang (ru->russian, uk->ukrainian, en->english);
    '' clears the override (reverts to the shared dictionary). Reuses the existing
    user_word_media row (same one as personal image/example) — no new rows.
    """
    col = _DICT_TRANSLATION_COLUMN.get(_normalize_dict_lang(dict_lang))
    if not col:
        raise ValueError("Invalid dict language for personal translation.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_word_media (user_id, word_id) VALUES (?, ?)",
            (int(user_id), str(word_id)),
        )
        conn.execute(
            f"UPDATE user_word_media SET {col} = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE user_id = ? AND word_id = ?",
            (_safe_text(value), int(user_id), str(word_id)),
        )


def set_user_word_media(db_path, user_id, word_id, image=None, example=None):
    """Upsert this user's personal image/example for a word (only given fields)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_word_media (user_id, word_id) VALUES (?, ?)",
            (int(user_id), str(word_id)),
        )
        sets, params = [], []
        if image is not None:
            sets.append("image = ?")
            params.append(_safe_text(image))
        if example is not None:
            sets.append("example = ?")
            params.append(_safe_text(example))
        if sets:
            params.extend([int(user_id), str(word_id)])
            conn.execute(
                f"UPDATE user_word_media SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP "
                f"WHERE user_id = ? AND word_id = ?",
                params,
            )


def apply_user_word_media(db_path, user_id, entry):
    """Apply the per-user view of image/example to a word entry.

    The owner sees the shared word as-is. A guest sees ONLY their personal
    image/example (blank when unset) — the owner's shared image/example are never
    shown to a guest. The input entry is left unchanged.
    """
    if not user_id or not entry or not entry.get("id"):
        return entry
    if is_owner(user_id):
        return entry
    media = get_user_word_media(db_path, user_id, entry.get("id"))
    out = dict(entry)
    out["image"] = media.get("image", "")
    out["example"] = media.get("example", "")
    # spec #7: personal translation override for the user's dict_lang. Kept in a
    # SEPARATE 'my_translation' key so the base russian/ukrainian/english fields
    # stay intact — display prefers my_translation, while the De>L1 checker can
    # still accept the base translation too (both count, per owner's decision).
    _, dict_lang = get_user_languages(db_path, user_id)
    col = _DICT_TRANSLATION_COLUMN.get(dict_lang)
    out["my_translation"] = media.get(col, "") if col else ""
    return out


def _normalize_interface_lang(value):
    """Handle normalize interface lang."""
    token = _safe_text(value).strip().lower()
    return token if token in INTERFACE_LANGS else ""


def _normalize_dict_lang(value):
    """Handle normalize dict lang."""
    token = _safe_text(value).strip().lower()
    return token if token in DICT_LANGS else ""


def get_user_languages(db_path, user_id):
    """Handle get user languages."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT interface_lang, dict_lang FROM users WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return "", ""
    return _normalize_interface_lang(row["interface_lang"]), _normalize_dict_lang(row["dict_lang"])


def set_user_interface_lang(db_path, user_id, interface_lang):
    """Handle set user interface lang."""
    value = _normalize_interface_lang(interface_lang)
    if not value:
        raise ValueError("Invalid interface language.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at)
            VALUES (?, '', '', ?, ?, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                interface_lang = excluded.interface_lang,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(user_id), _initial_user_status(user_id), value),
        )
        _ensure_default_user_levels(conn, int(user_id))


def set_user_dict_lang(db_path, user_id, dict_lang):
    """Handle set user dict lang."""
    value = _normalize_dict_lang(dict_lang)
    if not value:
        raise ValueError("Invalid dictionary language.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at)
            VALUES (?, '', '', ?, '', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                dict_lang = excluded.dict_lang,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(user_id), _initial_user_status(user_id), value),
        )
        _ensure_default_user_levels(conn, int(user_id))


def ensure_default_languages_for_existing_users(db_path, interface_lang="en", dict_lang="ru"):
    """Handle ensure default languages for existing users."""
    ui = _normalize_interface_lang(interface_lang) or "en"
    dl = _normalize_dict_lang(dict_lang) or "ru"
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE users
            SET interface_lang = ?,
                dict_lang = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE status != 'pending'
              AND (trim(coalesce(interface_lang, '')) = '' OR trim(coalesce(dict_lang, '')) = '')
            """,
            (ui, dl),
        )


def set_setting(db_path, key, value):
    """Handle set setting."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_safe_text(key), _safe_text(value)),
        )


def get_setting(db_path, key, default=None):
    """Handle get setting."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_safe_text(key),),
        ).fetchone()
    if not row:
        return default
    return row["value"]


def set_user_setting(db_path, user_id, key, value):
    """Upsert a per-user setting (user_settings table)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (int(user_id), _safe_text(key), _safe_text(value)),
        )


def get_user_setting(db_path, user_id, key, default=None):
    """This user's setting value, or `default` if they have none set.

    Per-user override layer over global defaults: the caller passes the global
    default; an unset key simply falls back to it. Strictly user-scoped — never
    sees another user's value.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (int(user_id), _safe_text(key)),
        ).fetchone()
    if not row:
        return default
    return row["value"]


def reset_user_settings(db_path, user_id, keys=None):
    """Reset some (keys given) or ALL of a user's settings to defaults.

    Deletes only that user's rows → the getter then falls back to the global
    default. Strictly user-scoped (never touches another user / the owner).
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        if keys:
            clean = [_safe_text(k) for k in keys]
            placeholders = ",".join("?" for _ in clean)
            conn.execute(
                f"DELETE FROM user_settings WHERE user_id = ? AND key IN ({placeholders})",
                (int(user_id), *clean),
            )
        else:
            conn.execute("DELETE FROM user_settings WHERE user_id = ?", (int(user_id),))


def reset_periodicity(db_path, user_id):
    """Reset the SR schedule for ONE user — deletes only their progress rows.

    Multiuser-safe: scoped by user_id so a reset never wipes other users' progress.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM progress WHERE user_id = ?", (int(user_id),))


def add_banned_word(db_path, raw_word):
    """Handle add banned word."""
    normalized = _normalize_text(raw_word)
    if not normalized:
        return False
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO banned_words (normalized_word, raw_word)
            VALUES (?, ?)
            """,
            (normalized, _safe_text(raw_word).strip()),
        )
        row = conn.execute(
            "SELECT changes() AS cnt"
        ).fetchone()
    return bool(row and int(row["cnt"]) > 0)


def is_banned_word(db_path, raw_word):
    """Handle is banned word."""
    normalized = _normalize_text(raw_word)
    if not normalized:
        return False
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM banned_words WHERE normalized_word = ? LIMIT 1",
            (normalized,),
        ).fetchone()
    return bool(row)


def find_existing_word(db_path, raw_word):
    """Handle find existing word."""
    candidate = _normalize_text(raw_word)
    if not candidate:
        return None

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian,
            ukrainian, english, topic, preposition_type, case_type
            FROM words
            WHERE lower(trim(german)) = ?
               OR lower(trim(infinitive)) = ?
               OR lower(trim(plural)) = ?
               OR lower(trim(prateritum)) = ?
               OR lower(trim(partizip2)) = ?
            LIMIT 1
            """,
            (candidate, candidate, candidate, candidate, candidate),
        ).fetchone()
        if row:
            return _row_to_entry(row)

        # Fallback for composite verb forms like "hat gesehen" and multi-value fields.
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian,
            ukrainian, english, topic
            FROM words
            """
        ).fetchall()

    for item in rows:
        variants = {
            _normalize_text(item["german"]),
            _normalize_text(item["infinitive"]),
        }
        variants.update(_form_variants(item["plural"]))
        variants.update(_form_variants(item["prateritum"]))
        variants.update(_form_variants(item["partizip2"]))
        if candidate in variants:
            return _row_to_entry(item)
    return None


def _dedupe_entry(conn, entry):
    """Handle dedupe entry."""
    key_type, key_value = _entry_key(entry)
    if key_type == "verb":
        row = conn.execute(
            "SELECT id FROM words WHERE type = 'verb' AND lower(trim(infinitive)) = ? LIMIT 1",
            (key_value,),
        ).fetchone()
    elif key_type == "praeposition":
        # spec #1: the same preposition spelling can be several distinct words, one
        # per (preposition_type, case_type) meaning — each with its own translation
        # and FSRS progress. So a duplicate is only an EXACT triple match; a new
        # (type, case) pair is intentionally allowed through.
        row = conn.execute(
            "SELECT id FROM words WHERE type = 'praeposition' "
            "AND lower(trim(german)) = ? "
            "AND lower(trim(coalesce(preposition_type, ''))) = ? "
            "AND lower(trim(coalesce(case_type, ''))) = ? LIMIT 1",
            (
                key_value,
                _normalize_text(entry.get("preposition_type", "")),
                _normalize_text(entry.get("case_type", "")),
            ),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM words WHERE lower(trim(german)) = ? LIMIT 1",
            (key_value,),
        ).fetchone()
    if row:
        raise ValueError("Already in database.")


def _normalize_word_entry_for_storage(entry):
    """Normalize word casing before storing."""
    result = dict(entry)
    word_type = _safe_text(result.get("type", "")).strip().lower()
    result["type"] = word_type
    if word_type == "phrase":
        return result

    article = _safe_text(result.get("article", "")).strip().lower()
    if article:
        result["article"] = article

    if word_type == "nomen":
        result["german"] = cap_word(result.get("german", ""))
    else:
        for field in ("german", "infinitive", "prateritum", "partizip2", "komparativ", "superlativ"):
            value = _safe_text(result.get(field, "")).strip().lower()
            if value:
                result[field] = value

    for field in ("russian", "ukrainian", "english"):
        value = _safe_text(result.get(field, "")).strip().lower()
        if value:
            result[field] = value

    return result


def add_word(db_path, entry):
    """Handle add word."""
    init_db(db_path)
    entry = _normalize_word_entry_for_storage(entry)
    if entry.get("id") in (None, "", "unique"):
        entry["id"] = str(uuid.uuid4())

    with _connect(db_path) as conn:
        _dedupe_entry(conn, entry)
        conn.execute(
            """
            INSERT INTO words (
                id, type, article, german, plural, infinitive, prateritum, partizip2, komparativ, superlativ,
                russian, ukrainian, english, image, example, topic, preposition_type, case_type, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                entry["id"],
                _safe_text(entry.get("type", "")),
                _safe_text(entry.get("article", "")),
                _safe_text(entry.get("german", "")),
                _safe_text(entry.get("plural", "")),
                _safe_text(entry.get("infinitive", "")),
                _safe_text(entry.get("prateritum", "")),
                _safe_text(entry.get("partizip2", "")),
                _safe_text(entry.get("komparativ", "")),
                _safe_text(entry.get("superlativ", "")),
                _safe_text(entry.get("russian", "")),
                _safe_text(entry.get("ukrainian", "")),
                _safe_text(entry.get("english", "")),
                _safe_text(entry.get("image", "")),
                _safe_text(entry.get("example", "")),
                _safe_text(entry.get("topic", "1")) or "1",
                _safe_text(entry.get("preposition_type", "")),
                _safe_text(entry.get("case_type", "")),
            ),
        )
    return entry


def _progress_for_row(row):
    """Handle progress for row."""
    if not row["p_due"]:
        return default_srs()
    return {
        "interval": int(row["p_interval"]),
        "repetitions": int(row["p_repetitions"]),
        "ease": float(row["p_ease"]),
        "due": row["p_due"],
    }


def select_due(
    db_path,
    user_id,
    mode,
    word_type=None,
    topics=None,
    today=None,
    exclude_correct_today=False,
):
    """Handle select due."""
    if today is None:
        today = date.today()
    due_today = today.isoformat()
    init_db(db_path)

    where_clauses = [
        "COALESCE(p.due, ?) <= ?",
        """NOT EXISTS (
            SELECT 1
            FROM learning_items li
            WHERE li.user_id = ?
              AND li.word_id = w.id
              AND li.status = 'active'
        )""",
        """(
            EXISTS (
                SELECT 1
                FROM learning_items l3
                WHERE l3.user_id = ?
                  AND l3.word_id = w.id
                  AND l3.status IN ('completed', 'skipped')
            )
            OR EXISTS (
                SELECT 1
                FROM quiz_events e
                WHERE e.user_id = ?
                  AND e.word_id = w.id
                  AND e.mode != 'learning'
            )
        )""",
    ]
    where_params = [due_today, due_today]
    where_params.append(int(user_id))
    where_params.append(int(user_id))
    where_params.append(int(user_id))
    if word_type:
        where_clauses.insert(0, "w.type = ?")
        where_params.insert(0, word_type)
    if topics:
        clean_topics = [_safe_text(x) for x in topics if _safe_text(x)]
        if clean_topics:
            placeholders = ",".join("?" for _ in clean_topics)
            where_clauses.append(f"w.topic IN ({placeholders})")
            where_params.extend(clean_topics)
    if exclude_correct_today:
        where_clauses.append(
            """NOT EXISTS (
                SELECT 1
                FROM quiz_events qe2
                WHERE qe2.user_id = ?
                  AND qe2.word_id = w.id
                  AND qe2.mode = ?
                  AND qe2.is_correct = 1
                  AND date(qe2.created_at) = date(?)
            )"""
        )
        where_params.extend([int(user_id), mode, due_today])

    where_sql = " AND ".join(where_clauses)

    query = f"""
        SELECT
            w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ, w.russian,
            w.ukrainian, w.english, w.image, w.example, w.topic, w.preposition_type, w.case_type, ldone.done_at AS learning_done_at,
            CASE WHEN EXISTS (
                SELECT 1
                FROM quiz_events qe
                WHERE qe.user_id = ?
                  AND qe.word_id = w.id
                  AND qe.mode != 'learning'
            ) THEN 1 ELSE 0 END AS has_non_learning_history,
            p.interval AS p_interval, p.repetitions AS p_repetitions, p.ease AS p_ease, p.due AS p_due
        FROM words w
        LEFT JOIN progress p
            ON p.word_id = w.id AND p.user_id = ? AND p.mode = ?
        LEFT JOIN (
            SELECT word_id, MAX(updated_at) AS done_at
            FROM learning_items
            WHERE user_id = ? AND status IN ('completed', 'skipped')
            GROUP BY word_id
        ) ldone
            ON ldone.word_id = w.id
        WHERE {where_sql}
        ORDER BY
            CASE WHEN ldone.done_at IS NOT NULL THEN 0 ELSE 1 END,
            ldone.done_at DESC,
            p.due ASC,
            w.created_at ASC
    """

    # Param order must match placeholder order in the query above:
    # has_non_learning_history (user_id), progress join (user_id, mode), ldone (user_id).
    params = [int(user_id), int(user_id), mode, int(user_id)] + where_params
    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    result = []
    for row in rows:
        entry = _row_to_entry(row)
        entry["srs"] = _progress_for_row(row)
        result.append(entry)
    return result


def update_fsrs(srs, quality, today=None, interval_divisor=1.0):
    """Handle FSRS-like update in existing FSRS storage format."""
    if today is None:
        today = date.today()

    quality = max(0, min(5, int(quality)))
    try:
        interval_divisor = float(interval_divisor)
    except (TypeError, ValueError):
        interval_divisor = 1.0
    if interval_divisor <= 0:
        interval_divisor = 1.0
    interval = max(1, int(srs.get("interval", 1)))
    repetitions = max(0, int(srs.get("repetitions", 0)))
    ease = float(srs.get("ease", DEFAULT_EASE))
    ease = max(MIN_EASE, min(MAX_EASE, ease))

    if quality < 3:
        # Softer than classic FSRS reset: step back one stage and shorten interval.
        repetitions = max(0, repetitions - 1)
        ease = max(MIN_EASE, ease - 0.25)
        interval = max(1, int(round(interval * 0.4)))
    else:
        repetitions += 1
        ease = min(MAX_EASE, ease + 0.05 * (quality - 3))
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 3
        else:
            growth = 1.7 + (ease - DEFAULT_EASE) * 0.35 + (quality - 3) * 0.08
            growth = max(1.3, min(2.3, growth))
            effective_growth = 1.0 + ((growth - 1.0) / interval_divisor)
            interval = max(1, int(round(interval * effective_growth)))
    due = today + timedelta(days=interval)
    return {
        "interval": interval,
        "repetitions": repetitions,
        "ease": round(ease, 2),
        "due": due.isoformat(),
    }


def update_progress(db_path, user_id, word_id, mode, quality, interval_divisor=1.0):
    """Handle update progress."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT interval, repetitions, ease, due
            FROM progress
            WHERE user_id = ? AND word_id = ? AND mode = ?
            """,
            (int(user_id), word_id, mode),
        ).fetchone()

        srs = (
            {
                "interval": int(row["interval"]),
                "repetitions": int(row["repetitions"]),
                "ease": float(row["ease"]),
                "due": row["due"],
            }
            if row
            else default_srs()
        )
        updated = update_fsrs(srs, quality, interval_divisor=interval_divisor)

        conn.execute(
            """
            INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, word_id, mode) DO UPDATE SET
                interval = excluded.interval,
                repetitions = excluded.repetitions,
                ease = excluded.ease,
                due = excluded.due,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(user_id),
                word_id,
                mode,
                int(updated["interval"]),
                int(updated["repetitions"]),
                float(updated["ease"]),
                updated["due"],
            ),
        )


def _utc_now_iso():
    """Handle utc now iso."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_learning_intervals(raw):
    """Handle parse learning intervals."""
    token = str(raw or "").strip()
    if not token:
        return []
    values = []
    for chunk in token.replace(";", ",").split(","):
        part = chunk.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value < 1:
            continue
        values.append(value)
    return values


def _active_learning_batch_row(conn, user_id):
    """Handle active learning batch row."""
    return conn.execute(
        """
        SELECT id, target_qty, status, ready_since, last_notified_at
        FROM learning_batches
        WHERE user_id = ? AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()


def learning_get_or_create_batch(db_path, user_id, daily_qty):
    """Handle learning get or create batch."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = _active_learning_batch_row(conn, user_id)
        if row:
            if int(row["target_qty"] or 0) != int(daily_qty):
                conn.execute(
                    """
                    UPDATE learning_batches
                    SET target_qty = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (int(daily_qty), row["id"]),
                )
            return row["id"]
        batch_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO learning_batches (id, user_id, target_qty, status)
            VALUES (?, ?, ?, 'active')
            """,
            (batch_id, int(user_id), int(daily_qty)),
        )
    return batch_id


def learning_active_count(db_path, user_id, batch_id=None):
    """Handle learning active count."""
    init_db(db_path)
    query = "SELECT COUNT(*) AS cnt FROM learning_items WHERE user_id = ? AND status = 'active'"
    params = [int(user_id)]
    if batch_id:
        query += " AND batch_id = ?"
        params.append(_safe_text(batch_id))
    with _connect(db_path) as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return int(row["cnt"] or 0) if row else 0


def learning_pick_new_words(db_path, user_id, limit, topics=None):
    """Handle learning pick new words."""
    init_db(db_path)
    if int(limit) <= 0:
        return []
    where_topics = ""
    params = [int(user_id), int(user_id)]
    if topics:
        clean_topics = [_safe_text(x) for x in topics if _safe_text(x)]
        if clean_topics:
            placeholders = ",".join("?" for _ in clean_topics)
            where_topics = f" AND w.topic IN ({placeholders})"
            params.extend(clean_topics)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ,
                   w.russian, w.ukrainian, w.english, w.topic
            FROM words w
            WHERE NOT EXISTS (
                SELECT 1 FROM quiz_events e
                WHERE e.user_id = ? AND e.word_id = w.id
            )
              AND NOT EXISTS (
                SELECT 1 FROM learning_items li
                WHERE li.user_id = ? AND li.word_id = w.id
              )
            {where_topics}
            ORDER BY RANDOM()
            LIMIT ?
            """.format(where_topics=where_topics),
            (*params, int(limit)),
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


def learning_add_words(db_path, user_id, words, batch_id, first_delay_seconds):
    """Handle learning add words."""
    init_db(db_path)
    due_iso = _utc_now_iso()
    inserted = 0
    with _connect(db_path) as conn:
        for entry in words:
            word_id = _safe_text(entry.get("id"))
            if not word_id:
                continue
            cur = conn.execute(
                """
                INSERT INTO learning_items (user_id, word_id, batch_id, step_index, preview_done, next_due, status)
                VALUES (?, ?, ?, 0, 0, ?, 'active')
                ON CONFLICT(user_id, word_id) DO NOTHING
                """,
                (int(user_id), word_id, _safe_text(batch_id), due_iso),
            )
            inserted += int(cur.rowcount > 0)
        conn.execute(
            """
            UPDATE learning_batches
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (_safe_text(batch_id),),
        )
    return inserted


def learning_add_word_by_id(db_path, user_id, word_id, batch_id, first_delay_seconds):
    """Add/restart a word in learning as a fresh item (preview from step 0).

    Manual "send to learning" must always (re)enqueue the word as new — even if it
    previously completed/skipped learning or is already active — so it goes through
    the preview/learning cycle again. Returns 1 (the upsert always touches a row).
    """
    init_db(db_path)
    due_iso = _utc_now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO learning_items (user_id, word_id, batch_id, step_index, preview_done, next_due, status)
            VALUES (?, ?, ?, 0, 0, ?, 'active')
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                batch_id = excluded.batch_id,
                step_index = 0,
                preview_done = 0,
                next_due = excluded.next_due,
                status = 'active',
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(user_id), _safe_text(word_id), _safe_text(batch_id), due_iso),
        )
        conn.execute(
            "UPDATE learning_batches SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (_safe_text(batch_id),),
        )
    return 1


def learning_preview_entry(db_path, user_id):
    """Handle learning preview entry."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ,
                w.russian, w.ukrainian, w.english, w.image, w.example, w.topic,
                li.step_index AS learning_step,
                li.next_due AS learning_due,
                li.batch_id AS learning_batch_id
            FROM learning_items li
            JOIN words w ON w.id = li.word_id
            WHERE li.user_id = ?
              AND li.status = 'active'
              AND li.preview_done = 0
            ORDER BY li.created_at ASC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
    return _row_to_entry(row) if row else None


def learning_mark_preview_next(db_path, user_id, word_id, first_interval_seconds):
    """Handle learning mark preview next."""
    init_db(db_path)
    now = datetime.now(timezone.utc)
    first_due_iso = (now + timedelta(seconds=max(0, int(first_interval_seconds)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT batch_id
            FROM learning_items
            WHERE user_id = ? AND word_id = ? AND status = 'active'
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
        if not row:
            return False
        batch_id = row["batch_id"]
        conn.execute(
            """
            UPDATE learning_items
            SET preview_done = 1, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND word_id = ? AND status = 'active'
            """,
            (int(user_id), _safe_text(word_id)),
        )
        pending = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM learning_items
                WHERE user_id = ? AND batch_id = ? AND status = 'active' AND preview_done = 0
                """,
                (int(user_id), batch_id),
            ).fetchone()["cnt"]
            or 0
        )
        if pending == 0:
            conn.execute(
                """
                UPDATE learning_items
                SET next_due = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND batch_id = ? AND status = 'active' AND preview_done = 1
                """,
                (first_due_iso, int(user_id), batch_id),
            )
        conn.execute(
            "UPDATE learning_batches SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (batch_id,),
        )
    return True


def learning_due_entries(db_path, user_id, exclude_word_id=None):
    """Return due learning entries."""
    init_db(db_path)
    now_iso = _utc_now_iso()
    base_query = """
            SELECT
                w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ,
                w.russian, w.ukrainian, w.english, w.image, w.example, w.topic,
                li.step_index AS learning_step,
                li.next_due AS learning_due,
                li.batch_id AS learning_batch_id
            FROM learning_items li
            JOIN words w ON w.id = li.word_id
            WHERE li.user_id = ?
              AND li.status = 'active'
              AND li.preview_done = 1
              AND li.next_due <= ?
    """
    order_clause = """
            ORDER BY
              CASE WHEN li.step_index = 0 THEN 0 ELSE 1 END ASC,
              CASE WHEN li.step_index = 0 THEN li.created_at ELSE NULL END ASC,
              li.next_due ASC,
              li.updated_at ASC
    """
    with _connect(db_path) as conn:
        if exclude_word_id:
            rows = conn.execute(
                f"""
                {base_query}
                  AND w.id != ?
                {order_clause}
                """,
                (int(user_id), now_iso, _safe_text(exclude_word_id)),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                {base_query}
                {order_clause}
                """,
                (int(user_id), now_iso),
            ).fetchall()
    return [_row_to_entry(row) for row in rows]


def learning_due_entry(db_path, user_id, exclude_word_id=None):
    """Handle learning due entry."""
    entries = learning_due_entries(db_path, user_id, exclude_word_id=exclude_word_id)
    if not entries and exclude_word_id:
        entries = learning_due_entries(db_path, user_id, exclude_word_id=None)
    return entries[0] if entries else None


def learning_update_result(db_path, user_id, word_id, ok, intervals_seconds):
    """Handle learning update result."""
    intervals = [int(v) for v in intervals_seconds if int(v) >= 0]
    if not intervals:
        return {"completed": True, "step_index": 0, "next_due": None}
    init_db(db_path)
    now = datetime.now(timezone.utc)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT step_index, batch_id
            FROM learning_items
            WHERE user_id = ? AND word_id = ? AND status = 'active'
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
        if not row:
            return {"completed": False, "step_index": 0, "next_due": None}
        step_index = int(row["step_index"] or 0)
        batch_id = row["batch_id"]

        if ok:
            next_step = step_index + 1
            if next_step >= len(intervals):
                conn.execute(
                    """
                    UPDATE learning_items
                    SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND word_id = ?
                    """,
                    (int(user_id), _safe_text(word_id)),
                )
                conn.execute(
                    """
                    UPDATE learning_batches
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (_safe_text(batch_id),),
                )
                return {"completed": True, "step_index": next_step, "next_due": None}
            due_iso = (now + timedelta(seconds=intervals[next_step])).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                UPDATE learning_items
                SET step_index = ?, next_due = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND word_id = ?
                """,
                (int(next_step), due_iso, int(user_id), _safe_text(word_id)),
            )
            conn.execute(
                """
                UPDATE learning_batches
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (_safe_text(batch_id),),
            )
            return {"completed": False, "step_index": next_step, "next_due": due_iso}

        due_iso = (now + timedelta(seconds=intervals[0])).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            UPDATE learning_items
            SET step_index = 0, next_due = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND word_id = ?
            """,
            (due_iso, int(user_id), _safe_text(word_id)),
        )
        conn.execute(
            """
            UPDATE learning_batches
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (_safe_text(batch_id),),
        )
        return {"completed": False, "step_index": 0, "next_due": due_iso}


def learning_apply_input_err(db_path, user_id, word_id, prev_step_index, intervals_seconds):
    """Handle apply input err for learning without reset."""
    intervals = [int(v) for v in intervals_seconds if int(v) >= 0]
    if not intervals:
        return {"completed": True, "step_index": 0, "next_due": None}
    init_db(db_path)
    now = datetime.now(timezone.utc)
    try:
        prev_step = int(prev_step_index)
    except (TypeError, ValueError):
        prev_step = 0
    prev_step = max(0, min(prev_step, len(intervals) - 1))
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT batch_id
            FROM learning_items
            WHERE user_id = ? AND word_id = ? AND status = 'active'
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
        if not row:
            return {"completed": False, "step_index": prev_step, "next_due": None}
        batch_id = row["batch_id"]
        next_step = prev_step + 1
        if next_step >= len(intervals):
            conn.execute(
                """
                UPDATE learning_items
                SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND word_id = ?
                """,
                (int(user_id), _safe_text(word_id)),
            )
            conn.execute(
                "UPDATE learning_batches SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (_safe_text(batch_id),),
            )
            return {"completed": True, "step_index": next_step, "next_due": None}
        due_iso = (now + timedelta(seconds=intervals[next_step])).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            UPDATE learning_items
            SET step_index = ?, next_due = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND word_id = ?
            """,
            (int(next_step), due_iso, int(user_id), _safe_text(word_id)),
        )
        conn.execute(
            "UPDATE learning_batches SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (_safe_text(batch_id),),
        )
        return {"completed": False, "step_index": next_step, "next_due": due_iso}


def learning_skip_word(db_path, user_id, word_id):
    """Handle learning skip word."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT batch_id
            FROM learning_items
            WHERE user_id = ? AND word_id = ? AND status = 'active'
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
        conn.execute(
            """
            UPDATE learning_items
            SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND word_id = ? AND status = 'active'
            """,
            (int(user_id), _safe_text(word_id)),
        )
        if row:
            conn.execute(
                "UPDATE learning_batches SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["batch_id"],),
            )
    return True


def learning_reset_session(db_path, user_id):
    """Handle learning reset session."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT word_id
            FROM learning_items
            WHERE user_id = ? AND status = 'active'
            """,
            (int(user_id),),
        ).fetchall()
        reset_word_ids = [str(row["word_id"]) for row in rows if row and row["word_id"]]

        conn.execute(
            "DELETE FROM learning_items WHERE user_id = ? AND status = 'active'",
            (int(user_id),),
        )

        if reset_word_ids:
            placeholders = ",".join("?" for _ in reset_word_ids)
            conn.execute(
                f"""
                DELETE FROM quiz_events
                WHERE user_id = ? AND mode = 'learning' AND word_id IN ({placeholders})
                """,
                (int(user_id), *reset_word_ids),
            )

        conn.execute(
            """
            DELETE FROM learning_batches
            WHERE user_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM learning_items li
                WHERE li.user_id = learning_batches.user_id
                  AND li.batch_id = learning_batches.id
              )
            """,
            (int(user_id),),
        )
    return True


def learning_status_snapshot(db_path, user_id, quorum_pct=70):
    """Handle learning status snapshot."""
    init_db(db_path)
    now_iso = _utc_now_iso()
    with _connect(db_path) as conn:
        batch = _active_learning_batch_row(conn, user_id)
        if not batch:
            return {
                "batch_id": "",
                "target_qty": 0,
                "active_count": 0,
                "preview_pending": 0,
                "active_due_pool": 0,
                "due_count": 0,
                "ready_needed": 0,
                "ready": False,
                "next_due": "",
                "completed_count": 0,
                "ready_since": "",
                "last_notified_at": "",
            }
        batch_id = batch["id"]
        active_count = int(
            conn.execute(
                "SELECT COUNT(*) AS cnt FROM learning_items WHERE user_id = ? AND batch_id = ? AND status = 'active'",
                (int(user_id), batch_id),
            ).fetchone()["cnt"]
            or 0
        )
        preview_pending = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM learning_items
                WHERE user_id = ? AND batch_id = ? AND status = 'active' AND preview_done = 0
                """,
                (int(user_id), batch_id),
            ).fetchone()["cnt"]
            or 0
        )
        active_due_pool = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM learning_items
                WHERE user_id = ? AND batch_id = ? AND status = 'active' AND preview_done = 1
                """,
                (int(user_id), batch_id),
            ).fetchone()["cnt"]
            or 0
        )
        due_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM learning_items
                WHERE user_id = ? AND batch_id = ? AND status = 'active' AND preview_done = 1 AND next_due <= ?
                """,
                (int(user_id), batch_id, now_iso),
            ).fetchone()["cnt"]
            or 0
        )
        next_due_row = conn.execute(
            """
            SELECT next_due
                FROM learning_items
                WHERE user_id = ? AND batch_id = ? AND status = 'active' AND preview_done = 1
                ORDER BY next_due ASC
                LIMIT 1
                """,
                (int(user_id), batch_id),
            ).fetchone()
        completed_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM learning_items
                WHERE user_id = ? AND batch_id = ? AND status IN ('completed', 'skipped')
                """,
                (int(user_id), batch_id),
            ).fetchone()["cnt"]
            or 0
        )
    needed = int(math.ceil(active_due_pool * (max(1, min(100, int(quorum_pct))) / 100.0))) if active_due_pool else 0
    return {
        "batch_id": batch_id,
        "target_qty": int(batch["target_qty"] or 0),
        "active_count": active_count,
        "preview_pending": preview_pending,
        "active_due_pool": active_due_pool,
        "due_count": due_count,
        "ready_needed": needed,
        "ready": bool(active_due_pool > 0 and preview_pending == 0 and due_count >= needed and needed > 0),
        "next_due": next_due_row["next_due"] if next_due_row else "",
        "completed_count": completed_count,
        "ready_since": batch["ready_since"] or "",
        "last_notified_at": batch["last_notified_at"] or "",
    }


def learning_batch_update_ready_state(db_path, batch_id, ready, now_iso):
    """Handle learning batch update ready state."""
    init_db(db_path)
    with _connect(db_path) as conn:
        if ready:
            row = conn.execute(
                "SELECT ready_since FROM learning_batches WHERE id = ?",
                (_safe_text(batch_id),),
            ).fetchone()
            if row and not row["ready_since"]:
                conn.execute(
                    "UPDATE learning_batches SET ready_since = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (_safe_text(now_iso), _safe_text(batch_id)),
                )
        else:
            conn.execute(
                "UPDATE learning_batches SET ready_since = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (_safe_text(batch_id),),
            )


def learning_batch_mark_notified(db_path, batch_id, now_iso):
    """Handle learning batch mark notified."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE learning_batches
            SET last_notified_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (_safe_text(now_iso), _safe_text(batch_id)),
        )


def learning_active_batches(db_path):
    """Handle learning active batches."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, target_qty, ready_since, last_notified_at
            FROM learning_batches
            WHERE status = 'active'
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def fsrs_due_forecast(db_path, user_id, mode):
    """Handle fsrs due forecast."""
    init_db(db_path)
    today = date.today().isoformat()
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                w.id AS word_id,
                COALESCE(p.due, date('now')) AS due_date,
                p.updated_at AS updated_at
            FROM words w
            LEFT JOIN progress p
              ON p.word_id = w.id
             AND p.user_id = ?
             AND p.mode = ?
            WHERE COALESCE(p.due, date('now')) <= date('now')
              AND NOT EXISTS (
                SELECT 1
                FROM learning_items li
                WHERE li.user_id = ?
                  AND li.word_id = w.id
                  AND li.status = 'active'
              )
              AND (
                EXISTS (
                    SELECT 1
                    FROM learning_items l2
                    WHERE l2.user_id = ?
                      AND l2.word_id = w.id
                      AND l2.status IN ('completed', 'skipped')
                )
                OR EXISTS (
                    SELECT 1
                    FROM quiz_events e
                    WHERE e.user_id = ?
                      AND e.word_id = w.id
                      AND e.mode != 'learning'
                )
              )
            """,
            (int(user_id), _safe_text(mode), int(user_id), int(user_id), int(user_id)),
        ).fetchall()
    per_hour = {h: 0 for h in range(24)}
    total = 0
    for row in rows:
        total += 1
        due_date = str(row["due_date"] or "")
        updated_at = row["updated_at"]
        if due_date < today:
            hour = 0
        elif updated_at and len(updated_at) >= 13:
            try:
                hour = int(updated_at[11:13])
            except ValueError:
                hour = 0
        else:
            hour = 0
        per_hour[hour] = per_hour.get(hour, 0) + 1
    peak_hour = None
    peak_count = 0
    for hour, count in per_hour.items():
        if count > peak_count:
            peak_count = count
            peak_hour = hour
    return {
        "total": total,
        "per_hour": per_hour,
        "peak_hour": peak_hour,
        "peak_count": peak_count,
    }


def set_progress(db_path, user_id, word_id, mode, srs):
    """Handle set progress."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, word_id, mode) DO UPDATE SET
                interval = excluded.interval,
                repetitions = excluded.repetitions,
                ease = excluded.ease,
                due = excluded.due,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(user_id),
                _safe_text(word_id),
                _safe_text(mode),
                int(srs.get("interval", 1)),
                int(srs.get("repetitions", 0)),
                float(srs.get("ease", DEFAULT_EASE)),
                _safe_text(srs.get("due", date.today().isoformat())),
            ),
        )


def log_quiz_event(db_path, user_id, word_id, mode, is_correct, review_stage_snapshot=None):
    """Handle log quiz event."""
    init_db(db_path)
    with _connect(db_path) as conn:
        prev = conn.execute(
            """
            SELECT is_correct
            FROM quiz_events
            WHERE user_id = ? AND word_id = ? AND mode = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id), _safe_text(word_id), _safe_text(mode)),
        ).fetchone()
        prev_is_correct = None if not prev else int(prev["is_correct"])
        conn.execute(
            """
            INSERT INTO quiz_events (user_id, word_id, mode, is_correct, prev_is_correct, review_stage_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                int(user_id),
                _safe_text(word_id),
                _safe_text(mode),
                1 if bool(is_correct) else 0,
                prev_is_correct,
                None if review_stage_snapshot is None else int(review_stage_snapshot),
            ),
        )


def mark_last_quiz_event_correct(db_path, user_id, word_id, mode):
    """Flip the most recent quiz_event for (user, word, mode) to correct in place.

    Used by input_err: the last answer was a typo, not a real miss. Updating the
    existing event (instead of appending a new correct one) means the daily/weekly
    first-touch retention counts that word as correct — a later appended event
    would be ignored because the report keys off the first answer of the day.
    Returns True if a row was updated."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE quiz_events
            SET is_correct = 1
            WHERE id = (
                SELECT id FROM quiz_events
                WHERE user_id = ? AND word_id = ? AND mode = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (int(user_id), _safe_text(word_id), _safe_text(mode)),
        )
        return cur.rowcount > 0


def list_words_for_translations_backfill(db_path, limit=0):
    """Handle list words for translations backfill."""
    init_db(db_path)
    query = """
        SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian, ukrainian, english, topic
        FROM words
        WHERE trim(coalesce(ukrainian, '')) = '' OR trim(coalesce(english, '')) = ''
        ORDER BY created_at ASC
    """
    params = ()
    if int(limit) > 0:
        query += " LIMIT ?"
        params = (int(limit),)

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_entry(row) for row in rows]


def update_word_translations(db_path, word_id, ukrainian=None, english=None):
    """Handle update word translations."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE words
            SET ukrainian = COALESCE(?, ukrainian),
                english = COALESCE(?, english),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                None if ukrainian is None else _safe_text(ukrainian),
                None if english is None else _safe_text(english),
                _safe_text(word_id),
            ),
        )


def update_word_fields(db_path, word_id, fields):
    """Handle update word fields."""
    allowed = {
        "type",
        "article",
        "german",
        "plural",
        "infinitive",
        "prateritum",
        "partizip2",
        "komparativ",
        "superlativ",
        "russian",
        "ukrainian",
        "english",
        "image",
        "example",
        "topic",
        "preposition_type",
        "case_type",
    }
    init_db(db_path)
    with _connect(db_path) as conn:
        existing_cols = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute("PRAGMA table_info(words)").fetchall()
        }
    values = {}
    for key, value in dict(fields or {}).items():
        if key not in allowed:
            continue
        if key not in existing_cols:
            continue
        # spec #1: the two preposition columns are a controlled vocabulary. Guard the
        # storage layer too (defense in depth behind the clarify/AI validation): only
        # a known value or '' (clears the field) is ever written — never free text.
        if key in ("preposition_type", "case_type"):
            normalized = _safe_text(value).strip().lower()
            allowed_vocab = PREPOSITION_TYPES if key == "preposition_type" else CASE_TYPES
            if normalized and normalized not in allowed_vocab:
                continue
            values[key] = normalized
            continue
        values[key] = _safe_text(value)
    if not values:
        return

    parts = [f"{key} = ?" for key in values]
    params = list(values.values()) + [_safe_text(word_id)]
    query = f"UPDATE words SET {', '.join(parts)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"

    with _connect(db_path) as conn:
        conn.execute(query, tuple(params))


def delete_word(db_path, word_id):
    """Handle delete word."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM words WHERE id = ?",
            (_safe_text(word_id),),
        )
        return cur.rowcount > 0


def update_form_stats(db_path, user_id, word_id, checks):
    """
    checks: dict[field_name -> bool], field_name in:
      article, plural, prateritum, partizip2
    """
    safe_checks = {str(k).strip().lower(): bool(v) for k, v in dict(checks or {}).items()}
    allowed = {"article", "plural", "prateritum", "partizip2"}
    fields = [k for k in safe_checks if k in allowed]
    if not fields:
        return

    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO form_stats (user_id, word_id)
            VALUES (?, ?)
            ON CONFLICT(user_id, word_id) DO NOTHING
            """,
            (int(user_id), _safe_text(word_id)),
        )

        today_iso = (date.today() + timedelta(days=WEAK_REVIEW_DAYS_STAGE1)).isoformat()
        for field in fields:
            ok = safe_checks[field]
            pre_row = conn.execute(
                f"""
                SELECT
                    {field}_errors AS errors,
                    {field}_streak_wrong AS streak_wrong,
                    {field}_review_stage AS stage,
                    {field}_active AS active
                FROM form_stats
                WHERE user_id = ? AND word_id = ?
                """,
                (int(user_id), _safe_text(word_id)),
            ).fetchone()
            pre_errors = int(pre_row["errors"] or 0) if pre_row else 0
            pre_streak_wrong = int(pre_row["streak_wrong"] or 0) if pre_row else 0
            pre_stage = int(pre_row["stage"] or 0) if pre_row else 0
            was_active_before = bool(pre_row["active"]) if pre_row else False
            if ok:
                conn.execute(
                    f"""
                    UPDATE form_stats
                    SET {field}_streak_correct = {field}_streak_correct + 1,
                        {field}_streak_wrong = 0
                    WHERE user_id = ? AND word_id = ?
                    """,
                    (int(user_id), _safe_text(word_id)),
                )
                row = conn.execute(
                    f"""
                    SELECT {field}_streak_correct AS s
                    FROM form_stats
                    WHERE user_id = ? AND word_id = ?
                    """,
                    (int(user_id), _safe_text(word_id)),
                ).fetchone()
                streak = int(row["s"] or 0) if row else 0
                # A single correct must NOT clear the episode flag: a freshly-entered
                # word is held in the quiz until it earns WEAK_ACTIVE_CLEAR_STREAK in a
                # row — only then the flag drops and the word promotes to the ladder.
                if streak >= WEAK_ACTIVE_CLEAR_STREAK:
                    conn.execute(
                        f"""
                        UPDATE form_stats
                        SET {field}_errors = 0,
                            {field}_streak_wrong = 0,
                            {field}_streak_correct = 0,
                            {field}_active = 0
                        WHERE user_id = ? AND word_id = ?
                        """,
                        (int(user_id), _safe_text(word_id)),
                    )
                    if pre_stage == 0 and was_active_before:
                        conn.execute(
                            f"""
                            UPDATE form_stats
                            SET {field}_review_stage = 1,
                                {field}_next_review = ?
                            WHERE user_id = ? AND word_id = ?
                            """,
                            (today_iso, int(user_id), _safe_text(word_id)),
                        )
            else:
                conn.execute(
                    f"""
                    UPDATE form_stats
                    SET {field}_errors = {field}_errors + 1,
                        {field}_streak_wrong = {field}_streak_wrong + 1,
                        {field}_streak_correct = 0
                    WHERE user_id = ? AND word_id = ?
                    """,
                    (int(user_id), _safe_text(word_id)),
                )
                # Entry: flip the episode flag once the wrong answer reaches the
                # entry threshold (total errors) or the wrong-streak threshold.
                if (
                    pre_errors + 1 >= WEAK_ACTIVE_MIN_ERRORS
                    or pre_streak_wrong + 1 >= WEAK_ACTIVE_MIN_STREAK_WRONG
                ):
                    conn.execute(
                        f"""
                        UPDATE form_stats
                        SET {field}_active = 1
                        WHERE user_id = ? AND word_id = ?
                        """,
                        (int(user_id), _safe_text(word_id)),
                    )


def update_weak_review_state(db_path, user_id, word_id, field, ok):
    """Handle update weak review state."""
    field = _safe_text(field).strip().lower()
    if field not in {"article", "plural", "prateritum", "partizip2"}:
        return 0
    init_db(db_path)
    today = date.today()
    today_iso = today.isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT
                {field}_review_stage AS stage,
                {field}_active AS active
            FROM form_stats
            WHERE user_id = ? AND word_id = ?
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
        if not row:
            return 0
        stage = int(row["stage"] or 0)
        active = bool(row["active"])
        if active or stage <= 0:
            return stage
        if ok:
            if stage == 1:
                next_review = (today + timedelta(days=WEAK_REVIEW_DAYS_STAGE2)).isoformat()
                conn.execute(
                    f"""
                    UPDATE form_stats
                    SET {field}_review_stage = 2,
                        {field}_next_review = ?
                    WHERE user_id = ? AND word_id = ?
                    """,
                    (next_review, int(user_id), _safe_text(word_id)),
                )
                return 2
            if stage == 2:
                next_review = (today + timedelta(days=WEAK_REVIEW_DAYS_STAGE3)).isoformat()
                conn.execute(
                    f"""
                    UPDATE form_stats
                    SET {field}_review_stage = 3,
                        {field}_next_review = ?
                    WHERE user_id = ? AND word_id = ?
                    """,
                    (next_review, int(user_id), _safe_text(word_id)),
                )
                return 3
            conn.execute(
                f"""
                UPDATE form_stats
                SET {field}_review_stage = 0,
                    {field}_next_review = NULL
                WHERE user_id = ? AND word_id = ?
                """,
                (int(user_id), _safe_text(word_id)),
            )
            return 0
        next_review = (today + timedelta(days=WEAK_REVIEW_DAYS_STAGE1)).isoformat()
        conn.execute(
            f"""
            UPDATE form_stats
            SET {field}_review_stage = 1,
                {field}_next_review = ?
            WHERE user_id = ? AND word_id = ?
            """,
            (next_review, int(user_id), _safe_text(word_id)),
        )
        return 1


def apply_weak_input_err(db_path, user_id, word_id, field):
    """Handle apply weak input error correction."""
    field = _safe_text(field).strip().lower()
    if field not in {"article", "plural", "prateritum", "partizip2"}:
        return False
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT
                {field}_errors AS errors,
                {field}_streak_wrong AS streak_wrong,
                {field}_streak_correct AS streak_correct
            FROM form_stats
            WHERE user_id = ? AND word_id = ?
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
        if not row:
            return False
        errors = int(row["errors"] or 0)
        streak_wrong = int(row["streak_wrong"] or 0)
        streak_correct = int(row["streak_correct"] or 0)
        new_errors = max(0, errors - 1)
        new_streak_wrong = max(0, streak_wrong - 1)
        new_streak_correct = streak_correct + 1
        # input_err explicitly reverses the last (mis-matched) wrong answer, so it
        # may pull a word back OUT of an episode it never should have entered — if
        # the reverted counters no longer meet the entry threshold, drop the flag.
        # (The normal-correct path keeps the flag; only this manual undo clears it.)
        active = new_errors >= WEAK_ACTIVE_MIN_ERRORS or new_streak_wrong >= WEAK_ACTIVE_MIN_STREAK_WRONG
        conn.execute(
            f"""
            UPDATE form_stats
            SET {field}_errors = ?,
                {field}_streak_wrong = ?,
                {field}_streak_correct = ?,
                {field}_active = ?
            WHERE user_id = ? AND word_id = ?
            """,
            (new_errors, new_streak_wrong, new_streak_correct, 1 if active else 0, int(user_id), _safe_text(word_id)),
        )
    return True


def get_form_state(db_path, user_id, word_id, field):
    """Handle get form state."""
    field = _safe_text(field).strip().lower()
    if field not in {"article", "plural", "prateritum", "partizip2"}:
        return {"errors": 0, "streak_wrong": 0, "review_stage": 0, "active": False}
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT
                {field}_errors AS errors,
                {field}_streak_wrong AS streak_wrong,
                {field}_review_stage AS stage,
                {field}_active AS active
            FROM form_stats
            WHERE user_id = ? AND word_id = ?
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
    if not row:
        return {"errors": 0, "streak_wrong": 0, "review_stage": 0, "active": False}
    errors = int(row["errors"] or 0)
    streak_wrong = int(row["streak_wrong"] or 0)
    stage = int(row["stage"] or 0)
    active = bool(row["active"])
    return {"errors": errors, "streak_wrong": streak_wrong, "review_stage": stage, "active": active}


def get_form_review_stage(db_path, user_id, word_id, field):
    """Handle get form review stage."""
    field = _safe_text(field).strip().lower()
    if field not in {"article", "plural", "prateritum", "partizip2"}:
        return 0
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT {field}_review_stage AS stage
            FROM form_stats
            WHERE user_id = ? AND word_id = ?
            """,
            (int(user_id), _safe_text(word_id)),
        ).fetchone()
    return int(row["stage"] or 0) if row else 0


def list_weak_words(db_path, user_id, weak_field, word_type=None, topics=None, limit=200):
    """Handle list weak words."""
    weak_field = _safe_text(weak_field).strip().lower()
    if weak_field not in {"article", "plural", "prateritum", "partizip2"}:
        return []
    init_db(db_path)
    today = date.today().isoformat()
    active_cond = f"(fs.{weak_field}_active = 1)"
    review_cond = f"(fs.{weak_field}_review_stage > 0 AND date(fs.{weak_field}_next_review) <= date(?))"
    where = [
        "fs.user_id = ?",
        f"({active_cond} OR {review_cond})",
    ]
    params = [int(user_id), today]
    if word_type:
        where.append("w.type = ?")
        params.append(_safe_text(word_type))
    if topics:
        clean_topics = [_safe_text(x) for x in topics if _safe_text(x)]
        if clean_topics:
            placeholders = ",".join("?" for _ in clean_topics)
            where.append(f"w.topic IN ({placeholders})")
            params.extend(clean_topics)
    query = f"""
        SELECT
            w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ, w.russian,
            w.ukrainian, w.english, w.topic,
            fs.{weak_field}_review_stage AS review_stage,
            fs.{weak_field}_next_review AS next_review,
            p.interval AS p_interval, p.repetitions AS p_repetitions, p.ease AS p_ease, p.due AS p_due,
            CASE WHEN {active_cond} THEN 1 ELSE 0 END AS is_active
        FROM form_stats fs
        JOIN words w ON w.id = fs.word_id
        LEFT JOIN progress p ON p.word_id = w.id AND p.user_id = ? AND p.mode = ?
        WHERE {' AND '.join(where)}
        ORDER BY is_active DESC, COALESCE(fs.{weak_field}_next_review, '') ASC, w.created_at ASC
        LIMIT ?
    """
    mode = f"weak_{weak_field}"
    query_params = [int(user_id), mode] + params + [int(limit)]
    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(query_params)).fetchall()
    result = []
    for row in rows:
        entry = _row_to_entry(row)
        entry["srs"] = _progress_for_row(row)
        result.append(entry)
    return result


def _get_or_create_group(conn, word_id):
    """Handle get or create group."""
    row = conn.execute(
        "SELECT group_id FROM word_synonyms WHERE word_id = ?",
        (_safe_text(word_id),),
    ).fetchone()
    if row:
        return int(row["group_id"])
    cur = conn.execute("INSERT INTO synonym_groups DEFAULT VALUES")
    group_id = int(cur.lastrowid)
    conn.execute(
        "INSERT OR REPLACE INTO word_synonyms (word_id, group_id) VALUES (?, ?)",
        (_safe_text(word_id), group_id),
    )
    return group_id


def _cleanup_small_group(conn, group_id):
    """Handle cleanup small group."""
    rows = conn.execute(
        "SELECT word_id FROM word_synonyms WHERE group_id = ?",
        (int(group_id),),
    ).fetchall()
    if len(rows) >= 2:
        return
    conn.execute("DELETE FROM word_synonyms WHERE group_id = ?", (int(group_id),))
    conn.execute("DELETE FROM synonym_groups WHERE id = ?", (int(group_id),))


def add_synonym_link(db_path, anchor_word_id, target_word_id):
    """Handle add synonym link."""
    a = _safe_text(anchor_word_id)
    b = _safe_text(target_word_id)
    if not a or not b or a == b:
        return False, None
    init_db(db_path)
    with _connect(db_path) as conn:
        g1 = _get_or_create_group(conn, a)
        g2 = _get_or_create_group(conn, b)
        if g1 == g2:
            return False, g1
        conn.execute(
            "UPDATE word_synonyms SET group_id = ? WHERE group_id = ?",
            (int(g1), int(g2)),
        )
        conn.execute("DELETE FROM synonym_groups WHERE id = ?", (int(g2),))
    return True, g1


def get_synonym_group(db_path, word_id):
    """Handle get synonym group."""
    wid = _safe_text(word_id)
    if not wid:
        return []
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT group_id FROM word_synonyms WHERE word_id = ?",
            (wid,),
        ).fetchone()
        if not row:
            return []
        group_id = int(row["group_id"])
        rows = conn.execute(
            """
            SELECT w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ, w.russian
            , w.ukrainian, w.english, w.topic
            FROM word_synonyms s
            JOIN words w ON w.id = s.word_id
            WHERE s.group_id = ?
            ORDER BY w.created_at ASC
            """,
            (group_id,),
        ).fetchall()
    return [_row_to_entry(x) for x in rows]


def get_synonym_group_id(db_path, word_id):
    """Handle get synonym group id."""
    wid = _safe_text(word_id)
    if not wid:
        return None
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT group_id FROM word_synonyms WHERE word_id = ?",
            (wid,),
        ).fetchone()
    if not row:
        return None
    return int(row["group_id"])


def remove_synonym_link(db_path, word_id):
    """Handle remove synonym link."""
    wid = _safe_text(word_id)
    if not wid:
        return False
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT group_id FROM word_synonyms WHERE word_id = ?",
            (wid,),
        ).fetchone()
        if not row:
            return False
        group_id = int(row["group_id"])
        conn.execute("DELETE FROM word_synonyms WHERE word_id = ?", (wid,))
        _cleanup_small_group(conn, group_id)
    return True


def clear_synonym_group(db_path, word_id):
    """Handle clear synonym group."""
    wid = _safe_text(word_id)
    if not wid:
        return False
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT group_id FROM word_synonyms WHERE word_id = ?",
            (wid,),
        ).fetchone()
        if not row:
            return False
        group_id = int(row["group_id"])
        conn.execute("DELETE FROM word_synonyms WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM synonym_groups WHERE id = ?", (group_id,))
    return True


def list_synonym_candidates_by_ru(db_path, anchor_word_id):
    """Handle list synonym candidates by ru."""
    anchor = get_word_by_id(db_path, anchor_word_id)
    if not anchor:
        return []
    anchor_type = _safe_text(anchor.get("type", "")).strip().lower()
    ru_values = {v for v in _form_variants(anchor.get("russian", "")) if v}
    if not ru_values:
        return []
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            WHERE lower(trim(type)) = ?
            """,
            (anchor_type,),
        ).fetchall()
    result = []
    for row in rows:
        entry = _row_to_entry(row)
        if entry["id"] == anchor["id"]:
            continue
        values = {v for v in _form_variants(entry.get("russian", "")) if v}
        if ru_values.intersection(values):
            result.append(entry)
    return result


def get_word_by_id(db_path, word_id):
    """Handle get word by id."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            WHERE id = ?
            LIMIT 1
            """,
            (_safe_text(word_id),),
        ).fetchone()
    if not row:
        return None
    return _row_to_entry(row)


def list_all_words(db_path):
    """Handle list all words."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


def list_words_by_topic(db_path, topic):
    """Handle list words by topic."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            WHERE topic = ?
            ORDER BY created_at ASC
            """,
            (_safe_text(topic),),
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


def list_learning_active_words(db_path, user_id):
    """Handle list active learning words."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.russian
                , w.ukrainian, w.english, w.topic
            FROM learning_items li
            JOIN words w ON w.id = li.word_id
            WHERE li.user_id = ?
              AND li.status = 'active'
            ORDER BY
                CASE WHEN trim(coalesce(w.topic, '')) GLOB '[0-9]*' THEN 0 ELSE 1 END ASC,
                CASE WHEN trim(coalesce(w.topic, '')) GLOB '[0-9]*' THEN CAST(trim(w.topic) AS INTEGER) ELSE NULL END ASC,
                w.topic ASC,
                li.created_at ASC
            """,
            (int(user_id),),
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


def get_word_study_status(db_path, user_id, word_id):
    """Return compact status for DB card: queue_learning / learning / fsrs N."""
    init_db(db_path)
    uid = int(user_id)
    wid = _safe_text(word_id)
    with _connect(db_path) as conn:
        li = conn.execute(
            """
            SELECT status
            FROM learning_items
            WHERE user_id = ? AND word_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (uid, wid),
        ).fetchone()
        if li and _safe_text(li["status"]) == "active":
            return "learning"

        max_rep_row = conn.execute(
            """
            SELECT MAX(repetitions) AS max_rep
            FROM progress
            WHERE user_id = ? AND word_id = ?
            """,
            (uid, wid),
        ).fetchone()
        max_rep = int(max_rep_row["max_rep"] or 0) if max_rep_row else 0
        if max_rep > 0:
            return f"fsrs {max_rep}"

        touched = conn.execute(
            """
            SELECT 1
            FROM quiz_events
            WHERE user_id = ? AND word_id = ?
            LIMIT 1
            """,
            (uid, wid),
        ).fetchone()
        completed = conn.execute(
            """
            SELECT 1
            FROM learning_items
            WHERE user_id = ? AND word_id = ? AND status = 'completed'
            LIMIT 1
            """,
            (uid, wid),
        ).fetchone()
    if not touched and not completed:
        return "queue_learning"
    return "fsrs 0"


def list_learning_queue_words(db_path, user_id, topics=None):
    """Handle new words that have no first touch and are not completed in learning."""
    init_db(db_path)
    where_topics = ""
    params = [int(user_id), int(user_id)]
    if topics:
        clean_topics = [_safe_text(x) for x in topics if _safe_text(x)]
        if clean_topics:
            placeholders = ",".join("?" for _ in clean_topics)
            where_topics = f" AND w.topic IN ({placeholders})"
            params.extend(clean_topics)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.russian
                , w.ukrainian, w.english, w.topic
            FROM words w
            WHERE NOT EXISTS (
                SELECT 1 FROM quiz_events e
                WHERE e.user_id = ? AND e.word_id = w.id
            )
              AND NOT EXISTS (
                SELECT 1 FROM learning_items li
                WHERE li.user_id = ? AND li.word_id = w.id AND li.status = 'completed'
              )
            {where_topics}
            ORDER BY
                CASE WHEN trim(coalesce(w.topic, '')) GLOB '[0-9]*' THEN 0 ELSE 1 END ASC,
                CASE WHEN trim(coalesce(w.topic, '')) GLOB '[0-9]*' THEN CAST(trim(w.topic) AS INTEGER) ELSE NULL END ASC,
                w.topic ASC,
                w.created_at ASC
            """.format(where_topics=where_topics),
            tuple(params),
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


# spec #6: display label -> the form's active-episode flag column in form_stats.
_WEAK_FORM_COLUMNS = (
    ("artikel", "article_active"),
    ("plural", "plural_active"),
    ("präteritum", "prateritum_active"),
    ("partizip2", "partizip2_active"),
)


def list_weak_form_words(db_path, user_id):
    """Words with an ACTIVE weak-form episode for this user (spec #6).

    A word is listed when any weak-form flag (#18A `*_active`) is set — the exact
    same episode mechanics that the weak quizzes use (3 correct in a row clears a
    flag, dropping the word). Each entry carries a `weak_forms` list of the active
    forms. Ordered worst-first (most active forms), then alphabetically. Replaces
    the old wrong-streak list (which was unrelated to the weak-form modes).
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum,
                w.partizip2, w.komparativ, w.superlativ, w.russian, w.ukrainian, w.english, w.topic,
                fs.article_active AS article_active, fs.plural_active AS plural_active,
                fs.prateritum_active AS prateritum_active, fs.partizip2_active AS partizip2_active
            FROM form_stats fs
            JOIN words w ON w.id = fs.word_id
            WHERE fs.user_id = ?
              AND (fs.article_active = 1 OR fs.plural_active = 1
                   OR fs.prateritum_active = 1 OR fs.partizip2_active = 1)
            """,
            (int(user_id),),
        ).fetchall()
    out = []
    for row in rows:
        entry = _row_to_entry(row)
        entry["weak_forms"] = [label for label, col in _WEAK_FORM_COLUMNS if row[col]]
        out.append(entry)
    # worst first (most active forms), then alphabetical by German lemma
    out.sort(key=lambda e: (-len(e["weak_forms"]), (e.get("german") or e.get("infinitive") or "").lower()))
    return out


def find_word_by_value(db_path, token):
    """Handle find word by value."""
    candidate = _normalize_text(token)
    if not candidate:
        return None
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            WHERE id = ?
               OR lower(trim(german)) = ?
               OR lower(trim(infinitive)) = ?
            LIMIT 1
            """,
            (_safe_text(token), candidate, candidate),
        ).fetchone()
    if not row:
        return None
    return _row_to_entry(row)


_ARTICLE_PREFIXES = {
    "der",
    "die",
    "das",
    "den",
    "dem",
    "des",
    "ein",
    "eine",
    "einen",
    "einem",
    "eines",
    "kein",
    "keine",
    "keinen",
    "keinem",
    "keines",
}


def _strip_leading_article(token):
    """Handle strip leading article."""
    normalized = _normalize_text(token)
    parts = normalized.split()
    if parts and parts[0] in _ARTICLE_PREFIXES:
        return " ".join(parts[1:])
    return normalized


def search_word(db_path, token):
    """Handle search word."""
    candidate = _normalize_text(token)
    if not candidate:
        return None
    stripped = _strip_leading_article(candidate)
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            WHERE id = ?
               OR lower(trim(german)) IN (?, ?)
               OR lower(trim(infinitive)) IN (?, ?)
               OR lower(trim(plural)) IN (?, ?)
               OR lower(trim(prateritum)) IN (?, ?)
               OR lower(trim(partizip2)) IN (?, ?)
               OR lower(trim(russian)) IN (?, ?)
               OR lower(trim(ukrainian)) IN (?, ?)
               OR lower(trim(english)) IN (?, ?)
            LIMIT 1
            """,
            (
                _safe_text(token),
                candidate,
                stripped,
                candidate,
                stripped,
                candidate,
                stripped,
                candidate,
                stripped,
                candidate,
                stripped,
                candidate,
                stripped,
                candidate,
                stripped,
                candidate,
                stripped,
            ),
        ).fetchone()
        if row:
            return _row_to_entry(row)

        like = f"%{candidate}%"
        like_stripped = f"%{stripped}%"
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian
            , ukrainian, english, image, example, topic, preposition_type, case_type
            FROM words
            WHERE lower(german) LIKE ? OR lower(german) LIKE ?
               OR lower(infinitive) LIKE ? OR lower(infinitive) LIKE ?
               OR lower(plural) LIKE ? OR lower(plural) LIKE ?
               OR lower(prateritum) LIKE ? OR lower(prateritum) LIKE ?
               OR lower(partizip2) LIKE ? OR lower(partizip2) LIKE ?
               OR lower(russian) LIKE ? OR lower(russian) LIKE ?
               OR lower(ukrainian) LIKE ? OR lower(ukrainian) LIKE ?
               OR lower(english) LIKE ? OR lower(english) LIKE ?
            LIMIT 20
            """,
            (
                like,
                like_stripped,
                like,
                like_stripped,
                like,
                like_stripped,
                like,
                like_stripped,
                like,
                like_stripped,
                like,
                like_stripped,
                like,
                like_stripped,
                like,
                like_stripped,
            ),
        ).fetchall()
    if not rows:
        return None

    def _matches(entry):
        forms = {
            _normalize_text(entry.get("german", "")),
            _normalize_text(entry.get("infinitive", "")),
            _normalize_text(entry.get("plural", "")),
            _normalize_text(entry.get("prateritum", "")),
            _normalize_text(entry.get("partizip2", "")),
        }
        if candidate in forms or stripped in forms:
            return True
        for field in ("russian", "ukrainian", "english"):
            variants = _form_variants(entry.get(field, ""))
            if candidate in variants or stripped in variants:
                return True
        return False

    for row in rows:
        entry = _row_to_entry(row)
        if _matches(entry):
            return entry
    return _row_to_entry(rows[0])


def has_translation_collision(db_path, translation, dict_lang):
    """Handle has translation collision."""
    field = {
        "ru": "russian",
        "uk": "ukrainian",
        "en": "english",
    }.get(dict_lang, "russian")
    candidate = _normalize_text(translation)
    if not candidate:
        return False
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {field} FROM words"
        ).fetchall()
    for row in rows:
        value = row[field] if isinstance(row, sqlite3.Row) else row[0]
        if candidate in _form_variants(value):
            return True
    return False


def add_word_flag(db_path, word_id, user_id, comment):
    """Handle add word flag."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO word_flags (word_id, user_id, comment, status, created_at, reviewed_at)
            VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP, '')
            """,
            (_safe_text(word_id), int(user_id), _safe_text(comment)),
        )


def set_word_flag_status(db_path, flag_id, status):
    """Handle set word flag status."""
    safe = _safe_text(status).strip().lower()
    if safe not in {"pending", "reviewed"}:
        raise ValueError("Invalid flag status.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE word_flags
            SET status = ?, reviewed_at = CASE WHEN ? = 'reviewed' THEN CURRENT_TIMESTAMP ELSE '' END
            WHERE id = ?
            """,
            (safe, safe, int(flag_id)),
        )


def list_pending_word_flags(db_path, limit=0):
    """Handle list pending word flags."""
    init_db(db_path)
    query = """
        SELECT
            f.id AS flag_id,
            f.word_id AS flag_word_id,
            f.user_id AS flag_user_id,
            f.comment AS flag_comment,
            f.created_at AS flag_created_at,
            w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.komparativ, w.superlativ, w.russian
            , w.ukrainian, w.english, w.topic
        FROM word_flags f
        JOIN words w ON w.id = f.word_id
        WHERE f.status = 'pending'
        ORDER BY f.created_at ASC
    """
    params = ()
    if int(limit) > 0:
        query += " LIMIT ?"
        params = (int(limit),)

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        item = _row_to_entry(row)
        item["flag_id"] = int(row["flag_id"])
        item["flag_word_id"] = row["flag_word_id"]
        item["flag_user_id"] = int(row["flag_user_id"])
        item["flag_comment"] = row["flag_comment"] or ""
        item["flag_created_at"] = row["flag_created_at"] or ""
        result.append(item)
    return result


# --- P6: user error/fix reports + general feedback. Users write their own rows;
# the owner reviews all and closes (status -> reviewed) or deletes via buttons. ---

def add_error_report(db_path, user_id, word_id, comment, mode="", state="", last_action=""):
    """Store a user's error/fix report on a word, with a small where-it-happened trace."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO error_reports
                (word_id, user_id, mode, state, last_action, comment, status, created_at, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP, '')
            """,
            (
                _safe_text(word_id), int(user_id), _safe_text(mode),
                _safe_text(state), _safe_text(last_action), _safe_text(comment),
            ),
        )
        return int(cur.lastrowid)


def list_error_reports(db_path, status="pending", limit=0):
    """Reports for owner review (default: pending), newest last; joins word + username."""
    init_db(db_path)
    query = """
        SELECT r.id, r.word_id, r.user_id, r.mode, r.state, r.last_action,
               r.comment, r.status, r.created_at,
               w.german AS german, u.username AS username, u.first_name AS first_name
        FROM error_reports r
        LEFT JOIN words w ON w.id = r.word_id
        LEFT JOIN users u ON u.user_id = r.user_id
        WHERE r.status = ?
        ORDER BY r.created_at ASC
    """
    params = [_safe_text(status)]
    if int(limit) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "id": int(row["id"]),
            "word_id": row["word_id"] or "",
            "user_id": int(row["user_id"]),
            "mode": row["mode"] or "",
            "state": row["state"] or "",
            "last_action": row["last_action"] or "",
            "comment": row["comment"] or "",
            "status": row["status"] or "",
            "created_at": row["created_at"] or "",
            "german": row["german"] or "",
            "username": row["username"] or "",
            "first_name": row["first_name"] or "",
        }
        for row in rows
    ]


def set_error_report_status(db_path, report_id, status):
    """Mark a report reviewed/pending."""
    safe = _safe_text(status).strip().lower()
    if safe not in {"pending", "reviewed"}:
        raise ValueError("Invalid report status.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE error_reports
            SET status = ?, reviewed_at = CASE WHEN ? = 'reviewed' THEN CURRENT_TIMESTAMP ELSE '' END
            WHERE id = ?
            """,
            (safe, safe, int(report_id)),
        )


def delete_error_report(db_path, report_id):
    """Remove a report row entirely."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM error_reports WHERE id = ?", (int(report_id),))


def delete_all_error_reports(db_path, status=None):
    """Delete every report (or only those with `status` when given)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        if status is None:
            conn.execute("DELETE FROM error_reports")
        else:
            conn.execute("DELETE FROM error_reports WHERE status = ?", (_safe_text(status),))


def add_feedback(db_path, user_id, text):
    """Store a user's general feedback about the bot."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback (user_id, text, status, created_at, reviewed_at)
            VALUES (?, ?, 'pending', CURRENT_TIMESTAMP, '')
            """,
            (int(user_id), _safe_text(text)),
        )
        return int(cur.lastrowid)


def list_feedback(db_path, status="pending", limit=0):
    """Feedback for owner review (default: pending), newest last; joins username."""
    init_db(db_path)
    query = """
        SELECT f.id, f.user_id, f.text, f.status, f.created_at,
               u.username AS username, u.first_name AS first_name
        FROM feedback f
        LEFT JOIN users u ON u.user_id = f.user_id
        WHERE f.status = ?
        ORDER BY f.created_at ASC
    """
    params = [_safe_text(status)]
    if int(limit) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "text": row["text"] or "",
            "status": row["status"] or "",
            "created_at": row["created_at"] or "",
            "username": row["username"] or "",
            "first_name": row["first_name"] or "",
        }
        for row in rows
    ]


def set_feedback_status(db_path, feedback_id, status):
    """Mark feedback reviewed/pending."""
    safe = _safe_text(status).strip().lower()
    if safe not in {"pending", "reviewed"}:
        raise ValueError("Invalid feedback status.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE feedback
            SET status = ?, reviewed_at = CASE WHEN ? = 'reviewed' THEN CURRENT_TIMESTAMP ELSE '' END
            WHERE id = ?
            """,
            (safe, safe, int(feedback_id)),
        )


def delete_feedback(db_path, feedback_id):
    """Remove a feedback row entirely."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM feedback WHERE id = ?", (int(feedback_id),))


def delete_all_feedback(db_path):
    """Delete every feedback row."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM feedback")


def create_template_from_word(raw_word):
    """Handle create template from word."""
    raw_word = raw_word.strip()
    if not raw_word:
        return None

    is_nomen = raw_word[:1].isupper() and (len(raw_word) == 1 or raw_word[1:].islower())
    if is_nomen:
        return {
            "id": "unique",
            "type": "nomen",
            "article": "",
            "german": raw_word,
            "plural": "",
            "russian": "",
        }

    return {
        "id": "unique",
        "type": "verb",
        "infinitive": raw_word,
        "prateritum": "",
        "partizip2": "",
        "russian": "",
    }


def list_vf_candidates(db_path, user_id, only_unchecked=True, limit=0):
    """List noun/verb words for VF check with optional unchanged skip."""
    init_db(db_path)
    candidates = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2
            , russian, ukrainian, english, topic
            FROM words
            WHERE type IN ('nomen', 'verb')
            ORDER BY created_at ASC
            """
        ).fetchall()
        for row in rows:
            entry = _row_to_entry(row)
            source_hash = _vf_source_hash(entry)
            last = conn.execute(
                """
                SELECT source_hash
                FROM vf_checks
                WHERE user_id = ? AND word_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(user_id), _safe_text(entry.get("id"))),
            ).fetchone()
            if only_unchecked and last and _safe_text(last["source_hash"]) == source_hash:
                continue
            entry["vf_source_hash"] = source_hash
            candidates.append(entry)
            if limit and len(candidates) >= int(limit):
                break
    return candidates


def reset_vf_checks(db_path):
    """Clear all stored verbformen check results (spec #2 recheck).

    The old vf_checks rows (from the .com verify flow) are stale now that the .de
    parser fills more columns — wipe them so a recheck starts clean.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM vf_checks")


def list_words_for_vf_recheck(db_path):
    """Existing words the .de parser can verify: (word_id, lookup, type) for
    verbs/nouns/adjectives (lookup = infinitive for verbs, else german)."""
    out = []
    for entry in list_all_words(db_path):
        wtype = _safe_text(entry.get("type", "")).strip().lower()
        if wtype not in {"verb", "nomen", "adjektiv"}:
            continue
        lookup = entry.get("infinitive") if wtype == "verb" else entry.get("german")
        lookup = _safe_text(lookup).strip()
        if lookup:
            out.append((entry.get("id"), lookup, wtype))
    return out


def save_vf_check_result(
    db_path,
    user_id,
    word_id,
    word_type,
    base_form,
    status,
    retryable=False,
    local_payload=None,
    remote_payload=None,
    diff_payload=None,
    source_hash="",
):
    """Persist single VF check result."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO vf_checks (
                user_id, word_id, word_type, base_form, status, retryable,
                local_payload, remote_payload, diff_payload, source_hash, approved, applied, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, CURRENT_TIMESTAMP)
            """,
            (
                int(user_id),
                _safe_text(word_id),
                _safe_text(word_type),
                _safe_text(base_form),
                _safe_text(status),
                1 if retryable else 0,
                json.dumps(local_payload or {}, ensure_ascii=False),
                json.dumps(remote_payload or {}, ensure_ascii=False),
                json.dumps(diff_payload or {}, ensure_ascii=False),
                _safe_text(source_hash),
            ),
        )


def list_latest_vf_mismatches(db_path, user_id, limit=200):
    """Return latest mismatch rows per word."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT MAX(id) AS id
                FROM vf_checks
                WHERE user_id = ?
                GROUP BY word_id
            )
            SELECT c.*
            FROM vf_checks c
            JOIN latest l ON l.id = c.id
            WHERE c.status = 'mismatch'
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("local_payload", "remote_payload", "diff_payload"):
            raw = item.get(key)
            try:
                item[key] = json.loads(raw) if raw else {}
            except Exception:
                item[key] = {}
        items.append(item)
    return items


def approve_vf_checks(db_path, user_id, ids):
    """Mark selected vf check rows as approved."""
    clean_ids = [int(x) for x in ids if str(x).strip().isdigit()]
    if not clean_ids:
        return 0
    init_db(db_path)
    placeholders = ",".join("?" for _ in clean_ids)
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"""
            UPDATE vf_checks
            SET approved = 1, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND id IN ({placeholders})
            """,
            (int(user_id), *clean_ids),
        )
    return int(cur.rowcount or 0)


def list_approved_vf_checks(db_path, user_id, only_not_applied=True):
    """List approved checks to apply."""
    init_db(db_path)
    where = "WHERE user_id = ? AND approved = 1"
    if only_not_applied:
        where += " AND applied = 0"
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM vf_checks
            {where}
            ORDER BY id ASC
            """,
            (int(user_id),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("local_payload", "remote_payload", "diff_payload"):
            raw = item.get(key)
            try:
                item[key] = json.loads(raw) if raw else {}
            except Exception:
                item[key] = {}
        items.append(item)
    return items


def mark_vf_check_applied(db_path, check_id):
    """Mark VF check row as applied."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE vf_checks
            SET applied = 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(check_id),),
        )


def list_latest_vf_retryable(db_path, user_id, limit=200):
    """List latest retryable failures."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT MAX(id) AS id
                FROM vf_checks
                WHERE user_id = ?
                GROUP BY word_id
            )
            SELECT c.*
            FROM vf_checks c
            JOIN latest l ON l.id = c.id
            WHERE c.retryable = 1
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]
