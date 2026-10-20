"""Serialize each user's frame transition, commit, and presentation."""

import asyncio
import weakref
from contextlib import asynccontextmanager

_locks = weakref.WeakValueDictionary()


@asynccontextmanager
async def study_lock(user_id):
    """Acquire once at the outer handler; nested helpers must not reacquire."""
    key = (asyncio.get_running_loop(), int(user_id))
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        yield


def invalidate_study_frames(context):
    """Close live correction rights and queues after undo or startup recovery."""
    exact = {"study_frame", "learning_frame", "learning_send_receipt", "quiz_return_point", "quiz_published_view",
             "last_answered_entry", "last_answered_mode", "last_answered_ok",
             "last_answered_srs", "session_mode", "retry_queue", "retry_counts",
             "retry_since_last", "fsrs_cycle_queue", "last_learning_entry_id",
             "last_learning_ok", "last_learning_prev_step", "learning_preview_mode",
             "learning_recent_ids"}
    for key in list(context.user_data):
        if key in exact or key.startswith(("quiz_", "oral_", "conjugation_", "session_seen_",
                                            "session_correct_", "session_wrong_")):
            context.user_data.pop(key, None)
