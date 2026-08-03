"""Asking and answering quiz questions over HTTP (miniapp.md §7.2, M4.3 + M5.3).

Three routes and one rule: every question the client is shown was minted here with
a one-shot token, and every answer has to bring that token back (§4.4). Everything
between the token and the database is a flow module — `quiz_flow` for the three
FSRS directions, `weak_flow` for the four drilling modes — and both are the bot's
own loop, so no verdict, no interval and no counter is computed in this file.

The two flows expose the same handful of functions on purpose: the routes pick one
by mode and then stop caring which it is. What still differs is visible in the
payload rather than in the control flow — a weak session marks repeats as drills
instead of retries, and ends without a summary.

The answer response carries the next question with it. That is deliberate: the
cycle in §8.5a is `answer → verdict → tap → next question`, and a second round trip
between the tap and the next word is exactly the delay the app exists to remove.
When the queue runs out, the summary (if the mode has one) comes in the same slot
instead, and the session stays open until /session/stop so the user can read it.
"""

import logging

from aiohttp import web

from ..common.lang import _user_dict_lang
from . import quiz_flow, weak_flow
from .access import API_PREFIX, REQUEST_USER_ID
from .app_keys import REGISTRY_KEY
from .dict_api import card_for
from .http_json import json_body
from .serializers import quiz_feedback, quiz_question
from .sessions import StaleQuestion

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# A mode the app knows how to open but cannot yet run itself (learning, M6).
# Reported as an outcome rather than an error: the session is real, there is simply
# nothing for the app to ask, and the client has a screen for it.
EMPTY_CHAT_ONLY = "chat_only"
EMPTY_NO_DUE = "no_due"

# Which loop drives which mode. A mode that is in neither is not runnable in the
# app yet, and says so.
_FLOWS = dict(
    [(mode, quiz_flow) for mode in quiz_flow.FSRS_DIRECTIONS]
    + [(mode, weak_flow) for mode in weak_flow.WEAK_MODES]
)


def _flow(session):
    """The flow module that runs this session, or None when the app cannot."""
    return _FLOWS.get(session.mode)


def ask_next(registry, session, user_id, advance=True):
    """Move to the next word and describe it, or return None when there is none.

    Stores the serialized question on the session so a WebView that was minimized
    and reopened is handed back the same one (§7.2) — and mints the token here,
    where the question is actually shown, rather than wherever it is asked for.
    """
    flow = _flow(session)
    entry = flow.next_question(session, user_id) if advance else flow.current_entry(session)
    if not entry:
        session.question = None
        session.question_token = ""
        return None
    session.question = quiz_question(
        entry,
        session.mode,
        _user_dict_lang(user_id),
        registry.issue_question(session),
        flow.progress(session, user_id),
        **flow.question_flags(session),
    )
    return session.question


def begin_session(registry, session, user_id):
    """Open the counters and hand out the first question, as /session/start needs it.

    Returns the `{question: …}` / `{empty: …}` half of the start response, so the
    route stays about sessions and this stays about quizzes.
    """
    flow = _flow(session)
    if flow is None:
        return {"empty": EMPTY_CHAT_ONLY}
    flow.begin(session, user_id)
    question = ask_next(registry, session, user_id, advance=False)
    return {"question": question} if question else {"empty": EMPTY_NO_DUE}


def session_summary(session, user_id):
    """The end-of-session tally, for /session/stop and for a drained queue.

    None both for a mode the app cannot run and for weak modes, which have no
    summary at all — the chat does not give one worth showing (bug #30), and the
    app inherits that rather than inventing its own.
    """
    flow = _flow(session)
    if flow is None:
        return None
    return flow.summary(session, user_id)


def _next_step(registry, session, user_id):
    """What follows the verdict: the next question, the summary, or a bare end.

    A weak session that runs out has nothing to report, so it ends with `no_due` —
    the same thing the chat says when it has no weak words left to ask about.
    """
    question = ask_next(registry, session, user_id)
    if question:
        return {"question": question}
    summary = session_summary(session, user_id)
    return {"summary": summary} if summary is not None else {"empty": EMPTY_NO_DUE}


async def _answer(request, dont_know):
    """The shared body of /session/answer and /session/idk.

    "I don't know" is a wrong answer that skips the verdict — the same thing the
    chat's own button does — so the two endpoints differ by one flag and nothing
    else.
    """
    body = await json_body(request)
    registry = request.app[REGISTRY_KEY]
    user_id = request[REQUEST_USER_ID]
    session = registry.require(user_id)
    flow = _flow(session)
    if flow is None:
        return web.json_response({"empty": EMPTY_CHAT_ONLY})

    # Spends the token before anything is written: a double tap or a network retry
    # gets a 409 here rather than a second row in progress and quiz_events (§4.4).
    registry.consume_question(session, str(body.get("question_token") or ""))
    session.question = None

    text = "" if dont_know else str(body.get("text") or "")
    result = flow.submit_answer(session, user_id, text, dont_know=dont_know)
    if result is None:
        # The token matched but the word is gone — only reachable if the session was
        # emptied between the two, and answering nothing must not look like success.
        raise StaleQuestion("no question on screen")

    dict_lang = _user_dict_lang(user_id)
    card = card_for(user_id, result.entry.get("id"), dict_lang)
    return web.json_response(
        {
            "correct": result.ok,
            "feedback": quiz_feedback(result.entry, session.mode, dict_lang, result.streak, card),
            "next": _next_step(registry, session, user_id),
        }
    )


@routes.post(f"{API_PREFIX}session/answer")
async def answer(request):
    """Score a typed answer, then hand over the next question."""
    return await _answer(request, dont_know=False)


@routes.post(f"{API_PREFIX}session/idk")
async def idk(request):
    """"I don't know": scored wrong, answer shown, next question as usual."""
    return await _answer(request, dont_know=True)


@routes.post(f"{API_PREFIX}session/input_err")
async def input_err(request):
    """Take back the last answer as a typo (§7.2, business_logic.md §7a).

    Not guarded by a question token, and it must not be: by the time the user
    presses it the next question has already been issued, while the correction
    applies to the answer before it. The rollback itself is the guard — FSRS and
    weak alike fire once, for the last answer only, and only when it was wrong.
    """
    registry = request.app[REGISTRY_KEY]
    user_id = request[REQUEST_USER_ID]
    session = registry.require(user_id)
    flow = _flow(session)
    if flow is None:
        return web.json_response({"applied": False})
    applied = flow.apply_input_err(session, user_id)
    if applied:
        logger.info("webapp: input_err applied for %s in %s", user_id, session.mode)
    return web.json_response({"applied": applied})
