"""Weak-form drilling without Telegram: the verdict, the scoring write, the picker.

Split out of `handlers/weak.py` so the Mini App drills the same four fields
through the same code as the chat (miniapp.md §5.1). The four chat handlers were
one block of logic copied four times with a field name changed; that block is
here once, parameterized by the field, and the handlers keep only what actually
differs — the prompt text and the keyboard.

Nothing here touches PTB's `Update` or a Telegram object. `context` is used, but
only as the duck-typed session holder the web side also provides (§5.2): the drill
queue, the "last answered" snapshot and the per-session word set all live in
`context.user_data` and have to be shared, not re-implemented.

Weak is not FSRS. There is no interval and no due date — a form is either flagged
weak (`*_active`) or in the review ladder (`*_review_stage`), and a wrong answer
queues the word for in-session repeats (the drill queue) instead of shortening an
interval.
"""

import random

from ..app_config import get_db_path
from ..storage import (
    get_form_review_stage,
    get_form_state,
    list_weak_words,
    log_quiz_event,
    update_form_stats,
    update_weak_review_state,
)
from ..text_utils import normalize_answer as _normalize_answer
from ..ui.formatting import (  # noqa: PLC2701 - the chat's own checkers, reused verbatim
    _article_matches,
    _expected_partizip2,
)
from .quiz_core import _plural_matches
from .session import (
    _enqueue_weak_drill,
    _mark_session_word,
    _maybe_take_weak_drill_entry,
    _set_weak_drill_since_last,
    _weak_drill_since_last,
)
from .txlog import _capture_transaction_snapshot, _log_transaction
from .word_filter import resolve_quiz_filter

WEAK_FIELDS = ("article", "plural", "prateritum", "partizip2")

# Which words a field can even be asked about. Not a display choice: asking a verb
# for its plural would put a word into the queue that can never leave it.
WEAK_WORD_TYPES = {
    "article": "nomen",
    "plural": "nomen",
    "prateritum": "verb",
    "partizip2": "verb",
}

# How many weak words the picker looks at. The bot's own cap; `list_weak_words`
# orders by "active first, then earliest review", so a larger pool would only add
# words that are further from being due.
WEAK_POOL_LIMIT = 300


def weak_mode(field):
    """The mode string weak sessions are keyed by, in the bot's own spelling."""
    return f"weak_{field}"


def weak_verdict(entry, field, text, dont_know=False):
    """Was this answer right? The chat's own comparison, per field.

    `dont_know` short-circuits to wrong without looking at the text — the same
    thing the chat's "I don't know" button does before it ever reaches a checker.
    """
    if dont_know:
        return False
    if field == "article":
        return _article_matches(text, entry.get("article", ""))
    if field == "plural":
        return _plural_matches(text, entry.get("plural", ""))
    if field == "prateritum":
        return _normalize_answer(text) == _normalize_answer(entry.get("prateritum", ""))
    if field == "partizip2":
        return _normalize_answer(text) == _expected_partizip2(entry)
    raise ValueError(f"not a weak field: {field!r}")


def remember_last_weak_answer(context, entry, field, ok):
    """Snapshot the answer just given, so input_err knows what it may roll back."""
    context.user_data["last_weak_answered_entry"] = entry
    context.user_data["last_weak_answered_field"] = field
    context.user_data["last_weak_answered_ok"] = bool(ok)


def score_weak_answer(context, user_id, entry, field, ok):
    """Record one answer: form stats, the review ladder, the log and the drill queue.

    Returns False without writing anything when the word on screen is a drill
    repeat: drills are in-session practice, so they must not move the weak stats,
    the review stage or the session's word count — otherwise a single wrong answer
    would keep re-scoring itself for as long as it is being drilled.
    """
    if context.user_data.get("quiz_is_weak_drill"):
        return False

    db = get_db_path()
    word_id = entry.get("id")
    mode = weak_mode(field)
    # Read before the write: `update_form_stats` may clear the active flag, and the
    # branch below has to know which state the answer was given in.
    state = get_form_state(db, user_id, word_id, field)
    snapshot = _capture_transaction_snapshot(user_id, word_id, [mode])
    update_form_stats(db, user_id, word_id, {field: ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(db, user_id, word_id, field, ok)
    else:
        stage_snapshot = get_form_review_stage(db, user_id, word_id, field)
    log_quiz_event(
        db,
        user_id=user_id,
        word_id=word_id,
        mode=mode,
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    remember_last_weak_answer(context, entry, field, ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _mark_session_word(context, mode, word_id)
    _log_transaction(user_id, f"quiz:{mode}", snapshot)
    if not ok:
        _enqueue_weak_drill(context, mode, word_id, user_id)
    return True


def weak_due_words(user_id, field, word_type=None, limit=WEAK_POOL_LIMIT):
    """Words whose `field` is weak or due for review, under the user's filter."""
    quiz_filter = resolve_quiz_filter(user_id)
    return list_weak_words(
        get_db_path(),
        user_id=user_id,
        weak_field=field,
        word_type=word_type,
        topics=quiz_filter.topics,
        cefr_levels=quiz_filter.cefr_levels,
        limit=limit,
    )


def _pick_weak_entry(context, user_id, weak_field, word_type=None):
    """A fresh due word for this field: flagged-weak first, review words after."""
    due = weak_due_words(user_id, weak_field, word_type=word_type)
    if not due:
        return None
    active = [w for w in due if w.get("is_active")]
    pool_src = active if active else due
    # Read-only: avoid repeating the last shown word; the final pick is recorded
    # by pick_weak_or_drill_entry (so it reflects drill overrides too).
    last_id = context.user_data.get("last_quiz_ids", {}).get(weak_mode(weak_field))
    pool = [w for w in pool_src if w.get("id") != last_id] if len(pool_src) > 1 else pool_src
    if not pool:
        pool = pool_src
    return random.choice(pool)


def pick_weak_or_drill_entry(context, user_id, weak_field, word_type=None):
    """Pick the next weak word: a fresh due word, or a drill repeat — never two in a row."""
    mode = weak_mode(weak_field)
    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    last_id = last_ids.get(mode)
    fresh = _pick_weak_entry(context, user_id, weak_field, word_type=word_type)

    drill = _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=last_id, force=False)
    if drill:
        entry, is_drill = drill, True
    elif fresh:
        entry, is_drill = fresh, False
    else:
        # No fresh due word left — fall back to a drill (still avoid consecutive if possible).
        drill = _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=last_id, force=True)
        if not drill:
            drill = _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=None, force=True)
        entry, is_drill = (drill, True) if drill else (None, False)

    if not entry:
        return None

    last_ids[mode] = entry.get("id")
    context.user_data["quiz_is_weak_drill"] = is_drill
    if not is_drill:
        _set_weak_drill_since_last(context, mode, _weak_drill_since_last(context, mode) + 1)
    return entry
