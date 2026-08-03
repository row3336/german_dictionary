"""The FSRS quiz loop, without aiohttp (miniapp.md §7.2, M4).

What the chat does across four prompt senders and three answer handlers, minus
everything that speaks Telegram: pick the next word, score the answer, keep the
session counters. The HTTP layer above this one only moves JSON around.

Every step here is the bot's own function called with a `WebContext` (§5.2) — the
picker, the retry queue, the verdict, the SRS write and the counters are all
shared code, not a parallel implementation. That is the whole design constraint of
§5.1: two implementations of "was this answer right" drift, and the user is the
one who finds out that the chat counted an answer the app did not.

Directions are the wire names the client speaks (`l1_de`, `de_l1`, `pl_sg`); the
bot's own mode strings depend on the dictionary language and are resolved here.
"""

from collections import namedtuple

from ..app_config import (
    CONFETTI_MESSAGE_EFFECT_ID,
    FIRE_MESSAGE_EFFECT_ID,
    POOP_MESSAGE_EFFECT_ID,
    get_db_path,
)
from ..common.input_err import apply_fsrs_input_err
from ..common.lang import _mode_de_l1, _mode_l1_de, _mode_pl_sg, _user_dict_lang
from ..common.quiz_core import (
    _check_de_l1,
    _check_pl_sg,
    _check_ru_de_strict,
    _update_srs_for_entry,
)
from ..common.session import (
    _bump_answer_streak,
    _bump_session_result,
    _force_take_retry_entry,
    _fsrs_queue_length,
    _mark_session_word,
    _maybe_take_retry_entry,
    _pick_due_entry,
    _register_retry_result,
    _remember_last_answer,
    _reset_session,
    _retry_since_last,
    _session_correct_count,
    _session_count,
    _session_effect_id,
    _session_has_seen,
    _session_wrong_count,
    _set_retry_since_last,
)
from ..storage import apply_user_word_media, update_form_stats
from ..ui.formatting import _is_pl_sg_eligible

L1_DE = "l1_de"
DE_L1 = "de_l1"
PL_SG = "pl_sg"
FSRS_DIRECTIONS = (L1_DE, DE_L1, PL_SG)

# pl>sg asks for the singular behind a plural, so a noun whose two forms coincide
# under "die" cannot be asked at all (_is_pl_sg_eligible). The bot gives the picker
# a bounded number of tries rather than looping until the queue runs dry, and the
# app has to give up at the same point or the two disagree about "no due words"
# (bot/handlers/quiz.py, _send_pl_sg_prompt).
PL_SG_PICK_ATTEMPTS = 30

# How many retry shows the user has been through this session. Purely for the
# summary (§7.4) — the bot never displays it, so there is no counter to share.
RETRIES_SHOWN_KEY = "web_retries_shown"

# The session-end effect as a wire name. Which one is due is decided by the bot's
# own thresholds; only the naming is ours (§7.4).
_EFFECT_NAMES = {
    CONFETTI_MESSAGE_EFFECT_ID: "confetti",
    FIRE_MESSAGE_EFFECT_ID: "fire",
    POOP_MESSAGE_EFFECT_ID: "poop",
}

AnswerResult = namedtuple("AnswerResult", ("entry", "ok", "is_retry", "streak"))


def mode_for(user_id, direction):
    """Wire direction -> the bot's dict-language-dependent mode string."""
    if direction == L1_DE:
        return _mode_l1_de(user_id)
    if direction == DE_L1:
        return _mode_de_l1(user_id)
    if direction == PL_SG:
        return _mode_pl_sg(user_id)
    raise ValueError(f"not an FSRS direction: {direction!r}")


# --- picking the next word ---

def _word_type_for(session):
    """pl>sg is a nouns-only mode; other directions take the session's filter."""
    return "nomen" if session.mode == PL_SG else session.word_type


def _pick_due(session, user_id, mode):
    """The next due word, respecting pl>sg eligibility."""
    ctx = session.ctx
    word_type = _word_type_for(session)
    if session.mode != PL_SG:
        return _pick_due_entry(ctx, user_id, mode, word_type=word_type)
    for _ in range(PL_SG_PICK_ATTEMPTS):
        candidate = _pick_due_entry(ctx, user_id, mode, word_type=word_type)
        if not candidate:
            return None
        if _is_pl_sg_eligible(candidate):
            return candidate
    return None


def next_question(session, user_id):
    """Advance the session to the next word, or None when nothing is left.

    Retry shows come first, then the FSRS queue; when the queue is empty the
    pending retries are drained without waiting for their gap, so the last few
    repeats still happen instead of being dropped at the end of a session. pl>sg is
    the one direction that skips that drain — same as the chat.
    """
    ctx = session.ctx
    mode = mode_for(user_id, session.mode)

    entry = _maybe_take_retry_entry(ctx, mode, user_id)
    if entry:
        ctx.user_data[RETRIES_SHOWN_KEY] = int(ctx.user_data.get(RETRIES_SHOWN_KEY) or 0) + 1
    else:
        ctx.user_data["quiz_is_retry"] = False
        entry = _pick_due(session, user_id, mode)
        if not entry and session.mode != PL_SG:
            entry = _force_take_retry_entry(ctx, mode)
            if entry:
                ctx.user_data[RETRIES_SHOWN_KEY] = int(ctx.user_data.get(RETRIES_SHOWN_KEY) or 0) + 1
    if not entry:
        # Unlike the chat, which just stops sending prompts, the app keeps a live
        # session object around until /session/stop. Leaving the spent word in
        # place would let a late request score it a second time.
        ctx.user_data.pop("quiz_entry", None)
        return None

    entry = apply_user_word_media(get_db_path(), user_id, entry)  # personal media (P4)
    ctx.user_data["quiz_entry"] = entry
    return entry


def begin(session, user_id):
    """Open the counters for a fresh session and return its first question."""
    _reset_session(session.ctx, mode_for(user_id, session.mode))
    session.ctx.user_data[RETRIES_SHOWN_KEY] = 0
    return next_question(session, user_id)


def current_entry(session):
    """The word on screen right now, or None."""
    return session.ctx.user_data.get("quiz_entry")


# --- answering ---

def _verdict(direction, entry, text, user_id):
    """(ok, form_checks) for this direction — the chat's verdict, verbatim."""
    if direction == L1_DE:
        return _check_ru_de_strict(entry, text)
    if direction == DE_L1:
        return _check_de_l1(entry, text, _user_dict_lang(user_id)), {}
    return _check_pl_sg(entry, text), {}


def submit_answer(session, user_id, text, dont_know=False):
    """Score the answer to the question on screen; None when there is none.

    `dont_know` is the app's "I don't know" button: a wrong answer that skips the
    verdict, exactly as the chat treats its own button. The order of the writes
    below matters and mirrors the handlers — a retry show is scored neither into
    SRS nor into the summary, only back into the retry queue.
    """
    ctx = session.ctx
    mode = mode_for(user_id, session.mode)
    entry = ctx.user_data.get("quiz_entry")
    if not entry:
        return None

    is_retry = bool(ctx.user_data.get("quiz_is_retry"))
    ok, form_checks = (False, {}) if dont_know else _verdict(session.mode, entry, text, user_id)
    if not is_retry:
        # Before scoring: the snapshot input_err rolls back to is the SRS state as
        # it was when the question was asked.
        _remember_last_answer(ctx, entry, mode, ok)
        _update_srs_for_entry(user_id, entry, mode, ok)
        if form_checks:
            update_form_stats(get_db_path(), user_id, entry.get("id"), form_checks)
        _mark_session_word(ctx, mode, entry.get("id"))
    ctx.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(ctx, mode, entry.get("id"), ok, is_retry, user_id)
    if not is_retry and ok:
        _set_retry_since_last(ctx, mode, _retry_since_last(ctx, mode) + 1)
    _bump_session_result(ctx, mode, ok, is_retry)
    streak = _bump_answer_streak(ctx, ok)
    return AnswerResult(entry=entry, ok=ok, is_retry=is_retry, streak=streak)


def apply_input_err(session, user_id):
    """Turn the last answer from a typo into a correct one. True when it applied."""
    return apply_fsrs_input_err(session.ctx, user_id, mode_for(user_id, session.mode)).applied


# --- what the client shows around the question ---

def progress(session, user_id):
    """The "12 / 24" counter (§7.4).

    `total_due` is what is known right now — words already answered, plus what is
    left in the FSRS queue, plus the one on screen. It can grow mid-session when
    the queue is refilled from the database, which is honest: the bot's cycle
    refills too, and a counter that only ever shrinks would be a promise the queue
    does not make.
    """
    ctx = session.ctx
    mode = mode_for(user_id, session.mode)
    answered = _session_count(ctx, mode)
    entry = ctx.user_data.get("quiz_entry") or {}
    # A retry show is a word that was already answered and already counted; adding
    # it again would make the total climb every time a word comes back.
    pending = 1 if entry and not _session_has_seen(ctx, mode, entry.get("id")) else 0
    return {"answered": answered, "total_due": answered + _fsrs_queue_length(ctx, mode) + pending}


def summary(session, user_id):
    """The end-of-session tally (§7.4). Same numbers the chat reports."""
    ctx = session.ctx
    mode = mode_for(user_id, session.mode)
    return {
        "answered": _session_count(ctx, mode),
        "correct": _session_correct_count(ctx, mode),
        "wrong": _session_wrong_count(ctx, mode),
        "retries": int(ctx.user_data.get(RETRIES_SHOWN_KEY) or 0),
        "mode": session.mode,
        "effect": _EFFECT_NAMES.get(_session_effect_id(ctx, mode)),
    }
