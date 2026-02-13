import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

DEFAULT_EASE = 2.5
MIN_EASE = 1.3

QUIZ_RU_DE = "ru_de"
QUIZ_DE_RU = "de_ru"
QUIZ_ARTICLE = "article"
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
INTERFACE_LANGS = ("en", "ru", "uk")
DICT_LANGS = ("ru", "uk", "en")


def default_srs(today=None):
    if today is None:
        today = date.today()
    return {
        "interval": 1,
        "repetitions": 0,
        "ease": DEFAULT_EASE,
        "due": today.isoformat(),
    }


def _connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
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
                russian TEXT NOT NULL DEFAULT '',
                ukrainian TEXT NOT NULL DEFAULT '',
                english TEXT NOT NULL DEFAULT '',
                cefr_level TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS user_levels (
                user_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                PRIMARY KEY (user_id, level),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
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

            CREATE TABLE IF NOT EXISTS quiz_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                word_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                prev_is_correct INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );
            """
        )
        _ensure_schema(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_words_cefr ON words(cefr_level)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_levels_user ON user_levels(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_word_flags_status_created ON word_flags(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_events_day_user ON quiz_events(created_at, user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_events_user_word_mode ON quiz_events(user_id, word_id, mode, created_at)")


def _ensure_schema(conn):
    word_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(words)").fetchall()
    }
    if "cefr_level" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN cefr_level TEXT NOT NULL DEFAULT ''")
    if "ukrainian" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN ukrainian TEXT NOT NULL DEFAULT ''")
    if "english" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN english TEXT NOT NULL DEFAULT ''")

    user_cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "interface_lang" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN interface_lang TEXT NOT NULL DEFAULT 'en'")
    if "dict_lang" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN dict_lang TEXT NOT NULL DEFAULT 'ru'")


def _normalize_text(text):
    return " ".join(str(text).strip().lower().split())


def _safe_text(value):
    if value is None:
        return ""
    return str(value)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_level(level):
    value = _safe_text(level).strip().upper()
    return value if value in CEFR_LEVELS else ""


def _entry_key(entry):
    entry_type = _normalize_text(entry.get("type", ""))
    if entry_type == "verb":
        return ("verb", _normalize_text(entry.get("infinitive", "")))
    return (entry_type or "nomen", _normalize_text(entry.get("german", "")))


def _row_to_entry(row):
    return {
        "id": row["id"],
        "type": row["type"],
        "article": row["article"] or "",
        "german": row["german"] or "",
        "plural": row["plural"] or "",
        "infinitive": row["infinitive"] or "",
        "prateritum": row["prateritum"] or "",
        "partizip2": row["partizip2"] or "",
        "russian": row["russian"] or "",
        "ukrainian": row["ukrainian"] or "",
        "english": row["english"] or "",
        "cefr_level": row["cefr_level"] or "",
    }


def load_words(db_path):
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian, cefr_level
            , ukrainian, english
            FROM words
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


def count_words(db_path):
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM words").fetchone()
    return int(row["cnt"])


def register_or_update_user(db_path, user_id, username="", first_name=""):
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

        conn.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at
            )
            VALUES (?, ?, ?, 'pending', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (int(user_id), _safe_text(username), _safe_text(first_name)),
        )
        _ensure_default_user_levels(conn, int(user_id))
    return "pending"


def get_user_status(db_path, user_id):
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


def _normalize_interface_lang(value):
    token = _safe_text(value).strip().lower()
    return token if token in INTERFACE_LANGS else ""


def _normalize_dict_lang(value):
    token = _safe_text(value).strip().lower()
    return token if token in DICT_LANGS else ""


def get_user_languages(db_path, user_id):
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
    value = _normalize_interface_lang(interface_lang)
    if not value:
        raise ValueError("Invalid interface language.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at)
            VALUES (?, '', '', 'pending', ?, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                interface_lang = excluded.interface_lang,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(user_id), value),
        )
        _ensure_default_user_levels(conn, int(user_id))


def set_user_dict_lang(db_path, user_id, dict_lang):
    value = _normalize_dict_lang(dict_lang)
    if not value:
        raise ValueError("Invalid dictionary language.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, interface_lang, dict_lang, created_at, updated_at)
            VALUES (?, '', '', 'pending', '', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                dict_lang = excluded.dict_lang,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(user_id), value),
        )
        _ensure_default_user_levels(conn, int(user_id))


def ensure_default_languages_for_existing_users(db_path, interface_lang="en", dict_lang="ru"):
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
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_safe_text(key),),
        ).fetchone()
    if not row:
        return default
    return row["value"]


def find_existing_word(db_path, raw_word):
    candidate = _normalize_text(raw_word)
    if not candidate:
        return None

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian, cefr_level
            , ukrainian, english
            FROM words
            WHERE lower(trim(german)) = ? OR lower(trim(infinitive)) = ?
            LIMIT 1
            """,
            (candidate, candidate),
        ).fetchone()
    if not row:
        return None
    return _row_to_entry(row)


def _dedupe_entry(conn, entry):
    key_type, key_value = _entry_key(entry)
    if key_type == "verb":
        row = conn.execute(
            "SELECT id FROM words WHERE type = 'verb' AND lower(trim(infinitive)) = ? LIMIT 1",
            (key_value,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM words WHERE lower(trim(type)) = ? AND lower(trim(german)) = ? LIMIT 1",
            (key_type, key_value),
        ).fetchone()
    if row:
        raise ValueError("Already in database.")


def add_word(db_path, entry):
    init_db(db_path)
    entry = dict(entry)
    if entry.get("id") in (None, "", "unique"):
        entry["id"] = str(uuid.uuid4())

    with _connect(db_path) as conn:
        _dedupe_entry(conn, entry)
        conn.execute(
            """
            INSERT INTO words (
                id, type, article, german, plural, infinitive, prateritum, partizip2, russian, ukrainian, english, cefr_level
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _safe_text(entry.get("russian", "")),
                _safe_text(entry.get("ukrainian", "")),
                _safe_text(entry.get("english", "")),
                _normalize_level(entry.get("cefr_level", "")),
            ),
        )
    return entry


def _progress_for_row(row):
    if not row["p_due"]:
        return default_srs()
    return {
        "interval": int(row["p_interval"]),
        "repetitions": int(row["p_repetitions"]),
        "ease": float(row["p_ease"]),
        "due": row["p_due"],
    }


def select_due(db_path, user_id, mode, word_type=None, levels=None, today=None):
    if today is None:
        today = date.today()
    due_today = today.isoformat()
    init_db(db_path)

    where_clauses = ["COALESCE(p.due, ?) <= ?"]
    params = [int(user_id), mode, due_today, due_today]
    if word_type:
        where_clauses.insert(0, "w.type = ?")
        params.insert(2, word_type)
    if levels:
        safe_levels = [lvl for lvl in (_normalize_level(x) for x in levels) if lvl]
        if safe_levels:
            placeholders = ",".join("?" for _ in safe_levels)
            where_clauses.append(f"upper(w.cefr_level) IN ({placeholders})")
            params.extend(safe_levels)

    where_sql = " AND ".join(where_clauses)

    query = f"""
        SELECT
            w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.russian, w.cefr_level,
            w.ukrainian, w.english,
            p.interval AS p_interval, p.repetitions AS p_repetitions, p.ease AS p_ease, p.due AS p_due
        FROM words w
        LEFT JOIN progress p
            ON p.word_id = w.id AND p.user_id = ? AND p.mode = ?
        WHERE {where_sql}
    """

    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    result = []
    for row in rows:
        entry = _row_to_entry(row)
        entry["srs"] = _progress_for_row(row)
        result.append(entry)
    return result


def update_sm2(srs, quality, today=None):
    if today is None:
        today = date.today()

    quality = max(0, min(5, int(quality)))
    interval = int(srs.get("interval", 1))
    repetitions = int(srs.get("repetitions", 0))
    ease = float(srs.get("ease", DEFAULT_EASE))

    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 2
        elif repetitions == 1:
            interval = 6
        else:
            interval = int(round(interval * ease))
        repetitions += 1

    ease = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(MIN_EASE, ease)

    due = today + timedelta(days=interval)
    return {
        "interval": interval,
        "repetitions": repetitions,
        "ease": round(ease, 2),
        "due": due.isoformat(),
    }


def update_progress(db_path, user_id, word_id, mode, quality):
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
        updated = update_sm2(srs, quality)

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


def log_quiz_event(db_path, user_id, word_id, mode, is_correct):
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
            INSERT INTO quiz_events (user_id, word_id, mode, is_correct, prev_is_correct, created_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                int(user_id),
                _safe_text(word_id),
                _safe_text(mode),
                1 if bool(is_correct) else 0,
                prev_is_correct,
            ),
        )


def list_daily_user_stats(db_path, day_iso, user_id=None):
    init_db(db_path)
    params = [str(day_iso)]
    where = "date(e.created_at) = date(?)"
    if user_id is not None:
        where += " AND e.user_id = ?"
        params.append(int(user_id))

    query = f"""
        SELECT
            e.user_id AS user_id,
            u.username AS username,
            u.first_name AS first_name,
            COUNT(*) AS total_attempts,
            SUM(CASE WHEN e.is_correct = 1 THEN 1 ELSE 0 END) AS correct_attempts,
            COUNT(DISTINCT e.word_id) AS unique_words,
            SUM(CASE WHEN e.is_correct = 1 AND e.prev_is_correct = 0 THEN 1 ELSE 0 END) AS wrong_to_correct,
            SUM(CASE WHEN e.is_correct = 1 AND e.prev_is_correct = 1 THEN 1 ELSE 0 END) AS correct_to_correct,
            SUM(CASE WHEN e.is_correct = 1 AND e.prev_is_correct IS NULL THEN 1 ELSE 0 END) AS first_time_correct
        FROM quiz_events e
        LEFT JOIN users u ON u.user_id = e.user_id
        WHERE {where}
        GROUP BY e.user_id, u.username, u.first_name
        ORDER BY total_attempts DESC, correct_attempts DESC
    """
    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    result = []
    for row in rows:
        total = int(row["total_attempts"] or 0)
        correct = int(row["correct_attempts"] or 0)
        win_rate = round((correct / total) * 100, 1) if total > 0 else 0.0
        wrong_to_correct = int(row["wrong_to_correct"] or 0)
        correct_to_correct = int(row["correct_to_correct"] or 0)
        first_time_correct = int(row["first_time_correct"] or 0)
        memory_score = round(
            correct_to_correct + (wrong_to_correct * 0.7) + (first_time_correct * 0.5),
            1,
        )
        result.append(
            {
                "user_id": int(row["user_id"]),
                "username": row["username"] or "",
                "first_name": row["first_name"] or "",
                "total_attempts": total,
                "correct_attempts": correct,
                "win_rate": win_rate,
                "unique_words": int(row["unique_words"] or 0),
                "wrong_to_correct": wrong_to_correct,
                "correct_to_correct": correct_to_correct,
                "first_time_correct": first_time_correct,
                "memory_score": memory_score,
            }
        )
    return result


def get_daily_totals(db_path, day_iso):
    rows = list_daily_user_stats(db_path, day_iso=day_iso)
    if not rows:
        return {
            "total_attempts": 0,
            "correct_attempts": 0,
            "win_rate": 0.0,
            "unique_users": 0,
            "unique_words": 0,
            "wrong_to_correct": 0,
            "correct_to_correct": 0,
            "first_time_correct": 0,
            "memory_score": 0.0,
        }

    total_attempts = sum(x["total_attempts"] for x in rows)
    correct_attempts = sum(x["correct_attempts"] for x in rows)
    unique_words = sum(x["unique_words"] for x in rows)
    wrong_to_correct = sum(x["wrong_to_correct"] for x in rows)
    correct_to_correct = sum(x["correct_to_correct"] for x in rows)
    first_time_correct = sum(x["first_time_correct"] for x in rows)
    memory_score = round(sum(x["memory_score"] for x in rows), 1)
    return {
        "total_attempts": total_attempts,
        "correct_attempts": correct_attempts,
        "win_rate": round((correct_attempts / total_attempts) * 100, 1) if total_attempts > 0 else 0.0,
        "unique_users": len(rows),
        "unique_words": unique_words,
        "wrong_to_correct": wrong_to_correct,
        "correct_to_correct": correct_to_correct,
        "first_time_correct": first_time_correct,
        "memory_score": memory_score,
    }


def list_daily_leaderboard(db_path, day_iso, min_attempts=1, limit=20):
    rows = list_daily_user_stats(db_path, day_iso=day_iso)
    filtered = [r for r in rows if r["total_attempts"] >= int(min_attempts)]
    filtered.sort(key=lambda x: (x["win_rate"], x["total_attempts"], x["memory_score"]), reverse=True)
    return filtered[: int(limit)]


def list_words_for_cefr_backfill(db_path, limit=0):
    init_db(db_path)
    query = """
        SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian, cefr_level
        , ukrainian, english
        FROM words
        WHERE trim(coalesce(cefr_level, '')) = ''
        ORDER BY created_at ASC
    """
    params = ()
    if int(limit) > 0:
        query += " LIMIT ?"
        params = (int(limit),)

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_entry(row) for row in rows]


def list_words_for_translations_backfill(db_path, limit=0):
    init_db(db_path)
    query = """
        SELECT id, type, article, german, plural, infinitive, prateritum, partizip2, russian, ukrainian, english, cefr_level
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
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE words
            SET ukrainian = COALESCE(?, ukrainian),
                english = COALESCE(?, english)
            WHERE id = ?
            """,
            (
                None if ukrainian is None else _safe_text(ukrainian),
                None if english is None else _safe_text(english),
                _safe_text(word_id),
            ),
        )


def update_word_cefr_level(db_path, word_id, level):
    safe_level = _normalize_level(level)
    if not safe_level:
        raise ValueError("Invalid CEFR level.")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE words SET cefr_level = ? WHERE id = ?",
            (safe_level, _safe_text(word_id)),
        )


def update_word_fields(db_path, word_id, fields):
    allowed = {
        "type",
        "article",
        "german",
        "plural",
        "infinitive",
        "prateritum",
        "partizip2",
        "russian",
        "ukrainian",
        "english",
        "cefr_level",
    }
    values = {}
    for key, value in dict(fields or {}).items():
        if key not in allowed:
            continue
        if key == "cefr_level":
            values[key] = _normalize_level(value)
        else:
            values[key] = _safe_text(value)
    if not values:
        return

    parts = [f"{key} = ?" for key in values]
    params = list(values.values()) + [_safe_text(word_id)]
    query = f"UPDATE words SET {', '.join(parts)} WHERE id = ?"

    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(query, tuple(params))


def add_word_flag(db_path, word_id, user_id, comment):
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
    init_db(db_path)
    query = """
        SELECT
            f.id AS flag_id,
            f.word_id AS flag_word_id,
            f.user_id AS flag_user_id,
            f.comment AS flag_comment,
            f.created_at AS flag_created_at,
            w.id, w.type, w.article, w.german, w.plural, w.infinitive, w.prateritum, w.partizip2, w.russian, w.cefr_level
            , w.ukrainian, w.english
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


def create_template_from_word(raw_word):
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
