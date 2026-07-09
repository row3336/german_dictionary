"""Learning reactions (👍/👎) + end-of-session closing message.

* Learning answers get a flat 👍 (correct) / 👎 (wrong) reaction on the user's
  message (same contract as FSRS quizzes, no streak escalation); the answer
  message id is remembered so input_err can lift the 👎.
* When the whole session clears into FSRS, the trailing feedback and the
  "queue empty" line are merged into ONE closing message carrying the 🔥 effect
  (FSRS-style), so nothing follows to knock the effect off. When words are still
  pending (not due yet), no effect and the next_due readout is shown instead.
"""
import asyncio
import os
import sqlite3
import tempfile
import types
import unittest
import warnings

from bot.app_config import FIRE_MESSAGE_EFFECT_ID
from bot.handlers.learning import _apply_learning_reaction, _send_learning_prompt
from bot.storage import (
    add_word,
    init_db,
    learning_add_words,
    learning_get_or_create_batch,
    register_or_update_user,
)

warnings.filterwarnings("ignore", category=ResourceWarning)

GUEST = 111
THUMBS_UP, THUMBS_DOWN = "\U0001F44D", "\U0001F44E"


class _Bot:
    def __init__(self, raise_always=False):
        self.reactions = []
        self.raise_always = raise_always

    async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
        if self.raise_always:
            raise RuntimeError("Bot API down")
        self.reactions.append((chat_id, message_id, reaction))


class _Msg:
    def __init__(self, message_id=1):
        self.message_id = message_id
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return types.SimpleNamespace(message_id=self.message_id + 1000)


class _Update:
    def __init__(self, uid, message_id=1):
        self.message = _Msg(message_id)
        self.effective_user = types.SimpleNamespace(id=uid, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=uid)


class _Ctx:
    def __init__(self, user_data=None, bot=None):
        self.user_data = user_data or {}
        self.bot = bot or _Bot()


def _run(coro):
    return asyncio.run(coro)


class LearningAnswerReactionTest(unittest.TestCase):
    def test_correct_answer_reacts_thumbs_up_and_remembers_message(self):
        update = _Update(GUEST, message_id=42)
        ctx = _Ctx()
        _run(_apply_learning_reaction(update, ctx, True))
        self.assertEqual(ctx.bot.reactions, [(GUEST, 42, [THUMBS_UP])])
        self.assertEqual(ctx.user_data["last_learning_answer_message_id"], 42)

    def test_wrong_answer_reacts_thumbs_down(self):
        update = _Update(GUEST, message_id=43)
        ctx = _Ctx()
        _run(_apply_learning_reaction(update, ctx, False))
        self.assertEqual(ctx.bot.reactions, [(GUEST, 43, [THUMBS_DOWN])])

    def test_reaction_failure_does_not_raise(self):
        update = _Update(GUEST, message_id=44)
        ctx = _Ctx(bot=_Bot(raise_always=True))
        _run(_apply_learning_reaction(update, ctx, True))  # must not raise


class LearningEndOfSessionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        register_or_update_user(self.db, GUEST, "g", "G")
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = "999"
        self.addCleanup(self._restore)
        self._word = add_word(
            self.db,
            {"id": "unique", "type": "nomen", "article": "der", "german": "Ingenieur",
             "plural": "Ingenieure", "russian": "инженер", "ukrainian": "інженер", "english": "engineer", "topic": "1"},
        )

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _enqueue(self, status, next_due):
        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=10)
        learning_add_words(self.db, user_id=GUEST, words=[self._word], batch_id=batch_id, first_delay_seconds=0)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE learning_items SET status=?, preview_done=1, next_due=? WHERE user_id=? AND word_id=?",
                (status, next_due, GUEST, self._word["id"]),
            )

    def test_session_cleared_merges_feedback_and_fires(self):
        # one word, now completed -> active_count 0, completed_count 1 -> fire + merge
        self._enqueue(status="completed", next_due="2000-01-01T00:00:00Z")
        update = _Update(GUEST)
        ctx = _Ctx()
        _run(_send_learning_prompt(update, ctx, "feedback ✅ Learning done ✅, sent to fsrs"))
        self.assertEqual(len(update.message.calls), 1)  # merged into ONE message
        text, kw = update.message.calls[0]
        self.assertIn("feedback ✅", text)                # trailing feedback merged in
        self.assertNotIn("next_due", text)                # no pending readout on completion
        self.assertEqual(kw.get("message_effect_id"), FIRE_MESSAGE_EFFECT_ID)

    def test_words_still_pending_no_fire_shows_next_due(self):
        # one word still active but not due yet -> come-back-later, no effect
        self._enqueue(status="active", next_due="2099-01-01T00:00:00Z")
        update = _Update(GUEST)
        ctx = _Ctx()
        _run(_send_learning_prompt(update, ctx, "feedback"))
        self.assertEqual(len(update.message.calls), 1)
        text, kw = update.message.calls[0]
        self.assertIn("next_due", text)
        self.assertNotIn("message_effect_id", kw)


if __name__ == "__main__":
    unittest.main()
