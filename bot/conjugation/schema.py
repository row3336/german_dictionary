"""Conjugation DDL and admission hooks in the caller's transaction."""

from datetime import datetime, timezone


def ensure_schema(conn):
    """Create schema without committing the enclosing migration transaction."""
    statements = (
        """CREATE TABLE IF NOT EXISTS conjugation_admissions (
            user_id INTEGER NOT NULL, pair TEXT NOT NULL,
            word_id TEXT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            admitted_at TEXT NOT NULL, PRIMARY KEY (user_id,pair,word_id),
            CHECK (pair IN ('ru_de','uk_de','en_de')))""",
        """CREATE TABLE IF NOT EXISTS conjugation_blocks (
            user_id INTEGER NOT NULL, pair TEXT NOT NULL, word_id TEXT NOT NULL,
            tense TEXT NOT NULL CHECK (tense IN ('praesens','praeteritum')),
            phase TEXT NOT NULL DEFAULT 'preparation' CHECK (phase IN ('preparation','test')),
            cycle INTEGER NOT NULL DEFAULT 1 CHECK (cycle >= 1),
            interval INTEGER NOT NULL DEFAULT 1 CHECK (interval >= 1),
            repetitions INTEGER NOT NULL DEFAULT 0 CHECK (repetitions >= 0),
            ease REAL NOT NULL DEFAULT 2.5, due TEXT, prepared_at TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id,pair,word_id,tense),
            FOREIGN KEY (user_id,pair,word_id)
                REFERENCES conjugation_admissions(user_id,pair,word_id) ON DELETE CASCADE,
            CHECK ((phase='preparation' AND due IS NULL) OR (phase='test' AND due IS NOT NULL)))""",
        """CREATE TABLE IF NOT EXISTS conjugation_passes (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, pair TEXT NOT NULL,
            word_id TEXT NOT NULL, tense TEXT NOT NULL, cycle INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active','completed','reset')),
            pronoun_order_json TEXT NOT NULL, expected_forms_json TEXT NOT NULL,
            baseline_srs_json TEXT NOT NULL,
            applied_result TEXT CHECK (applied_result IN ('wrong','correct')),
            result_day TEXT, revision INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL, completed_at TEXT,
            FOREIGN KEY (user_id,pair,word_id,tense)
                REFERENCES conjugation_blocks(user_id,pair,word_id,tense) ON DELETE CASCADE)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS conjugation_one_active_pass
            ON conjugation_passes(user_id,pair,word_id,tense,cycle) WHERE status='active'""",
        """CREATE TABLE IF NOT EXISTS conjugation_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            pass_id TEXT NOT NULL REFERENCES conjugation_passes(id) ON DELETE CASCADE,
            pronoun_group TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 6),
            answer_text TEXT NOT NULL, is_correct INTEGER NOT NULL CHECK (is_correct IN (0,1)),
            corrected INTEGER NOT NULL DEFAULT 0 CHECK (corrected IN (0,1)),
            original_text TEXT NOT NULL, original_correct INTEGER NOT NULL CHECK (original_correct IN (0,1)),
            answered_at TEXT NOT NULL, answer_day TEXT NOT NULL,
            correction_session_id TEXT, action_key TEXT NOT NULL,
            UNIQUE(user_id,action_key), UNIQUE(pass_id,pronoun_group), UNIQUE(pass_id,ordinal))""",
        """CREATE INDEX IF NOT EXISTS conjugation_blocks_due
            ON conjugation_blocks(user_id,pair,phase,due)""",
        """CREATE INDEX IF NOT EXISTS conjugation_answers_session
            ON conjugation_answers(user_id,correction_session_id,id)""",
    )
    for statement in statements:
        conn.execute(statement)


def admit_if_qualified(conn, user_id, word_id, mode, repetitions, now_iso=None):
    """Latch exact written-pair admission; eligibility is checked separately."""
    if mode not in ('ru_de', 'uk_de', 'en_de') or int(repetitions or 0) < 2:
        return False
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO conjugation_admissions(user_id,pair,word_id,admitted_at)
           SELECT ?,?,id,? FROM words WHERE id=? AND type='verb'
           ON CONFLICT(user_id,pair,word_id) DO NOTHING""",
        (int(user_id), mode, now_iso, word_id),
    )
    return bool(cursor.rowcount)


def seed_admissions(conn, now_iso=None):
    """Seed knowable qualification, never grades inferred from carousel views."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO conjugation_admissions(user_id,pair,word_id,admitted_at)
           SELECT p.user_id,p.mode,p.word_id,? FROM progress p JOIN words w ON w.id=p.word_id
           WHERE p.mode IN ('ru_de','uk_de','en_de') AND p.repetitions>=2 AND w.type='verb'
           ON CONFLICT(user_id,pair,word_id) DO NOTHING""", (now_iso,),
    )
