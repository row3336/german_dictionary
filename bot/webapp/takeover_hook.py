"""Reverse takeover: an answer in the chat wins the session back (miniapp.md §4.2 п.2).

There is no button for this. The user simply goes back to the conversation and
types, and that act alone hands the session from the app to the bot — which is the
only behaviour that matches how people actually switch between a chat and an app.

Implemented as a callback for a PTB handler in an earlier group, installed at
startup by bot/webapp/integration.py. Two consequences of that choice:

  * bot/handlers/quiz.py is not modified — the bot's own flow is untouched, and
    this branch keeps its hands off the files feature/multiuser also edits;
  * the callback must never swallow an update or raise. It reads a dict, flips a
    flag, and gets out of the way; the update then travels on to the handler that
    was always going to serve it.

Kept free of telegram imports so it can be tested without PTB; the handler object
itself is built where the wiring happens.
"""

import logging

from .sessions import CLIENT_APP

logger = logging.getLogger(__name__)

# Runs ahead of the bot's own handlers. Nothing here consumes the update, so the
# conversation continues exactly as before.
TAKEOVER_HANDLER_GROUP = -1


def build_takeover_callback(registry):
    """PTB callback that hands the session back to the chat when it is in the app."""

    async def on_update(update, context):
        try:
            user = getattr(update, "effective_user", None)
            user_id = getattr(user, "id", None)
            if user_id is None:
                return
            # Only act when there is something to take over. Touching the registry
            # for every update would also mean a record per user who ever wrote to
            # the bot, kept for the life of the process.
            if registry.active_client(user_id) != CLIENT_APP:
                return
            registry.claim_for_bot(user_id)
            logger.info("webapp: session for %s moved back to the chat", user_id)
        except Exception:
            # A failure here must not break the user's message; the worst case is
            # that the app keeps a session it should have lost, and the next
            # takeover fixes it.
            logger.exception("webapp: takeover hook failed")

    return on_update
