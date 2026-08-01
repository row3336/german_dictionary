"""The web layer's one door into the running bot (miniapp.md §4.2).

Two things have to happen in the bot's own memory when the app takes a session
over: the bot's quiz state for that user is cleared, and — if a question was
really left hanging in the chat — one message explains where the session went.
Both need the PTB Application, which the web layer otherwise never touches.

Everything is optional. Running `python -m bot.webapp.server` on its own gives a
bridge with no application behind it: every call is a no-op that reports "nothing
happened", so development without a bot stays possible and the callers need no
special case.

What is cleared, and what is not: only the quiz-session keys listed in §4.1. A
half-finished add-word draft, an edit in progress or anything else the user left
in the chat is none of this feature's business and survives untouched.
"""

import logging

from ..common.lang import _user_interface_lang
from ..common.session import (
    _reset_fsrs_cycle_state,
    _reset_retry_state,
    _reset_weak_drill_state,
)
from ..ctx_keys import (
    LAST_ANSWERED_ENTRY,
    LAST_ANSWERED_MODE,
    LAST_ANSWERED_OK,
    LAST_ANSWERED_SRS,
    LAST_LEARNING_ENTRY_ID,
    LAST_LEARNING_OK,
    LAST_LEARNING_PREV_STEP,
    LAST_QUIZ_IDS,
    LAST_WEAK_ANSWERED_ENTRY,
    LAST_WEAK_ANSWERED_FIELD,
    LAST_WEAK_ANSWERED_OK,
    LEARNING_PREVIEW_MODE,
    LEARNING_RECENT_IDS,
    QUIZ_ENTRY,
    QUIZ_IS_RETRY,
    QUIZ_IS_WEAK_DRILL,
    QUIZ_LAST_ANSWERED_ENTRY,
    SESSION_MODE,
)
from .ctx import WebContext
from .i18n_strings import app_strings

logger = logging.getLogger(__name__)

# Quiz-session keys with fixed names (§4.1). Add-flow, edit and settings keys are
# deliberately absent.
_QUIZ_KEYS = (
    QUIZ_ENTRY,
    QUIZ_IS_RETRY,
    QUIZ_IS_WEAK_DRILL,
    QUIZ_LAST_ANSWERED_ENTRY,
    LAST_QUIZ_IDS,
    LAST_ANSWERED_ENTRY,
    LAST_ANSWERED_MODE,
    LAST_ANSWERED_OK,
    LAST_ANSWERED_SRS,
    LAST_WEAK_ANSWERED_ENTRY,
    LAST_WEAK_ANSWERED_FIELD,
    LAST_WEAK_ANSWERED_OK,
    LEARNING_RECENT_IDS,
    LEARNING_PREVIEW_MODE,
    LAST_LEARNING_ENTRY_ID,
    LAST_LEARNING_OK,
    LAST_LEARNING_PREV_STEP,
    SESSION_MODE,
    "quiz_correct_streak",
)

# The rest are built from a mode at runtime (`session_seen_l1_de`, …), so they are
# matched by prefix rather than listed.
_QUIZ_KEY_PREFIXES = ("session_seen_", "session_correct_", "session_wrong_")

# Marks a session actually in progress in the chat — the condition for saying
# anything at all (owner's decision, 2026-08-01: no message when nothing was
# interrupted).
_ACTIVE_QUIZ_MARKERS = (SESSION_MODE, QUIZ_ENTRY)


class BotBridge:
    """Access to the bot's memory and outbox, or a disabled stand-in."""

    def __init__(self, application=None):
        self._application = application

    @property
    def available(self):
        return self._application is not None

    def _user_data(self, user_id):
        """This user's PTB user_data dict, or None.

        Read with .get: indexing PTB's mapping would create an empty record for
        every user who merely opened the app.
        """
        if not self.available:
            return None
        return self._application.user_data.get(user_id)

    def has_active_quiz(self, user_id):
        """Whether the chat currently holds an unanswered quiz session."""
        data = self._user_data(user_id)
        if not data:
            return False
        return any(marker in data for marker in _ACTIVE_QUIZ_MARKERS)

    def clear_quiz_state(self, user_id):
        """Drop the bot's quiz-session state for this user (§4.2 п.1).

        Returns whether there was anything to drop. The bot's own reset helpers do
        the queue work, so the app cannot clear "almost" the same thing the bot
        would.
        """
        data = self._user_data(user_id)
        if not data:
            return False
        before = len(data)
        context = WebContext(data)
        _reset_retry_state(context)
        _reset_weak_drill_state(context)
        _reset_fsrs_cycle_state(context)
        for key in _QUIZ_KEYS:
            data.pop(key, None)
        for key in [k for k in data if k.startswith(_QUIZ_KEY_PREFIXES)]:
            data.pop(key, None)
        return len(data) != before

    async def notify_session_moved(self, user_id):
        """Tell the chat that the session continues in the app. Best effort.

        A user who blocked the bot, or any other send failure, must not turn into a
        failed API request — the session in the app is already live by then.
        """
        if not self.available:
            return False
        text = app_strings(_user_interface_lang(user_id))["chat_session_moved"]
        try:
            await self._application.bot.send_message(chat_id=user_id, text=text)
        except Exception:
            logger.warning("webapp: could not notify user %s about the takeover", user_id)
            return False
        return True

    async def take_over_for_app(self, user_id):
        """Full hand-over of a user from the chat to the app.

        Returns whether a chat notice was sent, which is exactly when a question was
        actually interrupted.
        """
        interrupted = self.has_active_quiz(user_id)
        self.clear_quiz_state(user_id)
        if not interrupted:
            return False
        return await self.notify_session_moved(user_id)
