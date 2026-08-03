"""input_err — undo an answer that was a typo, not a miss (business_logic.md §7a).

FSRS and weak quizzes; the learning variant has its own rollback in storage.py.
Kept apart from quiz_core.py / weak_core.py on purpose: everything there is a pure
verdict or a forward scoring step, while this one reaches back into the session
(`context.user_data`) and rewrites what the previous answer already recorded.

Split out of handlers/quiz.py and handlers/weak.py so the Mini App corrects an
answer through the very same rollback as the chat (miniapp.md §5.1). Correcting
differently is worse than not correcting at all: the two clients would disagree
about an interval the user cannot see, and nobody could tell which one is right.

The two rollbacks undo different things because the two modes record different
things — FSRS restores the interval it overwrote, weak un-does one tally in
`form_stats` and, when the word was in the review ladder, the step it just lost.
"""

from collections import namedtuple

from ..app_config import get_db_path
from ..storage import apply_weak_input_err as _rollback_weak_form
from ..storage import (
    get_form_review_stage,
    get_form_state,
    log_quiz_event,
    mark_last_quiz_event_correct,
    set_progress,
    update_progress,
    update_weak_review_state,
)
from .quiz_core import _interval_divisor_for_mode
from .session import (
    _flip_session_result_to_correct,
    _remove_from_retry,
    _retry_since_last,
    _set_retry_since_last,
)
from .txlog import _capture_transaction_snapshot, _log_transaction
from .weak_core import weak_mode

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


def apply_weak_input_err(context, user_id, field):
    """Turn the last wrong weak answer for `field` into a correct one.

    Same three conditions as the FSRS rollback — last answer, this mode, and it has
    to have been wrong — but a drill repeat never reaches `remember_last_weak_answer`
    in the first place, so there is nothing stored to undo one.

    Returns True when a correction was actually applied.
    """
    last_entry = context.user_data.get("last_weak_answered_entry")
    last_field = context.user_data.get("last_weak_answered_field")
    last_ok = context.user_data.get("last_weak_answered_ok")
    if not last_entry or last_field != field or last_ok is None:
        return False
    if last_ok:
        return False

    db = get_db_path()
    word_id = last_entry.get("id")
    mode = weak_mode(field)
    snapshot = _capture_transaction_snapshot(user_id, word_id, [mode])
    if not _rollback_weak_form(db, user_id, word_id, field):
        return False
    state = get_form_state(db, user_id, word_id, field)
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(db, user_id, word_id, field, True)
    else:
        stage_snapshot = get_form_review_stage(db, user_id, word_id, field)
    log_quiz_event(
        db,
        user_id=user_id,
        word_id=word_id,
        mode=mode,
        is_correct=True,
        review_stage_snapshot=stage_snapshot,
    )
    _log_transaction(user_id, f"input_err:{mode}", snapshot)
    context.user_data["last_weak_answered_ok"] = True
    return True
