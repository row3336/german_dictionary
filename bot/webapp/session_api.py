"""Session lifecycle over HTTP (miniapp.md §7.2).

The routes that open, restore and close a quiz session, plus the one middleware
that turns the registry's refusals into the 409s the client is written against
(§7.7). Questions and answers themselves arrive in M4 — what is here is the frame
they will hang on.

Note what `start` does beyond creating a session: it hands the user over from the
chat. That is the whole point of §4 — the two clients cannot both believe they own
the same queue.
"""

import json
import logging

from aiohttp import web

from .access import API_PREFIX, REQUEST_USER_ID
from .app_keys import BRIDGE_KEY, REGISTRY_KEY
from .overview import MODES
from .sessions import SessionError

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# Modes a session can be opened in. Anything else is a client bug, and echoing it
# back into the session registry would only move the failure somewhere less obvious.
_STARTABLE_MODES = frozenset(MODES)

BAD_REQUEST = "bad_request"
BAD_MODE = "bad_mode"


@web.middleware
async def session_error_middleware(request, handler):
    """Map SessionTakenOver / StaleQuestion onto 409 with their wire codes."""
    try:
        return await handler(request)
    except SessionError as exc:
        logger.info("webapp: %s on %s (%s)", exc.code, request.path, exc)
        return web.json_response({"error": exc.code}, status=409)


async def _json_body(request):
    """Parse a JSON object body, or refuse with 400.

    An empty body is treated as an empty object: several of these endpoints have no
    required field, and demanding `{}` from the client buys nothing.
    """
    raw = await request.text()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": BAD_REQUEST}), content_type="application/json"
        )
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": BAD_REQUEST}), content_type="application/json"
        )
    return body


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
    body = await _json_body(request)
    mode = str(body.get("mode") or "").strip()
    if mode not in _STARTABLE_MODES:
        return web.json_response({"error": BAD_MODE}, status=400)
    word_type = body.get("word_type") or None

    user_id = request[REQUEST_USER_ID]
    session = request.app[REGISTRY_KEY].start(user_id, mode, word_type)
    # Only after the app's session exists: the chat notice says "it continues in
    # the app", and that has to be true by the time it is sent.
    chat_notified = await request.app[BRIDGE_KEY].take_over_for_app(user_id)

    payload = _session_view(session)
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
    """Close the session from the app side."""
    registry = request.app[REGISTRY_KEY]
    user_id = request[REQUEST_USER_ID]
    registry.require(user_id)  # 409 rather than a silent success on a dead session
    registry.stop(user_id)
    return web.json_response({"stopped": True})
