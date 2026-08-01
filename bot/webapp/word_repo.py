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
    _normalize_text,
    _row_to_entry,
    _strip_leading_article,
    apply_user_word_media,
    get_synonym_group,
    get_word_study_status,
    init_db,
)

# Weak-form flags, in the order the card lists them. The column prefix is what the
# database calls the form; the label the user sees is German either way (§8.4a).
WEAK_FORM_FIELDS = ("article", "plural", "prateritum", "partizip2")

# Columns a search looks through — every written form plus all three translations,
# the same set the bot's search_word uses, so a query finds the same word in the
# app as it does in the chat.
_SEARCHED_COLUMNS = (
    "german",
    "infinitive",
    "plural",
    "prateritum",
    "partizip2",
    "russian",
    "ukrainian",
    "english",
)

# The longest query we send to the database (§7.5). Anything past this is not a
# word, and a card is not the answer to it.
MAX_QUERY_LENGTH = 100

# Enough candidates for a disambiguation screen and no more: past a handful the
# list stops being a choice and becomes a search result page, which the app does
# not have (§2 — the dictionary is the smaller half of v1).
MAX_CANDIDATES = 5

# Every column the card can show. Interpolated into SQL from this constant only —
# never from anything a request carries; user values always travel as ? parameters.
_CARD_COLUMNS = """
    id, type, article, german, plural, infinitive, prateritum, partizip2,
    komparativ, superlativ, russian, ukrainian, english, image, example,
    topic, preposition_type, case_type, cefr_level,
    conjugation_praesens, conjugation_praeteritum
"""


def get_word_full(db_path, word_id):
    """One word with every column the card can show, or None."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_CARD_COLUMNS} FROM words WHERE id = ? LIMIT 1",
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


def search_words(db_path, token, limit=MAX_CANDIDATES):
    """Words matching `token`, best kind of match first (§7.5).

    Exact matches on any written form or translation come first; only when there
    are none does the search fall back to a substring match. The column set and the
    leading-article stripping mirror the bot's own `search_word`, so "der Tisch"
    and "Tische" find the same word in both places.

    Unlike the bot, this returns several rows. `search_word` takes LIMIT 1, which
    means the chat silently picks one of two homonyms; the app is specified to show
    the choice instead (§7.5), so it has to see them all.
    """
    candidate = _normalize_text(token)
    if not candidate:
        return []
    stripped = _strip_leading_article(candidate)
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT {_CARD_COLUMNS}
            FROM words
            WHERE {" OR ".join(f"lower(trim({col})) IN (?, ?)" for col in _SEARCHED_COLUMNS)}
            LIMIT ?
            """,
            _pair_params(candidate, stripped) + [int(limit)],
        ).fetchall()
        if not rows:
            condition = " OR ".join(
                f"lower({col}) LIKE ? ESCAPE '\\' OR lower({col}) LIKE ? ESCAPE '\\'"
                for col in _SEARCHED_COLUMNS
            )
            rows = conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM words WHERE {condition} LIMIT ?",
                _pair_params(_like_pattern(candidate), _like_pattern(stripped)) + [int(limit)],
            ).fetchall()
    return [_row_to_entry(row) for row in rows]


def _like_pattern(value):
    """Wrap a query in wildcards, escaping the ones the user typed.

    Without this a search for `%` matches the entire dictionary and a search for
    `_` matches every two-letter difference: not an injection (values still travel
    as parameters) but the user's text would be read as pattern syntax rather than
    as the word they are looking for.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _pair_params(first, second):
    """The (value, stripped-value) pair repeated once per searched column."""
    params = []
    for _ in _SEARCHED_COLUMNS:
        params.extend((first, second))
    return params


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
