"""Spec #9 (part B) — session-end summary: {correct}/{wrong} counts + a message
effect keyed to this session's accuracy. Scheme agreed with the owner
(2026-07-03): 100% correct -> confetti (perfect), 80-99% -> fire (good but not
perfect), below 80% -> poop (rough session); no answers at all this
session -> no effect (nothing to judge).
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
if "dotenv" not in sys.modules:  # quiz_ru_de imports bot.main locally (`from dotenv import load_dotenv`)
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from bot.app_config import (  # noqa: E402
    CONFETTI_MESSAGE_EFFECT_ID,
    FIRE_MESSAGE_EFFECT_ID,
    POOP_MESSAGE_EFFECT_ID,
    QUIZ_RU_DE,
)
from bot.common.session import (  # noqa: E402
    _bump_session_result,
    _reset_session,
    _session_correct_count,
    _session_effect_id,
    _session_wrong_count,
)
from bot.handlers import quiz as Q  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import init_db, register_or_update_user  # noqa: E402

OWNER, GUEST = 999, 111


def _run(coro):
    return asyncio.run(coro)


# --- pure logic: tally + effect-id selection -----------------------------------

class SessionResultTallyTest(unittest.TestCase):
    def test_bump_increments_correct_and_wrong_independently(self):
        ctx = types.SimpleNamespace(user_data={})
        _bump_session_result(ctx, QUIZ_RU_DE, True)
        _bump_session_result(ctx, QUIZ_RU_DE, True)
        _bump_session_result(ctx, QUIZ_RU_DE, False)
        self.assertEqual(_session_correct_count(ctx, QUIZ_RU_DE), 2)
        self.assertEqual(_session_wrong_count(ctx, QUIZ_RU_DE), 1)

    def test_new_session_resets_tallies(self):
        ctx = types.SimpleNamespace(user_data={})
        _bump_session_result(ctx, QUIZ_RU_DE, True)
        _bump_session_result(ctx, QUIZ_RU_DE, False)
        _reset_session(ctx, QUIZ_RU_DE)
        self.assertEqual(_session_correct_count(ctx, QUIZ_RU_DE), 0)
        self.assertEqual(_session_wrong_count(ctx, QUIZ_RU_DE), 0)


class SessionEffectIdTest(unittest.TestCase):
    def _ctx(self, correct, wrong):
        ctx = types.SimpleNamespace(user_data={})
        for _ in range(correct):
            _bump_session_result(ctx, QUIZ_RU_DE, True)
        for _ in range(wrong):
            _bump_session_result(ctx, QUIZ_RU_DE, False)
        return ctx

    def test_no_answers_at_all_means_no_effect(self):
        ctx = self._ctx(0, 0)
        self.assertIsNone(_session_effect_id(ctx, QUIZ_RU_DE))

    def test_perfect_session_is_confetti(self):
        ctx = self._ctx(5, 0)
        self.assertEqual(_session_effect_id(ctx, QUIZ_RU_DE), CONFETTI_MESSAGE_EFFECT_ID)

    def test_exactly_80_percent_is_fire_not_poop(self):
        ctx = self._ctx(8, 2)  # 80% exactly
        self.assertEqual(_session_effect_id(ctx, QUIZ_RU_DE), FIRE_MESSAGE_EFFECT_ID)

    def test_99_percent_is_fire(self):
        ctx = self._ctx(99, 1)
        self.assertEqual(_session_effect_id(ctx, QUIZ_RU_DE), FIRE_MESSAGE_EFFECT_ID)

    def test_below_80_percent_is_poop(self):
        ctx = self._ctx(7, 3)  # 70%
        self.assertEqual(_session_effect_id(ctx, QUIZ_RU_DE), POOP_MESSAGE_EFFECT_ID)

    def test_all_wrong_is_poop(self):
        ctx = self._ctx(0, 3)
        self.assertEqual(_session_effect_id(ctx, QUIZ_RU_DE), POOP_MESSAGE_EFFECT_ID)


# --- i18n: {correct}/{wrong} placeholders exist in all 3 languages --------------

class SessionSummaryI18nTest(unittest.TestCase):
    def test_all_languages_have_correct_and_wrong_placeholders(self):
        for lang in ("en", "ru", "uk"):
            template = TEXTS[lang]["session_words_count"]
            rendered = template.format(count=3, correct=2, wrong=1)
            self.assertIn("2", rendered)
            self.assertIn("1", rendered)
            self.assertIn("3", rendered)


# --- wiring: _send_session_summary / _stop_with_summary through the real flow --

class _Msg:
    def __init__(self, text="", message_id=1):
        self.text = text
        self.message_id = message_id
        self.calls = []  # (text, kwargs)

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return types.SimpleNamespace(message_id=self.message_id + 1000)


class _Update:
    def __init__(self, uid, text="", message_id=1):
        self.message = _Msg(text, message_id=message_id)
        self.effective_user = types.SimpleNamespace(id=uid, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=uid)


class _Bot:
    def __init__(self):
        self.reactions = []

    async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
        self.reactions.append((chat_id, message_id, reaction))


class _Ctx:
    def __init__(self, user_data=None, bot=None):
        self.user_data = user_data or {}
        self.bot = bot or _Bot()


class SessionSummaryWiringTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO words (id, type, german, article, plural, russian, topic) "
            "VALUES ('w1', 'nomen', 'Hund', 'der', 'Hunde', 'собака', '1')"
        )
        conn.commit()
        conn.close()
        register_or_update_user(self.db, GUEST, "g", "G")
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _entry(self):
        return {"id": "w1", "type": "nomen", "german": "Hund", "article": "der", "plural": "Hunde", "srs": {}}

    def _answer(self, ctx, text, message_id):
        update = _Update(GUEST, text, message_id=message_id)
        return _run(Q.quiz_ru_de(update, ctx))

    def test_stop_with_no_answers_has_no_effect_and_zero_counts(self):
        update = _Update(GUEST, "stop")
        ctx = _Ctx()
        _run(Q.quiz_ru_de(update, ctx))
        summary_call = update.message.calls[0]
        self.assertIn("0", summary_call[0])
        self.assertIsNone(summary_call[1].get("message_effect_id"))

    def test_perfect_session_stop_sends_confetti(self):
        ctx = _Ctx(user_data={"quiz_entry": self._entry()})
        self._answer(ctx, "der Hund Hunde", message_id=1)  # correct
        update = _Update(GUEST, "stop", message_id=2)
        _run(Q.quiz_ru_de(update, ctx))
        summary_call = update.message.calls[0]
        self.assertEqual(summary_call[1].get("message_effect_id"), CONFETTI_MESSAGE_EFFECT_ID)

    def test_all_wrong_session_stop_sends_poop(self):
        ctx = _Ctx(user_data={"quiz_entry": self._entry()})
        self._answer(ctx, "die Katze Katzen", message_id=1)  # wrong
        update = _Update(GUEST, "stop", message_id=2)
        _run(Q.quiz_ru_de(update, ctx))
        summary_call = update.message.calls[0]
        self.assertEqual(summary_call[1].get("message_effect_id"), POOP_MESSAGE_EFFECT_ID)

    def test_summary_text_reports_correct_and_wrong_counts(self):
        ctx = _Ctx(user_data={"quiz_entry": self._entry()})
        self._answer(ctx, "der Hund Hunde", message_id=1)  # correct
        ctx.user_data["quiz_entry"] = self._entry()
        self._answer(ctx, "die Katze Katzen", message_id=2)  # wrong
        update = _Update(GUEST, "stop", message_id=3)
        _run(Q.quiz_ru_de(update, ctx))
        summary_text = update.message.calls[0][0]
        # `count` is DISTINCT WORDS seen (both answers are on the same word "w1",
        # so count=1) while correct/wrong tally ANSWERS given (1 and 1).
        # English is this fixture's default interface language.
        self.assertEqual(
            summary_text,
            TEXTS["en"]["session_words_count"].format(count=1, correct=1, wrong=1),
        )


if __name__ == "__main__":
    unittest.main()
