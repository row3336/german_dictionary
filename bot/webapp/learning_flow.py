"""The learning loop, without aiohttp (miniapp.md §7.3, M6).

The third flow module, shaped like `quiz_flow` and `weak_flow` so the HTTP layer
above can drive any of them without knowing which: pick, grade, advance, report
progress. Everything it does is the chat's own function called with a
`WebContext` (§5.2); the show order, the verdict and the ladder writes live in
`common/learning_core.py` and are shared, not re-implemented (§5.1).

What is genuinely new here is the second phase. A learning batch is previewed
before it is quizzed, so a session hands out two kinds of question:

  * **preview** — the word with its forms and its translation, answered by
    "next" or "skip" rather than by typing. There is nothing to grade, so an
    answer arriving on a preview question is refused rather than scored;
  * **quiz** — an ordinary L1→De question, graded exactly as `l1_de` is.

The rest follows the chat:

  * **no retry queue and no drill queue.** A wrong answer sends the word back to
    step 0 of the ladder, and the ladder itself decides when it comes round
    again;
  * **no answer streak.** The chat reacts 👍/👎 flatly here (no escalation), so
    there is no streak to report and the flow sends 0;
  * **no end-of-session summary.** The chat ends a drained queue with a service
    line, not a tally, and the app inherits that — `summary()` returns None and
    `empty_info()` says why the queue is empty instead.
"""

from collections import namedtuple

from ..app_config import get_db_path
from ..common.input_err import apply_learning_input_err
from ..common.learning_core import (
    LEARNING_MODE,
    add_word_to_batch,
    due_entries,
    fill_batch,
    forget_shown_history,
    learning_verdict,
    mark_preview_next,
    note_learning_shown,
    pick_learning_due_entry,
    preview_entry,
    score_learning_answer,
    skip_word,
)
from ..common.session import (
    _mark_session_word,
    _reset_session,
    _session_count,
    _session_has_seen,
)
from ..common.settings import _learning_ready_quorum
from ..common.word_filter import count_words_for_filter
from ..storage import apply_user_word_media, learning_status_snapshot
from .outcomes import EMPTY_NO_DUE, EMPTY_NO_WORDS

LEARNING = LEARNING_MODE

PHASE_PREVIEW = "preview"
PHASE_QUIZ = "quiz"

# `completed` is the word leaving the batch for FSRS on this very answer — the
# thing the chat appends "sent to FSRS" for. `streak` is always 0 (see the module
# docstring); both fields are in the tuple so the HTTP layer can serialize any
# flow's verdict the same way.
AnswerResult = namedtuple("AnswerResult", ("entry", "ok", "is_retry", "streak", "completed"))


# --- picking the next word ---

def _in_preview(session):
    return bool(session.ctx.user_data.get("learning_preview_mode"))


def next_question(session, user_id):
    """Advance to the next word — a preview card or a due question — or None.

    Preview comes first and drains completely: a batch is reviewed before it is
    asked, which is what makes the first quiz round meaningful (its show order is
    the preview order).
    """
    ctx = session.ctx
    entry = preview_entry(user_id)
    is_preview = entry is not None
    if not is_preview:
        entry = pick_learning_due_entry(ctx, user_id)
    if not entry:
        # Unlike the chat, which simply stops sending prompts, the app keeps a live
        # session object until /session/stop. Leaving the spent word in place would
        # let a late request score it a second time.
        ctx.user_data.pop("quiz_entry", None)
        ctx.user_data["learning_preview_mode"] = False
        forget_shown_history(ctx)
        return None

    entry = apply_user_word_media(get_db_path(), user_id, entry)  # personal media (P4)
    ctx.user_data["quiz_entry"] = entry
    ctx.user_data["learning_preview_mode"] = is_preview
    if not is_preview:
        note_learning_shown(ctx, entry.get("id"))
    return entry


def begin(session, user_id):
    """Open the counters for a fresh session and return its first question.

    Drops the spacing window the same way entering the mode does in the chat: it
    describes what this session has shown, and carrying it over would space words
    against shows the user does not remember.
    """
    forget_shown_history(session.ctx)
    _reset_session(session.ctx, LEARNING)
    return next_question(session, user_id)


def current_entry(session):
    """The word on screen right now, or None."""
    return session.ctx.user_data.get("quiz_entry")


def question_flags(session):
    """How the client should mark this question (§7.4).

    Learning has neither retries nor drills; what it does have is the phase, which
    tells the client whether to show an input field or the next/skip pair.
    """
    return {
        "is_retry": False,
        "is_drill": False,
        "phase": PHASE_PREVIEW if _in_preview(session) else PHASE_QUIZ,
    }


# --- the preview phase ---

def preview_next(session, user_id):
    """"Next" on a preview card: the word is reviewed and joins the queue.

    False when the session is not on a preview card — answering the wrong screen
    must not silently mark some other word as reviewed.
    """
    entry = current_entry(session)
    if not entry or not _in_preview(session):
        return False
    return mark_preview_next(user_id, entry.get("id"))


def skip(session, user_id):
    """"Skip": the word leaves the batch. Allowed in both phases, as in the chat."""
    entry = current_entry(session)
    if not entry:
        return False
    return skip_word(user_id, entry.get("id"))


# --- answering ---

def submit_answer(session, user_id, text, dont_know=False):
    """Score the answer to the question on screen; None when there is none.

    Also None on a preview card: there is no verdict to give, and treating a typed
    answer as one would score a word the user has not been asked about yet.
    """
    ctx = session.ctx
    entry = ctx.user_data.get("quiz_entry")
    if not entry or _in_preview(session):
        return None

    ok = learning_verdict(entry, text, dont_know=dont_know)
    result = score_learning_answer(ctx, user_id, entry, ok)
    _mark_session_word(ctx, LEARNING, entry.get("id"))
    return AnswerResult(
        entry=entry,
        ok=ok,
        is_retry=False,
        streak=0,
        completed=bool(result.get("completed")),
    )


def apply_input_err(session, user_id):
    """Turn the last answer from a typo into a correct one. True when it applied."""
    return apply_learning_input_err(session.ctx, user_id)


# --- the batch, from outside a session ---

def fill(user_id):
    """Top the batch up to the daily quota ("add for learning")."""
    return fill_batch(user_id)


def send_to_learning(user_id, word_id):
    """Push one word into the batch, from a quiz in any mode (§7.2)."""
    return add_word_to_batch(user_id, word_id)


# --- what the client shows around the question ---

def progress(session, user_id):
    """The "12 / 24" counter (§7.4).

    Learning has a due date but no due list to count against: a word answered
    right comes back at its next ladder step, so "how many are left" is the words
    still waiting — previews plus due entries — that this session has not touched
    yet, on top of what it already answered. A word that returns for its next step
    is deliberately not counted twice; the counter tracks words worked through, not
    shows.
    """
    ctx = session.ctx
    answered = _session_count(ctx, LEARNING)
    snapshot = _status(user_id)
    pending = int(snapshot.get("preview_pending") or 0)
    pending += sum(1 for entry in due_entries(user_id) if not _session_has_seen(ctx, LEARNING, entry.get("id")))
    return {"answered": answered, "total_due": answered + pending}


def summary(session, user_id):  # noqa: ARG001 - part of the flow interface
    """No summary in learning — the app inherits the chat here (see the docstring)."""
    return None


def empty_info(session, user_id):  # noqa: ARG001 - part of the flow interface
    """Why the queue is empty, for the screen shown in place of a question.

    The chat draws the same distinction (`_learning_empty_text`): an empty topic
    filter is a different problem from "nothing due yet", and only the second one
    is worth waiting out.

    `next_due` is passed through as the stored UTC timestamp rather than as a
    countdown: the chat renders "5m 12s" server-side because it can only send
    text, while the app has a client that can keep the number ticking.
    """
    if count_words_for_filter(user_id) == 0:
        return {"empty": EMPTY_NO_WORDS}
    info = {"empty": EMPTY_NO_DUE}
    next_due = str(_status(user_id).get("next_due") or "").strip()
    if next_due:
        info["next_due"] = next_due
    return info


def _status(user_id):
    """The learning snapshot, with the user's own quorum setting."""
    db = get_db_path()
    return learning_status_snapshot(db, user_id, _learning_ready_quorum(db, user_id))
