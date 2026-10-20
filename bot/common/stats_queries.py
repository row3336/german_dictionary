"""Narrow statistics reads; SQL helpers can share their caller's connection."""

from datetime import date

from ..storage import _connect, select_due


def count_today(conn, user_id, mode, day):
    """Count distinct original cards in one exact mode and matching word type."""
    comparison = "=" if str(mode).startswith("phrase_") else "!="
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT e.word_id)
            FROM quiz_events e JOIN words w ON w.id = e.word_id
            WHERE e.user_id = ? AND e.mode = ? AND date(e.created_at) = date(?)
              AND LOWER(TRIM(w.type)) {comparison} 'phrase'""",
        (int(user_id), mode, day.isoformat()),
    ).fetchone()
    return int(row[0] or 0)


def fsrs_word_counts(conn):
    """One learned word per user, regardless of its number of progress modes."""
    return {
        int(row[0]): int(row[1]) for row in conn.execute(
            "SELECT user_id, COUNT(DISTINCT word_id) FROM progress "
            "WHERE repetitions >= 1 GROUP BY user_id"
        ).fetchall()
    }


def word_type_counts(conn):
    """Keep unknown and blank types in the dynamic dictionary total too."""
    return {
        row[0]: int(row[1]) for row in conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(LOWER(type)), ''), '-') AS word_type, COUNT(*) "
            "FROM words GROUP BY word_type ORDER BY word_type"
        ).fetchall()
    }


def progress_breakdown(db_path, user_id, modes, today=None):
    """Reuse quiz admission/due rules, without summing word and phrase modes."""
    today = today or date.today()
    due = {
        mode: len(select_due(db_path, user_id=user_id, mode=mode, today=today,
                             exclude_correct_today=True))
        for mode in modes
    }
    with _connect(db_path) as conn:
        completed = {mode: count_today(conn, user_id, mode, today) for mode in modes}
    return {"due": due, "today": completed}


def learned_word_counts(db_path):
    with _connect(db_path) as conn:
        return fsrs_word_counts(conn)


def dictionary_type_counts(db_path):
    with _connect(db_path) as conn:
        return word_type_counts(conn)
