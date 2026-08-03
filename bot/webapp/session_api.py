"""Session lifecycle over HTTP (miniapp.md §7.2).

The routes that open, restore and close a quiz session, plus the one middleware
that turns the registry's refusals into the 409s the client is written against
(§7.7). Scoring an answer lives next door in quiz_api.py; what these three do is
open the session, hand back the question it is on, and close it with a summary.

Note what `start` does beyond creating a session: it hands the user over from the
chat. That is the whole point of §4 — the two clients cannot both believe they own
the same queue.
"""

import logging

from aiohttp import web

from .access import API_PREFIX, REQUEST_USER_ID
from .app_keys import BRIDGE_KEY, REGISTRY_KEY
from .http_json import json_body
from .overview import MODES
from .quiz_api import begin_session, session_summary
from .sessions import SessionError

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# Modes a session can be opened in. Anything else is a client bug, and echoing it
# back into the session registry would only move the failure somewhere less obvious.
_STARTABLE_MODES = frozenset(MODES)

BAD_MODE = "bad_mode"


@web.middleware
async def session_error_middleware(request, handler):
    """Map SessionTakenOver / StaleQuestion onto 409 with their wire codes."""
    try:
        return await handler(request)
    except SessionError as exc:
        logger.info("webapp: %s on %s (%s)", exc.code, request.path, exc)
        return web.json_response({"error": exc.code}, status=409)


def _session_view(session):
    """What the client is told about its own session."""
    return {
        "session_id": session.session_id,
        "mode": session.mode,
        "word_type": session.word_type,
        "question": session.question,
    }


@routes.post(f"{API_PREFIX}session/start")
async def start_session(request):
    """Open a session in the app, taking it away from the chat (§4.2 п.1)."""
    body = await json_body(request)
    mode = str(body.get("mode") or "").strip()
    if mode not in _STARTABLE_MODES:
        return web.json_response({"error": BAD_MODE}, status=400)
    word_type = body.get("word_type") or None

    user_id = request[REQUEST_USER_ID]
    registry = request.app[REGISTRY_KEY]
    session = registry.start(user_id, mode, word_type)
    # Only after the app's session exists: the chat notice says "it continues in
    # the app", and that has to be true by the time it is sent.
    chat_notified = await request.app[BRIDGE_KEY].take_over_for_app(user_id)

    # The first question comes with the session: opening a quiz is one round trip,
    # not two, and an empty queue is answered before the user is shown an input
    # field they have nothing to type into.
    payload = {**_session_view(session), **begin_session(registry, session, user_id)}
    payload["chat_notified"] = chat_notified
    return web.json_response(payload)


@routes.get(f"{API_PREFIX}session/question")
async def current_question(request):
    """The question the user is on, for a WebView that was minimized and reopened.

    Deliberately does not mint a new token: coming back to the app is not answering,
    and a fresh question here would silently skip the word that was on screen.
    """
    session = request.app[REGISTRY_KEY].require(request[REQUEST_USER_ID])
    return web.json_response(_session_view(session))


@routes.post(f"{API_PREFIX}session/stop")
async def stop_session(request):
    """Close the session from the app side, and report what it added up to (§7.4)."""
    registry = request.app[REGISTRY_KEY]
    user_id = request[REQUEST_USER_ID]
    session = registry.require(user_id)  # 409 rather than a silent success on a dead session
    # Read before the session is dropped: the counters live in its own context, and
    # there is nothing left to count once the registry lets go of it.
    summary = session_summary(session, user_id)
    registry.stop(user_id)
    payload = {"stopped": True}
    if summary is not None:
        payload["summary"] = summary
    return web.json_response(payload)
