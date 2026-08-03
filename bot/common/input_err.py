"""input_err — undo an answer that was a typo, not a miss (business_logic.md §7a).

FSRS quizzes only; the weak and learning variants have their own rollbacks in
storage.py. Kept apart from quiz_core.py on purpose: everything there is a pure
verdict or a forward scoring step, while this one reaches back into the session
(`context.user_data`) and rewrites what the previous answer already recorded.

Split out of handlers/quiz.py so the Mini App corrects an answer through the very
same rollback as the chat (miniapp.md §5.1). Correcting differently is worse than
not correcting at all: the two clients would disagree about an interval the user
cannot see, and nobody could tell which one is right.
"""

from collections import namedtuple

from ..app_config import get_db_path
from ..storage import mark_last_quiz_event_correct, set_progress, update_progress
from .quiz_core import _interval_divisor_for_mode
from .session import (
    _flip_session_result_to_correct,
    _remove_from_retry,
    _retry_since_last,
    _set_retry_since_last,
)
from .txlog import _capture_transaction_snapshot, _log_transaction

# `message_id` is what the chat reacted to and now has to un-react; it is None for
# a web session, which has no message to lift a reaction from.
InputErrResult = namedtuple("InputErrResult", ("applied", "message_id"))

NOTHING_TO_FIX = InputErrResult(False, None)


def apply_fsrs_input_err(context, user_id, mode):
    """Turn the last wrong answer in `mode` into a correct one.

    Only the last answer, only a wrong one, and never a retry show — a retry never
    reaches _remember_last_answer, so there is nothing stored to roll back.
    """
    last_entry = context.user_data.get("last_answered_entry")
    last_mode = context.user_data.get("last_answered_mode")
    last_ok = context.user_data.get("last_answered_ok")
    last_srs = context.user_data.get("last_answered_srs")
    last_message_id = context.user_data.get("last_answered_message_id")
    if not last_entry or last_mode != mode or last_ok is None:
        return NOTHING_TO_FIX
    if last_ok:
        return NOTHING_TO_FIX
    if not last_srs:
        return NOTHING_TO_FIX
    snapshot = _capture_transaction_snapshot(user_id, last_entry.get("id"), [mode])
    set_progress(get_db_path(), user_id, last_entry.get("id"), mode, last_srs)
    update_progress(
        get_db_path(),
        user_id,
        last_entry.get("id"),
        mode,
        quality=5,
        interval_divisor=_interval_divisor_for_mode(user_id, mode),
    )
    # Flip the just-logged wrong event to correct IN PLACE (not a new appended
    # event): the daily/weekly first-touch report keys off the first answer, so an
    # appended correct would be ignored — input_err (a typo, not a real miss) must
    # actually count as correct in the reports.
    mark_last_quiz_event_correct(get_db_path(), user_id, last_entry.get("id"), mode)
    # Mirror it in the in-session summary: move this word from wrong to correct.
    _flip_session_result_to_correct(context, mode)
    _log_transaction(user_id, f"input_err:{mode}", snapshot)
    _remove_from_retry(context, mode, last_entry.get("id"))
    _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    context.user_data["last_answered_ok"] = True
    return InputErrResult(True, last_message_id)
