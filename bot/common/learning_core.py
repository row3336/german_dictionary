"""Learning mode without Telegram: the show order, the verdict, the writes.

Split out of `handlers/learning.py` so the Mini App runs the learning batch
through the same code as the chat (miniapp.md §5.1). What is left in the handler
is what actually speaks Telegram — the prompt text, the keyboards, the reactions.

Learning is neither FSRS nor weak. A word enters a batch, is previewed once, and
is then asked on a fixed ladder of intervals (`_learning_schedule_seconds`); a
right answer moves it one step up and the last step completes it into FSRS, a
wrong one drops it back to step 0. There is no interval algorithm to disagree
about — but there IS a show order, and that is the part worth sharing: the first
round follows the preview order exactly, later rounds are random with a spacing
window, and mix mode never touches either (it only shapes which words enter the
batch — see storage.learning_pick_new_words).

`context` is used only as the duck-typed session holder the web side also
provides (§5.2): the spacing window and the "last answered" snapshot live in
`context.user_data` and have to be shared, not re-implemented.
"""

import random
from collections import namedtuple

from ..app_config import get_db_path
from ..storage import (
    learning_active_count,
    learning_add_word_by_id,
    learning_add_words,
    learning_due_entries,
    learning_get_or_create_batch,
    learning_mark_preview_next,
    learning_pick_new_words,
    learning_preview_entry,
    learning_skip_word,
    learning_update_result,
    log_quiz_event,
)
from .quiz_core import _check_ru_de_strict
from .settings import (
    _learning_daily_qty,
    _learning_mix_mode_enabled,
    _learning_schedule_seconds,
)
from .txlog import _capture_transaction_snapshot, _log_transaction
from .word_filter import resolve_quiz_filter

# The mode string learning answers are logged and keyed by.
LEARNING_MODE = "learning"

# A word must not reappear within this many consecutive shows (>= once per three).
LEARNING_RECENT_SHOWS = 2

# Why a top-up added nothing, when it did.
FILL_OK = ""
FILL_BATCH_FULL = "batch_full"
FILL_NO_NEW = "no_new"

FillResult = namedtuple("FillResult", ("added", "active", "daily", "reason"))


# --- the batch ---------------------------------------------------------------

def fill_batch(user_id):
    """Top the batch up to the daily quota ("add for learning").

    Reports why nothing was added rather than deciding what to say about it: the
    chat has one wording for a full batch and another for an exhausted
    dictionary, and the app needs the same distinction in JSON.
    """
    db = get_db_path()
    daily_qty = _learning_daily_qty(db, user_id)
    batch_id = learning_get_or_create_batch(db, user_id=user_id, daily_qty=daily_qty)
    active_count = learning_active_count(db, user_id=user_id, batch_id=batch_id)
    need = max(0, daily_qty - active_count)
    if need <= 0:
        return FillResult(added=0, active=active_count, daily=daily_qty, reason=FILL_BATCH_FULL)

    quiz_filter = resolve_quiz_filter(user_id)
    picked = learning_pick_new_words(
        db,
        user_id=user_id,
        limit=need,
        topics=quiz_filter.topics,
        cefr_levels=quiz_filter.cefr_levels,
        mix=_learning_mix_mode_enabled(user_id),
    )
    if not picked:
        return FillResult(added=0, active=active_count, daily=daily_qty, reason=FILL_NO_NEW)

    added = learning_add_words(
        db,
        user_id=user_id,
        words=picked,
        batch_id=batch_id,
        first_delay_seconds=_learning_schedule_seconds(db, user_id)[0],
    )
    return FillResult(
        added=added,
        active=active_count + added,
        daily=daily_qty,
        reason=FILL_OK,
    )


def add_word_to_batch(user_id, word_id):
    """Send one word into the learning batch ("send to learning"). True when added."""
    db = get_db_path()
    batch_id = learning_get_or_create_batch(db, user_id=user_id, daily_qty=_learning_daily_qty(db, user_id))
    added = learning_add_word_by_id(
        db,
        user_id=user_id,
        word_id=word_id,
        batch_id=batch_id,
        first_delay_seconds=_learning_schedule_seconds(db, user_id)[0],
    )
    return bool(added)


# --- picking what to show ----------------------------------------------------

def preview_entry(user_id):
    """The next word waiting to be previewed, or None."""
    return learning_preview_entry(get_db_path(), user_id=user_id)


def due_entries(user_id):
    """Everything the running queue could show right now, in show order."""
    return learning_due_entries(get_db_path(), user_id=user_id)


def pick_learning_due_entry(context, user_id):
    """Pick the next word to show in the running learning queue.

    STATIC show rule — identical whether or not mix mode is on (mix only shapes
    which words enter the queue, never their show order):
      - first round (freshly previewed, step_index == 0): strict preview order
        (words as they were reviewed, i.e. created_at ASC from the query);
      - later rounds (step_index > 0): random, but a word is not shown again
        within the last two shows — at most once per three consecutive shows.
    Recent shows are tracked in context ("learning_recent_ids"); when the pool is
    too small to honour the spacing, it relaxes as far as needed.
    """
    entries = due_entries(user_id)
    if not entries:
        return None
    recent = context.user_data.get("learning_recent_ids") or []
    last_shown = recent[-1] if recent else None

    first_round = [entry for entry in entries if int(entry.get("learning_step") or 0) == 0]
    if first_round:
        # already ordered by created_at ASC (preview order) in the query
        for entry in first_round:
            if entry.get("id") != last_shown:
                return entry
        return first_round[0]

    pool = [entry for entry in entries if entry.get("id") not in recent]
    if not pool:
        pool = [entry for entry in entries if entry.get("id") != last_shown] or entries
    return random.choice(pool)


def note_learning_shown(context, word_id):
    """Record a shown word for the later-round spacing window (keep last N ids)."""
    if not word_id:
        return
    recent = context.user_data.get("learning_recent_ids") or []
    recent.append(word_id)
    context.user_data["learning_recent_ids"] = recent[-LEARNING_RECENT_SHOWS:]


def forget_shown_history(context):
    """Drop the spacing window — entering the mode and leaving the queue both do."""
    context.user_data.pop("learning_recent_ids", None)


# --- the preview phase -------------------------------------------------------

def mark_preview_next(user_id, word_id):
    """"Next" in the preview: the word is reviewed and joins the running queue."""
    snapshot = _capture_transaction_snapshot(user_id, word_id, [LEARNING_MODE])
    applied = learning_mark_preview_next(
        get_db_path(),
        user_id=user_id,
        word_id=word_id,
        first_interval_seconds=_learning_schedule_seconds(get_db_path(), user_id)[0],
    )
    _log_transaction(user_id, "learning:preview_next", snapshot)
    return bool(applied)


def skip_word(user_id, word_id):
    """"Skip": the word leaves the batch entirely, previewed or not."""
    snapshot = _capture_transaction_snapshot(user_id, word_id, [LEARNING_MODE])
    applied = learning_skip_word(get_db_path(), user_id=user_id, word_id=word_id)
    _log_transaction(user_id, "learning:skip", snapshot)
    return bool(applied)


# --- answering ---------------------------------------------------------------

def learning_verdict(entry, text, dont_know=False):
    """Was this answer right? The chat's own L1→De comparison.

    `dont_know` short-circuits to wrong without looking at the text — the same
    thing the chat's "I don't know" button does before it reaches a checker.
    """
    if dont_know:
        return False
    ok, _ = _check_ru_de_strict(entry, text)
    return ok


def remember_last_learning_answer(context, entry, ok):
    """Snapshot the answer just given, so input_err knows what it may roll back.

    The step the word was on BEFORE the answer is part of the snapshot: the
    rollback re-advances from there, and by the time it runs the stored step has
    already moved.
    """
    context.user_data["last_learning_entry_id"] = entry.get("id")
    context.user_data["last_learning_ok"] = bool(ok)
    context.user_data["last_learning_prev_step"] = int(entry.get("learning_step") or 0)


def score_learning_answer(context, user_id, entry, ok):
    """Record one answer: the log, the ladder step, the transaction trail.

    Returns what `learning_update_result` reported, so the caller can tell that
    the word has just completed the batch (and gone on to FSRS).
    """
    db = get_db_path()
    word_id = entry.get("id")
    remember_last_learning_answer(context, entry, ok)
    log_quiz_event(db, user_id=user_id, word_id=word_id, mode=LEARNING_MODE, is_correct=ok)
    snapshot = _capture_transaction_snapshot(user_id, word_id, [LEARNING_MODE])
    result = learning_update_result(
        db,
        user_id=user_id,
        word_id=word_id,
        ok=ok,
        intervals_seconds=_learning_schedule_seconds(db, user_id),
    )
    _log_transaction(user_id, "learning:answer", snapshot)
    return result
