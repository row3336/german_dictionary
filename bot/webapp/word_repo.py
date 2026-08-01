"""Reads behind the word card (miniapp.md §7.5).

Why this module exists: the card needs columns no existing accessor returns.
`get_word_by_id` omits komparativ/superlativ and both conjugation blocks, and
nothing in storage.py answers "when is this word next due, and which of its forms
are marked weak" for a single word.

It lives here rather than in storage.py only because this branch keeps its hands
off the files feature/multiuser also edits. These functions belong in storage.py
and should move there when the branches meet — they are ordinary data access, not
web logic. The shared connection helper and row mapper are reused so the entry
shape stays identical to every other read in the project.
"""

from ..storage import (  # noqa: PLC2701 - deliberate reuse, see module docstring
    _connect,
    _row_to_entry,
    apply_user_word_media,
    get_synonym_group,
    get_word_study_status,
    init_db,
)

# Weak-form flags, in the order the card lists them. The column prefix is what the
# database calls the form; the label the user sees is German either way (§8.4a).
WEAK_FORM_FIELDS = ("article", "plural", "prateritum", "partizip2")


def get_word_full(db_path, word_id):
    """One word with every column the card can show, or None."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, type, article, german, plural, infinitive, prateritum, partizip2,
                   komparativ, superlativ, russian, ukrainian, english, image, example,
                   topic, preposition_type, case_type, cefr_level,
                   conjugation_praesens, conjugation_praeteritum
            FROM words
            WHERE id = ?
            LIMIT 1
            """,
            (str(word_id or ""),),
        ).fetchone()
    return _row_to_entry(row) if row else None


def apply_media_with_fallback(db_path, user_id, entry):
    """Per-user image/example with COALESCE(personal, shared) — §7.5.

    This restores the fallback removed on 2026-06-27 (commit 8fc83ae), by the
    owner's decision of 2026-08-01. The argument is the data: user_word_media holds
    a single row for the whole database, while words.example is filled for 769 of
    907 words — the stricter rule showed guests an empty block where a perfectly
    good shared example existed.

    A personal value still wins whenever it is set; only a blank one falls through.
    """
    personal = apply_user_word_media(db_path, user_id, entry)
    merged = dict(personal)
    for field in ("image", "example"):
        merged[field] = personal.get(field) or entry.get(field) or ""
    return merged


def get_study_state(db_path, user_id, word_id):
    """Where this word stands for this user: stage, next repetition, weak forms."""
    status = get_word_study_status(db_path, user_id, word_id)
    state, _, repetitions = status.partition(" ")
    schedule = _next_due(db_path, user_id, word_id)
    return {
        # "learning" | "fsrs" | "queue_learning" — the bot's own vocabulary.
        "state": state,
        "repetitions": int(repetitions or 0),
        "due": schedule.get("due", ""),
        "interval": schedule.get("interval", 0),
        "weak_forms": get_weak_forms(db_path, user_id, word_id),
    }


def _next_due(db_path, user_id, word_id):
    """The soonest scheduled repetition across all modes of this word."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT due, interval
            FROM progress
            WHERE user_id = ? AND word_id = ? AND TRIM(COALESCE(due, '')) != ''
            ORDER BY due ASC
            LIMIT 1
            """,
            (int(user_id), str(word_id or "")),
        ).fetchone()
    if not row:
        return {}
    return {"due": row["due"] or "", "interval": int(row["interval"] or 0)}


def get_weak_forms(db_path, user_id, word_id):
    """Forms currently flagged weak for this user, in card order."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT article_active, plural_active, prateritum_active, partizip2_active
            FROM form_stats
            WHERE user_id = ? AND word_id = ?
            LIMIT 1
            """,
            (int(user_id), str(word_id or "")),
        ).fetchone()
    if not row:
        return []
    return [field for field in WEAK_FORM_FIELDS if row[f"{field}_active"]]


def load_card_sources(db_path, user_id, word_id):
    """Everything the card serializer needs, or None when the word is gone."""
    entry = get_word_full(db_path, word_id)
    if not entry:
        return None
    return {
        "entry": apply_media_with_fallback(db_path, user_id, entry),
        "synonyms": get_synonym_group(db_path, word_id),
        "study": get_study_state(db_path, user_id, word_id),
    }
