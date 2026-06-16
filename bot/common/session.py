"""In-session queue mechanics: session tracking, FSRS cycle queue,
session-retry glue, and weak-drill glue.

State lives in context.user_data (string keys kept verbatim for behavior parity;
see bot/ctx_keys.py for the centralized names to migrate to gradually).
"""

import random

from ..app_config import get_db_path
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


def _reset_session(context, mode):
    """Handle reset session."""
    context.user_data["session_mode"] = mode
    context.user_data[_session_key(mode)] = set()
    _reset_sm2_cycle_state(context, mode)


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


# --- FSRS cycle queue ---

def _reset_sm2_cycle_state(context, mode=None):
    """Handle reset sm2 cycle state."""
    bucket = context.user_data.get("sm2_cycle_queue")
    if not isinstance(bucket, dict):
        return
    if mode:
        bucket.pop(mode, None)
        return
    context.user_data.pop("sm2_cycle_queue", None)


def _pick_due_entry(context, user_id, mode, word_type=None):
    """Handle pick due entry."""
    is_sm2_mode = mode in {_mode_l1_de(user_id), _mode_de_l1(user_id), _mode_pl_sg(user_id)}

    def _fetch_due():
        return select_due(
            get_db_path(),
            user_id=user_id,
            mode=mode,
            word_type=word_type,
            topics=_topic_filter_for_query(),
            exclude_correct_today=is_sm2_mode,
        )

    def _build_sm2_queue(entries):
        learning_pool = []
        sm2_pool = []
        for item in entries:
            srs = item.get("srs") or {}
            reps = int(srs.get("repetitions", 0) or 0)
            has_non_learning = bool(item.get("has_non_learning_history"))
            # Fresh = moved from learning and has no non-learning history yet.
            if str(item.get("learning_done_at") or "").strip() and not has_non_learning and reps <= 1:
                learning_pool.append(item)
            else:
                sm2_pool.append(item)
        random.shuffle(learning_pool)
        random.shuffle(sm2_pool)
        mixed = []
        while learning_pool or sm2_pool:
            if learning_pool:
                mixed.append(learning_pool.pop(0))
            if sm2_pool:
                take = random.randint(2, 3)
                for _ in range(take):
                    if not sm2_pool:
                        break
                    mixed.append(sm2_pool.pop(0))
        return mixed

    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    last_id = last_ids.get(mode)
    if is_sm2_mode:
        bucket = context.user_data.setdefault("sm2_cycle_queue", {})
        queue = bucket.get(mode)
        if not queue:
            due = _fetch_due()
            if not due:
                return None
            pool = [w for w in due if w.get("id") != last_id] if len(due) > 1 else due
            if not pool:
                pool = due
            queue = _build_sm2_queue(pool)
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


def _maybe_take_retry_entry(context, mode):
    """Handle maybe take retry entry."""
    queue = _retry_queue(context, mode)
    if not queue:
        return None
    counts = _retry_counts(context, mode)
    gap = _session_retry_gap(get_db_path())
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


def _register_retry_result(context, mode, word_id, ok, was_retry):
    """Handle register retry result."""
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    max_repeats = _session_retry_iterations(get_db_path())
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


def _maybe_take_weak_drill_entry(context, mode, force=False):
    """Handle maybe take weak drill entry."""
    queue = _weak_drill_queue(context, mode)
    if not queue:
        return None
    gap = _weak_drill_gap(get_db_path())
    if not force and _weak_drill_since_last(context, mode) < gap:
        return None
    counts = _weak_drill_counts(context, mode)
    while queue:
        word_id = queue.pop(0)
        entry = get_word_by_id(get_db_path(), word_id)
        if not entry:
            counts.pop(word_id, None)
            continue
        _set_weak_drill_since_last(context, mode, 0)
        context.user_data["quiz_is_weak_drill"] = True
        return entry
    return None


def _track_weak_drill_show(context, mode, word_id):
    """Handle track weak drill show."""
    if not word_id:
        return
    queue = _weak_drill_queue(context, mode)
    counts = _weak_drill_counts(context, mode)
    repeats = _weak_drill_repeats(get_db_path())
    shown = int(counts.get(word_id, 0)) + 1
    if shown >= repeats:
        counts.pop(word_id, None)
        while word_id in queue:
            queue.remove(word_id)
        return
    counts[word_id] = shown
    if word_id not in queue:
        queue.append(word_id)
