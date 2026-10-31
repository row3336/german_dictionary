"""A wrong Learning answer can be corrected while the next word stays pinned."""

import asyncio
import os
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from _telegram_stub import install

install()

from bot.handlers import learning as L  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.learning_store import get_display  # noqa: E402
from bot.storage import (  # noqa: E402
    _connect, add_word, init_db, learning_get_or_create_batch,
    register_or_update_user, set_user_status,
)

USER_ID = 111


class _Bot:
    async def set_message_reaction(self, *args, **kwargs):
        return None


class _Update:
    next_id = 100

    def __init__(self, text):
        type(self).next_id += 1
        self.update_id = type(self).next_id
        self.message = types.SimpleNamespace(
            text=text, message_id=self.update_id, reply_text=self.reply_text)
        self.effective_user = types.SimpleNamespace(id=USER_ID)
        self.effective_chat = types.SimpleNamespace(id=USER_ID)
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return types.SimpleNamespace(message_id=self.update_id + 1000)


class _Context:
    def __init__(self):
        self.user_data = {}
        self.bot = _Bot()


def _run(coro):
    return asyncio.run(coro)


class LearningInputErrPreservesNextCardTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.db = handle.name
        handle.close()
        self.addCleanup(lambda: os.unlink(self.db))
        self.old = {key: os.environ.get(key) for key in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = "999"
        self.addCleanup(self.restore_env)
        init_db(self.db)
        register_or_update_user(self.db, USER_ID, "guest", "Guest")
        set_user_status(self.db, USER_ID, "approved")
        self.words = []
        for german, russian in (("Apfel", "яблоко"), ("Birne", "груша")):
            self.words.append(add_word(self.db, {
                "type": "nomen", "article": "der", "german": german,
                "plural": "", "russian": russian, "topic": "1",
            }))
        batch = learning_get_or_create_batch(self.db, USER_ID, 2)
        with _connect(self.db) as conn:
            for index, word in enumerate(self.words):
                conn.execute("""INSERT INTO learning_items
                    (user_id,word_id,batch_id,step_index,preview_done,next_due,status,
                     cycle_token,first_due_pending)
                    VALUES (?,?,?,3,1,'2000-01-01T00:00:00Z','active',?,0)""",
                    (USER_ID, word["id"], batch, "cycle-" + str(index)))

    def restore_env(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_wrong_a_then_input_err_keeps_b_without_grading_b(self):
        context = _Context()
        with patch("bot.learning_store.random.choice", side_effect=lambda items: items[0]):
            _run(L._send_learning_prompt(_Update(""), context, None))
            a_id = context.user_data["quiz_entry"]["id"]
            wrong = _Update("zzz-wrong")
            _run(L.learning_quiz(wrong, context))
            pinned = get_display(self.db, USER_ID)
            b_id = pinned["word_id"]
            self.assertNotEqual(a_id, b_id)
            b_action = pinned["action_key"]
            b_revision = pinned["revision"]

            corrected = _Update(t("en", "input_err"))
            _run(L.learning_quiz(corrected, context))
            after = get_display(self.db, USER_ID)
            self.assertEqual(after["word_id"], b_id)
            self.assertEqual(after["action_key"], b_action)
            self.assertGreater(after["revision"], b_revision)
            self.assertEqual(context.user_data["quiz_entry"]["id"], b_id)

            with sqlite3.connect(self.db) as conn:
                event = conn.execute(
                    "SELECT word_id,is_correct FROM quiz_events WHERE user_id=?", (USER_ID,)).fetchall()
                b_step = conn.execute(
                    "SELECT step_index FROM learning_items WHERE user_id=? AND word_id=?",
                    (USER_ID, b_id)).fetchone()[0]
            self.assertEqual(event, [(a_id, 1)])
            self.assertEqual(b_step, 3)

            second = _Update(t("en", "input_err"))
            _run(L.learning_quiz(second, context))
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM quiz_events WHERE user_id=?", (USER_ID,)).fetchone()[0], 1)
            self.assertIn(t("en", "input_err_nothing"), second.calls[-1][0])


if __name__ == "__main__":
    unittest.main()
