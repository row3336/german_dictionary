"""#3 / #15A (quiz.py slice) — quiz.py used to hardcode English strings
("session words = N", "No due ...", "Fixed previous answer."/"Nothing to fix.",
"Added to learning."/"Nothing to send.") outside of `t(lang, ...)`, so a ru/uk
guest always saw English regardless of their interface language. All of these
now route through i18n.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:  # quiz_ru_de imports bot.main locally, which does `from dotenv import load_dotenv`
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from bot.app_config import QUIZ_ARTICLE, QUIZ_PL_SG, QUIZ_RU_DE  # noqa: E402
from bot.handlers import quiz as Q  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_interface_lang  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text=""):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid, "username": "u", "first_name": "U"})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _run(coro):
    return asyncio.run(coro)


class QuizI18nTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_interface_lang(self.db, GUEST, "ru")
        # A word under the default topic '1' with no history: the topic filter matches
        # words (so it's the 'nothing due' path, not spec #8's 'no words for topic').
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, topic) VALUES ('seed', 'nomen', '1')")
        conn.commit()
        conn.close()

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_i18n_table_has_all_new_keys(self):
        for lang in ("en", "ru", "uk"):
            for key in (
                "session_words_count", "no_due_words", "no_due_pl_sg", "no_due_nouns",
                "input_err_fixed", "input_err_nothing", "added_to_learning", "nothing_to_send",
            ):
                self.assertIn(key, TEXTS[lang], f"{lang}/{key}")
        self.assertEqual(TEXTS["ru"]["session_words_count"], "слов за сессию = {count}")
        self.assertEqual(TEXTS["ru"]["no_due_words"], "Нет слов к повтору. Добавьте ещё или дождитесь срока.")

    def test_ru_de_no_due_words_localized_for_ru_guest(self):
        update = _Update(GUEST)
        state = _run(Q._send_ru_de_prompt(update, _Ctx(), None))
        self.assertEqual(state, QUIZ_RU_DE)
        texts = [c[0] for c in update.message.calls]
        self.assertIn("слов за сессию = 0", texts)
        self.assertIn(TEXTS["ru"]["no_due_words"], texts)
        self.assertFalse(any("No due" in t or "session words" in t for t in texts))

    def test_pl_sg_no_due_localized_for_ru_guest(self):
        update = _Update(GUEST)
        state = _run(Q._send_pl_sg_prompt(update, _Ctx(), None))
        self.assertEqual(state, QUIZ_PL_SG)
        texts = [c[0] for c in update.message.calls]
        self.assertIn(TEXTS["ru"]["no_due_pl_sg"], texts)
        self.assertFalse(any("No due" in t for t in texts))

    def test_article_no_due_localized_for_ru_guest(self):
        update = _Update(GUEST)
        state = _run(Q._send_article_prompt(update, _Ctx(), None))
        self.assertNotEqual(state, QUIZ_ARTICLE)  # falls back to TESTS_MENU when nothing is due
        texts = [c[0] for c in update.message.calls]
        self.assertIn(TEXTS["ru"]["no_due_nouns"], texts)
        self.assertFalse(any("No due" in t for t in texts))

    def test_input_err_nothing_to_fix_localized_for_ru_guest(self):
        update = _Update(GUEST, TEXTS["ru"]["input_err"])
        state = _run(Q.quiz_ru_de(update, _Ctx()))
        self.assertEqual(state, QUIZ_RU_DE)
        first_text = update.message.calls[0][0]
        self.assertEqual(first_text, TEXTS["ru"]["input_err_nothing"])

    def test_send_learn_nothing_to_send_localized_for_ru_guest(self):
        update = _Update(GUEST, TEXTS["ru"]["send_learn"])
        state = _run(Q.quiz_ru_de(update, _Ctx()))
        self.assertEqual(state, QUIZ_RU_DE)
        first_text = update.message.calls[0][0]
        self.assertEqual(first_text, TEXTS["ru"]["nothing_to_send"])


if __name__ == "__main__":
    unittest.main()
