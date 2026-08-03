"""The learning-only routes (miniapp.md §7.3, M6.3).

What a learning session needs beyond answering: walking through the preview
cards, dropping a word from the batch, topping the batch up — plus `send_learn`,
which belongs to the quiz screens of every mode and is the one route here that
touches no session at all.

`next` and `skip` spend a question token exactly as an answer does (§4.4), and for
the same reason: both write, and a double tap would otherwise mark two words
reviewed while the user saw one card. Everything they then do is
`learning_flow` — nothing here decides what the next card is.

The three session routes answer in the shape `/session/answer` uses, `{"next":
…}`, so the client advances through a preview card and through a verdict with the
same code path.
"""

import logging

from aiohttp import web

from ..app_config import get_db_path
from ..common.learning_core import FILL_OK
from . import learning_flow
from .access import API_PREFIX, REQUEST_USER_ID
from .app_keys import REGISTRY_KEY
from .http_json import json_body
from .quiz_api import next_step
from .sessions import StaleQuestion
from .word_repo import get_word_full

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# A session route that is not a learning session: the mode has no preview cards
# and no batch, so there is nothing sensible to do rather than nothing to say.
WRONG_MODE = "wrong_mode"

# `send_learn` was given a word id that is not in the dictionary. Refused rather
# than enqueued: `learning_add_word_by_id` upserts blindly, and a batch holding a
# row that joins to no word would break the preview query for good.
UNKNOWN_WORD = "unknown_word"


def _learning_session(request):
    """This user's live session, once it is confirmed to be a learning one."""
    session = request.app[REGISTRY_KEY].require(request[REQUEST_USER_ID])
    if session.mode != learning_flow.LEARNING:
        return None
    return session


async def _advance(request, action):
    """Shared body of /session/next and /session/skip: spend the token, then move on."""
    body = await json_body(request)
    registry = request.app[REGISTRY_KEY]
    user_id = request[REQUEST_USER_ID]
    session = _learning_session(request)
    if session is None:
        return web.json_response({"error": WRONG_MODE}, status=400)

    registry.consume_question(session, str(body.get("question_token") or ""))
    session.question = None
    if not action(session, user_id):
        # The token was current, so the card the user acted on was the one on
        # screen; if the write still did not apply, the session is out of step with
        # the database and must not be advanced as if it had.
        raise StaleQuestion("no learning card on screen")
    return web.json_response({"next": next_step(registry, session, user_id)})


@routes.post(f"{API_PREFIX}session/next")
async def preview_next(request):
    """"Next" on a preview card: the word is reviewed and joins the running queue."""
    return await _advance(request, learning_flow.preview_next)


@routes.post(f"{API_PREFIX}session/skip")
async def skip(request):
    """"Skip": the word leaves the batch. Works on a preview card and on a question."""
    return await _advance(request, learning_flow.skip)


@routes.post(f"{API_PREFIX}session/send_learn")
async def send_learn(request):
    """Send a word into the learning batch, from a quiz in any mode (§7.2).

    Not a session route: the word travels by id, so the same button works from the
    quiz feedback, and the batch it lands in is the user's own.
    """
    body = await json_body(request)
    user_id = request[REQUEST_USER_ID]
    word_id = str(body.get("word_id") or "").strip()
    if not word_id or not get_word_full(get_db_path(), word_id):
        return web.json_response({"error": UNKNOWN_WORD}, status=400)
    added = learning_flow.send_to_learning(user_id, word_id)
    logger.info("webapp: send_learn %s for %s -> %s", word_id, user_id, added)
    return web.json_response({"added": bool(added)})


@routes.post(f"{API_PREFIX}learning/fill")
async def fill(request):
    """Top the batch up to the daily quota ("add for learning").

    `reason` says why nothing was added when nothing was — a full batch and an
    exhausted dictionary are different situations, and the chat tells them apart
    too.
    """
    result = learning_flow.fill(request[REQUEST_USER_ID])
    payload = {"added": result.added, "active": result.active, "daily": result.daily}
    if result.reason != FILL_OK:
        payload["reason"] = result.reason
    return web.json_response(payload)
