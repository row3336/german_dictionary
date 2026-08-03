"""The weak-forms quiz loop, without aiohttp (miniapp.md §7.2, M5).

The counterpart of `quiz_flow` for the four drilling modes — article, plural,
präteritum, partizip2. Same shape, so the HTTP layer above can drive either one
without knowing which: pick, grade, advance, report progress.

Every step is the chat's own function called with a `WebContext` (§5.2). The
picker, the drill queue, the verdict and the writes into `form_stats` all live in
`common/weak_core.py` and are shared, not re-implemented (§5.1).

Three things differ from the FSRS loop, and all three are inherited from the chat
rather than chosen here:

  * **no retry queue — a drill queue.** A wrong answer queues the word for
    in-session repeats; a repeat is practice and scores nothing, so it moves
    neither `form_stats` nor the session's word count;
  * **no answer streak.** The chat reacts to FSRS answers only (spec #9), so a
    weak answer has no streak to report and sends 0;
  * **no end-of-session summary.** The chat has no meaningful one here (bug #30:
    its weak summary counts words but reports "0 correct, 0 wrong"), and the
    owner's call for M5 is that the app inherits the chat rather than growing its
    own. `summary()` returns None; a drained queue ends the session with an empty
    outcome instead.
"""

from collections import namedtuple

from ..app_config import get_db_path
from ..common.input_err import apply_weak_input_err
from ..common.session import _session_count, _session_has_seen, _reset_session, _reset_weak_drill_state
from ..common.weak_core import (
    WEAK_FIELDS,
    WEAK_WORD_TYPES,
    pick_weak_or_drill_entry,
    score_weak_answer,
    weak_due_words,
    weak_mode,
    weak_verdict,
)
from ..storage import apply_user_word_media

WEAK_MODES = tuple(weak_mode(field) for field in WEAK_FIELDS)

WEAK_ARTICLE = weak_mode("article")
WEAK_PLURAL = weak_mode("plural")
WEAK_PRATERITUM = weak_mode("prateritum")
WEAK_PARTIZIP2 = weak_mode("partizip2")

# Weak answers carry no streak (see the module docstring); the field stays in the
# result so the HTTP layer can serialize an FSRS and a weak verdict the same way.
AnswerResult = namedtuple("AnswerResult", ("entry", "ok", "is_drill", "streak"))


def field_for(mode):
    """`weak_plural` -> `plural`. Raises for anything that is not a weak mode."""
    if mode not in WEAK_MODES:
        raise ValueError(f"not a weak mode: {mode!r}")
    return mode[len("weak_"):]


# --- picking the next word ---

def next_question(session, user_id):
    """Advance to the next word — a fresh weak one or a drill repeat — or None.

    The picker decides between the two (never two drills in a row, and only after
    the configured gap), and records which it was; `question_flags` reads it back.
    """
    field = field_for(session.mode)
    entry = pick_weak_or_drill_entry(session.ctx, user_id, field, word_type=WEAK_WORD_TYPES[field])
    if not entry:
        # Unlike the chat, which simply stops sending prompts, the app keeps a live
        # session object until /session/stop. Leaving the spent word in place would
        # let a late request score it a second time.
        session.ctx.user_data.pop("quiz_entry", None)
        return None
    entry = apply_user_word_media(get_db_path(), user_id, entry)  # personal media (P4)
    session.ctx.user_data["quiz_entry"] = entry
    return entry


def begin(session, user_id):
    """Open the counters for a fresh session and return its first question.

    Clears the drill queue the same way entering the mode does in the chat: drills
    are in-session practice, and carrying yesterday's queue into a new session
    would repeat words the user has since answered.
    """
    mode = session.mode
    _reset_weak_drill_state(session.ctx, mode)
    _reset_session(session.ctx, mode)
    return next_question(session, user_id)


def current_entry(session):
    """The word on screen right now, or None."""
    return session.ctx.user_data.get("quiz_entry")


def question_flags(session):
    """How the client should mark this question (§7.4)."""
    return {"is_retry": False, "is_drill": bool(session.ctx.user_data.get("quiz_is_weak_drill"))}


# --- answering ---

def submit_answer(session, user_id, text, dont_know=False):
    """Score the answer to the question on screen; None when there is none."""
    ctx = session.ctx
    field = field_for(session.mode)
    entry = ctx.user_data.get("quiz_entry")
    if not entry:
        return None

    is_drill = bool(ctx.user_data.get("quiz_is_weak_drill"))
    ok = weak_verdict(entry, field, text, dont_know=dont_know)
    score_weak_answer(ctx, user_id, entry, field, ok)
    return AnswerResult(entry=entry, ok=ok, is_drill=is_drill, streak=0)


def apply_input_err(session, user_id):
    """Turn the last answer from a typo into a correct one. True when it applied."""
    return apply_weak_input_err(session.ctx, user_id, field_for(session.mode))


# --- what the client shows around the question ---

def progress(session, user_id):
    """The "12 / 24" counter (§7.4).

    Weak has no due date, so "how many are left" is a live count of the words the
    picker could still hand out, minus the ones already answered this session. It
    can shrink faster than `answered` grows — a word answered right leaves the weak
    list altogether — which is honest: the counter tracks work remaining, not a
    queue length promised at the start.

    Drill repeats are deliberately absent from both numbers: they were counted when
    they were first answered, and counting them again would make the total climb
    every time a word comes back.
    """
    ctx = session.ctx
    mode = session.mode
    field = field_for(mode)
    answered = _session_count(ctx, mode)
    due = weak_due_words(user_id, field, word_type=WEAK_WORD_TYPES[field])
    pending = sum(1 for word in due if not _session_has_seen(ctx, mode, word.get("id")))
    return {"answered": answered, "total_due": answered + pending}


def summary(session, user_id):  # noqa: ARG001 - part of the flow interface
    """No summary in weak modes — the app inherits the chat here (see the docstring)."""
    return None
