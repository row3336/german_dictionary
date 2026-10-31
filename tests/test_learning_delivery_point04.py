"""A saved Learning answer is never regraded after an uncertain Telegram send."""

import asyncio
import os
import tempfile
import types
import unittest

from bot.handlers.learning import _send_learning_prompt, learning_quiz, learning_resume_or_rescue
from telegram.error import NetworkError
from bot.i18n import t
from bot.learning_store import get_display
from bot.storage import _connect, init_db


class _Bot:
    async def set_message_reaction(self, *args, **kwargs):
        return None


class _Message:
    def __init__(self, text, message_id, uncertain=False, error=False, reply_id=None, chat_id=None):
        self.text = text
        self.message_id = message_id
        self.uncertain = uncertain
        self.error = error
        self.reply_id = reply_id
        self.chat_id = chat_id
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.error:
            raise NetworkError("transport failed")
        if self.uncertain:
            return None
        return types.SimpleNamespace(message_id=self.reply_id or self.message_id + 1000)


class _Update:
    next_id = 100

    def __init__(self, text, uncertain=False, error=False, message_id=None, reply_id=None, chat_id=None):
        type(self).next_id += 1
        self.update_id = type(self).next_id
        self.message = _Message(text, message_id or self.update_id, uncertain, error, reply_id, chat_id)
        self.effective_user = types.SimpleNamespace(id=900001)
        self.effective_chat = types.SimpleNamespace(id=900001)


class _Context:
    def __init__(self):
        self.user_data = {}
        self.bot = _Bot()


class LearningDeliveryTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.db = handle.name
        handle.close()
        self.addCleanup(lambda: os.unlink(self.db))
        old = {key: os.environ.get(key) for key in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = "999"
        self.addCleanup(self._restore, old)
        init_db(self.db)
        with _connect(self.db) as conn:
            conn.execute("INSERT INTO users(user_id,status) VALUES(900001,'approved')")
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES('batch',900001)")
            for word in ("A", "B"):
                conn.execute("INSERT INTO words(id,type,german,russian) VALUES(?,'nomen',?,'word')",
                             (word, word))
                conn.execute("""INSERT INTO learning_items
                    (user_id,word_id,batch_id,step_index,preview_done,next_due,status,
                     cycle_token,first_due_pending)
                    VALUES (900001,?,'batch',3,1,'2000-01-01T00:00:00Z','active',?,0)""",
                    (word, "cycle-" + word))

    @staticmethod
    def _restore(old):
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def event_count(self):
        with _connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM quiz_events WHERE user_id=900001").fetchone()[0]

    def test_restart_after_answer_and_uncertain_next_send(self):
        context = _Context()
        asyncio.run(_send_learning_prompt(_Update(""), context, None))
        first = get_display(self.db, 900001)
        self.assertEqual(first["send_state"], "confirmed")

        answer = _Update("wrong", uncertain=True)
        asyncio.run(learning_quiz(answer, context))
        waiting = get_display(self.db, 900001)
        self.assertEqual(waiting["send_state"], "uncertain")
        self.assertEqual(self.event_count(), 1)

        restarted = _Context()
        recovery = _Update("another wrong")
        asyncio.run(learning_resume_or_rescue(recovery, restarted))
        shown = get_display(self.db, 900001)
        self.assertEqual(shown["action_key"], waiting["action_key"])
        self.assertEqual(shown["send_state"], "confirmed")
        self.assertEqual(self.event_count(), 1)
        self.assertTrue(recovery.message.calls)

        next_answer = _Update("wrong again")
        asyncio.run(learning_quiz(next_answer, restarted))
        self.assertEqual(self.event_count(), 2)
        # A redelivery of the first accepted Update cannot score the new question.
        asyncio.run(learning_quiz(answer, restarted))
        self.assertEqual(self.event_count(), 2)

    def test_confirmed_question_can_be_answered_once_after_restart(self):
        context = _Context()
        asyncio.run(_send_learning_prompt(_Update(""), context, None))
        shown = get_display(self.db, 900001)
        self.assertEqual(shown["send_state"], "confirmed")
        restarted = _Context()
        answer = _Update("wrong")
        asyncio.run(learning_resume_or_rescue(answer, restarted))
        self.assertEqual(self.event_count(), 1)
        asyncio.run(learning_quiz(answer, restarted))
        self.assertEqual(self.event_count(), 1)


    def test_network_failure_keeps_next_question_for_restart(self):
        context = _Context()
        asyncio.run(_send_learning_prompt(_Update(""), context, None))
        answer = _Update("wrong", error=True)
        asyncio.run(learning_quiz(answer, context))
        pending = get_display(self.db, 900001)
        self.assertEqual(pending["send_state"], "uncertain")
        self.assertEqual(self.event_count(), 1)
        restarted = _Context()
        asyncio.run(learning_resume_or_rescue(_Update("continue"), restarted))
        self.assertEqual(get_display(self.db, 900001)["action_key"], pending["action_key"])
        self.assertEqual(get_display(self.db, 900001)["send_state"], "confirmed")
        self.assertEqual(self.event_count(), 1)

    def test_waiting_refreshes_when_word_becomes_due_without_grading_input(self):
        with _connect(self.db) as conn:
            conn.execute("UPDATE learning_items SET next_due='2999-01-01T00:00:00Z'")
        context = _Context()
        asyncio.run(_send_learning_prompt(_Update(""), context, None))
        self.assertEqual(get_display(self.db, 900001)["phase"], "waiting")
        with _connect(self.db) as conn:
            conn.execute("UPDATE learning_items SET next_due='2000-01-01T00:00:00Z'")
        asyncio.run(learning_quiz(_Update("continue"), context))
        self.assertEqual(get_display(self.db, 900001)["phase"], "question")
        self.assertEqual(self.event_count(), 0)

    def test_queued_second_answer_before_new_prompt_is_not_graded(self):
        context = _Context()
        asyncio.run(_send_learning_prompt(
            _Update("", message_id=10, reply_id=11, chat_id=900001), context, None))
        first = get_display(self.db, 900001)
        self.assertEqual(first["telegram_message_id"], 11)
        asyncio.run(learning_quiz(
            _Update("wrong", message_id=12, reply_id=14, chat_id=900001), context))
        next_question = get_display(self.db, 900001)
        self.assertEqual(next_question["telegram_message_id"], 14)
        self.assertEqual(self.event_count(), 1)
        asyncio.run(learning_quiz(
            _Update("wrong", message_id=13, reply_id=15, chat_id=900001), context))
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(get_display(self.db, 900001)["action_key"], next_question["action_key"])
        asyncio.run(learning_quiz(
            _Update("wrong", message_id=16, reply_id=17, chat_id=900001), context))
        self.assertEqual(self.event_count(), 2)


    def test_stale_clarify_button_on_waiting_does_not_suspend_plan(self):
        with _connect(self.db) as conn:
            conn.execute("UPDATE learning_items SET next_due='2999-01-01T00:00:00Z'")
        context = _Context()
        asyncio.run(_send_learning_prompt(_Update(""), context, None))
        self.assertEqual(get_display(self.db, 900001)["phase"], "waiting")
        asyncio.run(learning_quiz(_Update(t("en", "clarify_fields")), context))
        plan = get_display(self.db, 900001)
        self.assertEqual(plan["phase"], "waiting")
        self.assertEqual(plan["suspended_for_clarify"], 0)


if __name__ == "__main__":
    unittest.main()
