"""In-session queue mechanics: session tracking, FSRS cycle queue,
session-retry glue, and weak-drill glue.

State lives in context.user_data (string keys kept verbatim for behavior parity;
see bot/ctx_keys.py for the centralized names to migrate to gradually).
"""

import random

from ..app_config import (
    CONFETTI_MESSAGE_EFFECT_ID,
    FIRE_MESSAGE_EFFECT_ID,
    POOP_MESSAGE_EFFECT_ID,
    SESSION_EFFECT_GOOD_THRESHOLD_PCT,
    get_db_path,
)
from ..common.lang import _mode_de_l1, _mode_l1_de, _mode_pl_sg
from ..common.settings import (
    _session_retry_gap,
    _session_retry_iterations,
    _topic_filter_for_query,
    _weak_drill_gap,
    _weak_drill_repeats,
)
from ..retry_logic import maybe_take_retry_entry, register_retry_result
from ..storage import get_word_by_id, select_due


# --- session tracking ---

def _session_key(mode):
    """Handle session key."""
    return f"session_seen_{mode}"


def _session_correct_key(mode):
    """Handle session correct key."""
    return f"session_correct_{mode}"


def _session_wrong_key(mode):
    """Handle session wrong key."""
    return f"session_wrong_{mode}"


def _reset_session(context, mode):
    """Handle reset session."""
    context.user_data["session_mode"] = mode
    context.user_data[_session_key(mode)] = set()
    context.user_data[_session_correct_key(mode)] = 0
    context.user_data[_session_wrong_key(mode)] = 0
    _reset_fsrs_cycle_state(context, mode)
    _reset_answer_streak(context)


def _ensure_session_mode(context, mode):
    """Handle ensure session mode."""
    if context.user_data.get("session_mode") != mode:
        _reset_session(context, mode)


def _mark_session_word(context, mode, word_id):
    """Handle mark session word."""
    if not word_id:
        return
    _ensure_session_mode(context, mode)
    seen = context.user_data.get(_session_key(mode))
    if isinstance(seen, set):
        seen.add(word_id)


def _session_count(context, mode):
    """Handle session count."""
    seen = context.user_data.get(_session_key(mode))
    if isinstance(seen, set):
        return len(seen)
    return 0


def _bump_session_result(context, mode, ok):
    """Tally one more correct/wrong answer for `mode` this session (spec #9 summary)."""
    _ensure_session_mode(context, mode)
    key = _session_correct_key(mode) if ok else _session_wrong_key(mode)
    context.user_data[key] = int(context.user_data.get(key) or 0) + 1


def _session_correct_count(context, mode):
    """Handle session correct count."""
    return int(context.user_data.get(_session_correct_key(mode)) or 0)


def _session_wrong_count(context, mode):
    """Handle session wrong count."""
    return int(context.user_data.get(_session_wrong_key(mode)) or 0)


def _session_effect_id(context, mode):
    """Message-effect id for the session-end summary (spec #9 part B): a perfect
    session (100% correct) gets confetti, 80-99% gets fire, below 80% gets the
    poop effect. No answers this session at all -> no effect (nothing to judge)."""
    correct = _session_correct_count(context, mode)
    wrong = _session_wrong_count(context, mode)
    total = correct + wrong
    if total == 0:
        return None
    pct = (correct / total) * 100
    if pct >= 100:
        return CONFETTI_MESSAGE_EFFECT_ID
    if pct >= SESSION_EFFECT_GOOD_THRESHOLD_PCT:
        return FIRE_MESSAGE_EFFECT_ID
    return POOP_MESSAGE_EFFECT_ID


# --- Answer-reaction streak (spec #9) ---
# Consecutive-correct-answer counter that only decides which emoji reacts to the
# user's answer message (decorative). Session-scoped, in-memory only (never
# written to the DB); resets on any wrong answer or whenever a new session starts.

def _reset_answer_streak(context):
    """Handle reset answer streak."""
    context.user_data["quiz_correct_streak"] = 0


def _bump_answer_streak(context, ok):
    """Update the consecutive-correct-answer streak; returns the new value."""
    if not ok:
        _reset_answer_streak(context)
        return 0
    streak = int(context.user_data.get("quiz_correct_streak") or 0) + 1
    context.user_data["quiz_correct_streak"] = streak
    return streak


def _reaction_emoji_for_streak(streak):
    """Correct-answer reaction emoji by consecutive-streak threshold (spec #9):
    1-9 -> thumbs up, 10-19 -> hundred points, 20+ -> fire. All three are valid
    Telegram reaction emoji (telegram.constants.ReactionEmoji)."""
    if streak >= 20:
        return "\U0001F525"  # 🔥
    if streak >= 10:
        return "\U0001F4AF"  # 💯
    return "\U0001F44D"  # 👍


# --- FSRS cycle queue ---

def _reset_fsrs_cycle_state(context, mode=None):
    """Handle reset fsrs cycle state."""
    bucket = context.user_data.get("fsrs_cycle_queue")
    if not isinstance(bucket, dict):
        return
    if mode:
        bucket.pop(mode, None)
        return
    context.user_data.pop("fsrs_cycle_queue", None)


def _pick_due_entry(context, user_id, mode, word_type=None):
    """Handle pick due entry."""
    is_fsrs_mode = mode in {_mode_l1_de(user_id), _mode_de_l1(user_id), _mode_pl_sg(user_id)}

    def _fetch_due():
        return select_due(
            get_db_path(),
            user_id=user_id,
            mode=mode,
            word_type=word_type,
            topics=_topic_filter_for_query(user_id),
            exclude_correct_today=is_fsrs_mode,
        )

    def _build_fsrs_queue(entries):
        learning_pool = []
        fsrs_pool = []
        for item in entries:
            srs = item.get("srs") or {}
            reps = int(srs.get("repetitions", 0) or 0)
            has_non_learning = bool(item.get("has_non_learning_history"))
            # Fresh = moved from learning and has no non-learning history yet.
            if str(item.get("learning_done_at") or "").strip() and not has_non_learning and reps <= 1:
                learning_pool.append(item)
            else:
                fsrs_pool.append(item)
        random.shuffle(learning_pool)
        random.shuffle(fsrs_pool)
        mixed = []
        while learning_pool or fsrs_pool:
            if learning_pool:
                mixed.append(learning_pool.pop(0))
            if fsrs_pool:
                take = random.randint(2, 3)
                for _ in range(take):
                    if not fsrs_pool:
                        break
                    mixed.append(fsrs_pool.pop(0))
        return mixed

    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    last_id = last_ids.get(mode)
    if is_fsrs_mode:
        bucket = context.user_data.setdefault("fsrs_cycle_queue", {})
        queue = bucket.get(mode)
        if not queue:
            due = _fetch_due()
            if not due:
                return None
            pool = [w for w in due if w.get("id") != last_id] if len(due) > 1 else due
            if not pool:
                pool = due
            queue = _build_fsrs_queue(pool)
            if not queue:
                return None
            bucket[mode] = queue
        entry = queue.pop(0)
        last_ids[mode] = entry.get("id")
        return entry

    due = _fetch_due()
    if not due:
        return None
    pool = [w for w in due if w.get("id") != last_id] if len(due) > 1 else due
    if not pool:
        pool = due
    entry = random.choice(pool)
    last_ids[mode] = entry.get("id")
    return entry


# --- session-retry glue ---

def _retry_queue(context, mode):
    """Handle retry queue."""
    queues = context.user_data.setdefault("retry_queue", {})
    return queues.setdefault(mode, [])


def _retry_counts(context, mode):
    """Handle retry counts."""
    counts = context.user_data.setdefault("retry_counts", {})
    return counts.setdefault(mode, {})


def _retry_since_last(context, mode):
    """Handle retry since last."""
    counters = context.user_data.setdefault("retry_since_last", {})
    return counters.get(mode, 0)


def _set_retry_since_last(context, mode, value):
    """Handle set retry since last."""
    counters = context.user_data.setdefault("retry_since_last", {})
    counters[mode] = int(value)


def _maybe_take_retry_entry(context, mode, user_id):
    """Handle maybe take retry entry."""
    queue = _retry_queue(context, mode)
    if not queue:
        return None
    counts = _retry_counts(context, mode)
    gap = _session_retry_gap(get_db_path(), user_id)
    since_last = _retry_since_last(context, mode)
    entry, new_since, took = maybe_take_retry_entry(
        queue,
        counts,
        since_last,
        gap,
        lambda word_id: get_word_by_id(get_db_path(), word_id),
    )
    if not entry:
        return None
    context.user_data["quiz_is_retry"] = True
    if took:
        _set_retry_since_last(context, mode, new_since)
    return entry


def _force_take_retry_entry(context, mode):
    """Take the next retry entry ignoring the gap.

    Used to drain pending retries when the FSRS due queue is already empty: the
    remaining ses_rtr_it retries must finish (even one word back-to-back) before
    the session reports "no due words".
    """
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    while queue:
        entry, _new_since, took = maybe_take_retry_entry(
            queue,
            counts,
            0,
            0,
            lambda word_id: get_word_by_id(get_db_path(), word_id),
        )
        if entry:
            context.user_data["quiz_is_retry"] = True
            _set_retry_since_last(context, mode, 0)
            return entry
        # a missing word was popped — keep draining
    return None


def _register_retry_result(context, mode, word_id, ok, was_retry, user_id):
    """Handle register retry result."""
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    max_repeats = _session_retry_iterations(get_db_path(), user_id)
    register_retry_result(queue, counts, word_id, ok, was_retry, max_repeats)
    if not was_retry and not ok:
        _set_retry_since_last(context, mode, 0)


def _remove_from_retry(context, mode, word_id):
    """Handle remove from retry."""
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    if word_id in queue:
        queue[:] = [x for x in queue if x != word_id]
    counts.pop(word_id, None)


def _reset_retry_state(context):
    """Handle reset retry state."""
    context.user_data.pop("retry_queue", None)
    context.user_data.pop("retry_counts", None)
    context.user_data.pop("retry_since_last", None)
    context.user_data.pop("quiz_is_retry", None)


# --- weak-drill glue ---

def _weak_drill_queue(context, mode):
    """Handle weak drill queue."""
    queues = context.user_data.setdefault("weak_drill_queue", {})
    return queues.setdefault(mode, [])


def _weak_drill_counts(context, mode):
    """Handle weak drill counts."""
    counts = context.user_data.setdefault("weak_drill_counts", {})
    return counts.setdefault(mode, {})


def _weak_drill_since_last(context, mode):
    """Handle weak drill since last."""
    counters = context.user_data.setdefault("weak_drill_since_last", {})
    return counters.get(mode, 0)


def _set_weak_drill_since_last(context, mode, value):
    """Handle set weak drill since last."""
    counters = context.user_data.setdefault("weak_drill_since_last", {})
    counters[mode] = int(value)


def _reset_weak_drill_state(context, mode=None):
    """Handle reset weak drill state."""
    if mode:
        for key in ("weak_drill_queue", "weak_drill_counts", "weak_drill_since_last"):
            bucket = context.user_data.get(key)
            if isinstance(bucket, dict):
                bucket.pop(mode, None)
        return
    context.user_data.pop("weak_drill_queue", None)
    context.user_data.pop("weak_drill_counts", None)
    context.user_data.pop("weak_drill_since_last", None)
    context.user_data.pop("quiz_is_weak_drill", None)


def _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=None, force=False):
    """Take the next due drill word for `mode`, never returning `avoid_id` (no two in a row).

    Drill words are queued only after wrong answers (see `_enqueue_weak_drill`). Each serve
    decrements the word's remaining-drills budget; the word leaves the queue when spent.
    Respects `gap` (drill only after `gap` normal shows) unless `force`.
    """
    queue = _weak_drill_queue(context, mode)
    if not queue:
        return None
    if not force and _weak_drill_since_last(context, mode) < _weak_drill_gap(get_db_path(), user_id):
        return None
    counts = _weak_drill_counts(context, mode)
    pos = next((i for i, wid in enumerate(queue) if avoid_id is None or wid != avoid_id), None)
    if pos is None:
        return None
    word_id = queue.pop(pos)
    entry = get_word_by_id(get_db_path(), word_id)
    if not entry:
        counts.pop(word_id, None)
        return _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=avoid_id, force=True)
    remaining = int(counts.get(word_id, 1)) - 1
    if remaining > 0:
        counts[word_id] = remaining
        queue.append(word_id)
    else:
        counts.pop(word_id, None)
    _set_weak_drill_since_last(context, mode, 0)
    context.user_data["quiz_is_weak_drill"] = True
    return entry


def _enqueue_weak_drill(context, mode, word_id, user_id):
    """Queue a word for drilling after a WRONG answer (budget = the repeats setting)."""
    if not word_id:
        return
    repeats = _weak_drill_repeats(get_db_path(), user_id)
    if repeats <= 0:
        return
    queue = _weak_drill_queue(context, mode)
    counts = _weak_drill_counts(context, mode)
    counts[word_id] = int(repeats)
    if word_id not in queue:
        queue.append(word_id)
