"""Sprint 13: input_err must not restart an in-session retry budget."""

import asyncio
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv

from bot.app_config import QUIZ_RU_DE  # noqa: E402
from bot.common.session import (  # noqa: E402
    _force_take_retry_entry,
    _register_retry_result,
    _remove_from_retry,
)
from bot.handlers import learning as learning_handler  # noqa: E402
from bot.handlers import quiz as quiz_handler  # noqa: E402
from bot.storage import init_db, register_or_update_user  # noqa: E402


OWNER = 999
USER = 111
WORD_ID = "w1"
RETRY_MODE = "ru_de"


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    def __init__(self):
        self.reactions = []

    async def set_message_reaction(self, chat_id, message_id, reaction, **kwargs):
        self.reactions.append((chat_id, message_id, reaction))


class _Message:
    def __init__(self, text, message_id=10):
        self.text = text
        self.message_id = message_id


class _Update:
    def __init__(self, text):
        self.message = _Message(text)
        self.effective_user = types.SimpleNamespace(id=USER, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=USER)


class _Context:
    def __init__(self, user_data):
        self.user_data = user_data
        self.bot = _Bot()


class RetryInputErrBudgetTest(unittest.TestCase):
    def setUp(self):
        stand = Path('/Users/wf/py/_agent_work/888_german_dictionary/k15_impl_20261023')
        stand.mkdir(parents=True, exist_ok=True)
        self.db = str(Path(tempfile.mkdtemp(prefix='retry-budget-', dir=stand)) / 'synthetic.sqlite')
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO words (id, type, german, article, plural, russian, topic) "
            "VALUES (?, 'nomen', 'Hund', 'der', 'Hunde', 'собака', '1')",
            (WORD_ID,),
        )
        conn.commit()
        conn.close()
        register_or_update_user(self.db, USER, "u", "U")
        from bot.storage import set_user_status
        set_user_status(self.db, USER, "approved")
        self.old_env = {key: os.environ.get(key) for key in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _retry_context(self, count, pending=True):
        entry = {
            "id": WORD_ID,
            "type": "nomen",
            "german": "Hund",
            "article": "der",
            "plural": "Hunde",
            "russian": "собака",
        }
        context = _Context({
            "quiz_entry": entry,
            "quiz_is_retry": True,
            "last_answered_entry": entry,
            "last_answered_mode": RETRY_MODE,
            "last_answered_ok": False,
            "last_answered_srs": {"interval": 1, "repetitions": 0, "ease": 2.5},
            "last_answered_message_id": 9,
            "retry_queue": {RETRY_MODE: [WORD_ID] if pending else []},
            "retry_counts": {RETRY_MODE: {WORD_ID: count}},
        })
        from bot.written.state import establish_view
        frame = establish_view(context, RETRY_MODE, QUIZ_RU_DE, entry)
        frame['last_receipt'] = quiz_handler._update_srs_for_entry(USER, entry, RETRY_MODE, False,
            update=_Update("wrong"), context=context, state=QUIZ_RU_DE)
        return context

    def test_correction_removes_pending_but_preserves_and_continues_count(self):
        context = self._retry_context(count=2)

        applied = _run(quiz_handler._apply_input_err(_Update("input_err"), context, RETRY_MODE))

        self.assertTrue(applied)
        self.assertEqual(context.user_data["retry_queue"][RETRY_MODE], [])
        self.assertEqual(context.user_data["retry_counts"][RETRY_MODE][WORD_ID], 2)

        with patch("bot.common.session._session_retry_iterations", return_value=3):
            _register_retry_result(context, RETRY_MODE, WORD_ID, False, True, USER)
            self.assertEqual(context.user_data["retry_queue"][RETRY_MODE], [WORD_ID])
            entry = _force_take_retry_entry(context, RETRY_MODE)
            self.assertEqual(entry["id"], WORD_ID)
            self.assertEqual(context.user_data["retry_counts"][RETRY_MODE][WORD_ID], 3)
            _register_retry_result(context, RETRY_MODE, WORD_ID, False, True, USER)

        self.assertEqual(context.user_data["retry_queue"][RETRY_MODE], [])
        self.assertNotIn(WORD_ID, context.user_data["retry_counts"][RETRY_MODE])

    def test_correction_does_not_resurrect_an_exhausted_budget(self):
        context = self._retry_context(count=3)

        applied = _run(quiz_handler._apply_input_err(_Update("input_err"), context, RETRY_MODE))

        self.assertTrue(applied)
        self.assertEqual(context.user_data["retry_counts"][RETRY_MODE][WORD_ID], 3)
        with patch("bot.common.session._session_retry_iterations", return_value=3):
            _register_retry_result(context, RETRY_MODE, WORD_ID, False, True, USER)
        self.assertEqual(context.user_data["retry_queue"][RETRY_MODE], [])
        self.assertNotIn(WORD_ID, context.user_data["retry_counts"][RETRY_MODE])

    def test_full_remove_still_clears_queue_and_count(self):
        context = self._retry_context(count=2)

        _remove_from_retry(context, RETRY_MODE, WORD_ID)

        self.assertEqual(context.user_data["retry_queue"][RETRY_MODE], [])
        self.assertNotIn(WORD_ID, context.user_data["retry_counts"][RETRY_MODE])

    def test_send_learn_enqueues_and_preserves_consumed_retry_budget(self):
        context = self._retry_context(count=2)
        update = _Update("send_learn")
        update.update_id = 90001
        with sqlite3.connect(self.db) as conn:
            before = {table: conn.execute("SELECT * FROM " + table).fetchall()
                      for table in ("progress", "quiz_events", "transaction_log")}
        with patch.object(quiz_handler, "_send_written_prompt", new=AsyncMock(return_value=QUIZ_RU_DE)) as prompt:
            result = _run(quiz_handler.quiz_ru_de(update, context))
        prompt.assert_awaited_once()
        self.assertEqual(result, QUIZ_RU_DE)
        self.assertEqual(context.user_data["learning_send_receipt"]["enqueue_result"], "queued")
        self.assertEqual(context.user_data["retry_queue"][RETRY_MODE], [])
        self.assertEqual(context.user_data["retry_counts"][RETRY_MODE][WORD_ID], 2)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT word_id,status,learning_lifecycle FROM learning_requests").fetchall(), [(WORD_ID, "pending", 0)])
            self.assertEqual(conn.execute("SELECT * FROM learning_items").fetchall(), [])
            for table, rows in before.items():
                self.assertEqual(conn.execute("SELECT * FROM " + table).fetchall(), rows)


if __name__ == "__main__":
    unittest.main()
