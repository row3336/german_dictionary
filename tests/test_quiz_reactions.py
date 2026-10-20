"""Spec #9 (part A) — reactions on FSRS-quiz answers + the decorative streak.

Correct answers get a streak-based reaction (1-9 -> thumbs up, 10-19 -> hundred
points, 20+ -> fire); wrong answers get thumbs down. Reactions land on the
USER's answer message (never the bot's own text), only in the four FSRS quizzes
(Lang>De/De>Lang/pl>sg/article). A failed reaction
call must not break the quiz, and "input_err" (typo rollback) lifts the
thumbs-down from the corrected message without setting a new one.
"""

import asyncio
import inspect
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

from bot.app_config import QUIZ_ARTICLE, QUIZ_RU_DE  # noqa: E402
from bot.common.session import (  # noqa: E402
    _bump_answer_streak,
    _reaction_emoji_for_streak,
    _reset_answer_streak,
    _reset_session,
)
from bot.handlers import quiz as Q  # noqa: E402
from bot.storage import init_db, register_or_update_user  # noqa: E402

OWNER, GUEST = 999, 111
THUMBS_UP, THUMBS_DOWN, HUNDRED_POINTS, FIRE = "\U0001F44D", "\U0001F44E", "\U0001F4AF", "\U0001F525"


def _run(coro):
    return asyncio.run(coro)


# --- pure streak/emoji logic ------------------------------------------------

class ReactionEmojiThresholdTest(unittest.TestCase):
    def test_below_10_is_thumbs_up(self):
        for streak in (1, 5, 9):
            self.assertEqual(_reaction_emoji_for_streak(streak), THUMBS_UP)

    def test_10_to_19_is_hundred_points(self):
        for streak in (10, 15, 19):
            self.assertEqual(_reaction_emoji_for_streak(streak), HUNDRED_POINTS)

    def test_20_plus_is_fire(self):
        for streak in (20, 21, 100):
            self.assertEqual(_reaction_emoji_for_streak(streak), FIRE)


class AnswerStreakStateTest(unittest.TestCase):
    def test_bump_increments_on_correct(self):
        ctx = types.SimpleNamespace(user_data={})
        for expected in range(1, 12):
            self.assertEqual(_bump_answer_streak(ctx, True), expected)

    def test_bump_resets_to_zero_on_wrong(self):
        ctx = types.SimpleNamespace(user_data={"quiz_correct_streak": 15})
        self.assertEqual(_bump_answer_streak(ctx, False), 0)
        self.assertEqual(ctx.user_data["quiz_correct_streak"], 0)

    def test_explicit_reset(self):
        ctx = types.SimpleNamespace(user_data={"quiz_correct_streak": 7})
        _reset_answer_streak(ctx)
        self.assertEqual(ctx.user_data["quiz_correct_streak"], 0)

    def test_new_session_resets_streak(self):
        ctx = types.SimpleNamespace(user_data={"quiz_correct_streak": 12})
        _reset_session(ctx, QUIZ_RU_DE)
        self.assertEqual(ctx.user_data["quiz_correct_streak"], 0)

    def test_isolated_per_user_context(self):
        ctx_a = types.SimpleNamespace(user_data={})
        ctx_b = types.SimpleNamespace(user_data={})
        _bump_answer_streak(ctx_a, True)
        _bump_answer_streak(ctx_a, True)
        _bump_answer_streak(ctx_b, True)
        self.assertEqual(ctx_a.user_data["quiz_correct_streak"], 2)
        self.assertEqual(ctx_b.user_data["quiz_correct_streak"], 1)


# --- wiring: _apply_answer_reaction sets the right emoji on the right message --

class _Bot:
    def __init__(self, raise_always=False):
        self.reactions = []  # (chat_id, message_id, reaction)
        self.raise_always = raise_always

    async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
        if self.raise_always:
            raise RuntimeError("Bot API down")
        self.reactions.append((chat_id, message_id, reaction))


class _Msg:
    def __init__(self, text="", message_id=1):
        self.text = text
        self.message_id = message_id
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return types.SimpleNamespace(message_id=self.message_id + 1000)


class _Update:
    def __init__(self, uid, text="", message_id=1):
        self.message = _Msg(text, message_id=message_id)
        self.effective_user = types.SimpleNamespace(id=uid, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=uid)


class _Ctx:
    def __init__(self, user_data=None, bot=None):
        self.user_data = user_data or {}
        self.bot = bot or _Bot()


class ApplyAnswerReactionTest(unittest.TestCase):
    def test_correct_answer_reacts_thumbs_up_on_the_users_message(self):
        update = _Update(GUEST, "der Hund", message_id=42)
        ctx = _Ctx()
        _run(Q._apply_answer_reaction(update, ctx, True))
        self.assertEqual(ctx.bot.reactions, [(GUEST, 42, [THUMBS_UP])])

    def test_wrong_answer_reacts_thumbs_down(self):
        update = _Update(GUEST, "die Katze", message_id=42)
        ctx = _Ctx()
        _run(Q._apply_answer_reaction(update, ctx, False))
        self.assertEqual(ctx.bot.reactions, [(GUEST, 42, [THUMBS_DOWN])])

    def test_streak_crosses_hundred_points_threshold_on_the_message(self):
        ctx = _Ctx(user_data={"quiz_correct_streak": 9})
        update = _Update(GUEST, "x", message_id=7)
        _run(Q._apply_answer_reaction(update, ctx, True))  # 9 -> 10
        self.assertEqual(ctx.bot.reactions, [(GUEST, 7, [HUNDRED_POINTS])])

    def test_wrong_answer_resets_streak_then_next_correct_is_thumbs_up(self):
        ctx = _Ctx(user_data={"quiz_correct_streak": 19})
        _run(Q._apply_answer_reaction(_Update(GUEST, message_id=1), ctx, False))
        _run(Q._apply_answer_reaction(_Update(GUEST, message_id=2), ctx, True))
        self.assertEqual(ctx.bot.reactions[-1], (GUEST, 2, [THUMBS_UP]))

    def test_reaction_failure_does_not_raise(self):
        update = _Update(GUEST, "der Hund", message_id=42)
        ctx = _Ctx(bot=_Bot(raise_always=True))
        # Must not raise out of the answer flow.
        _run(Q._apply_answer_reaction(update, ctx, True))

    def test_no_message_id_is_a_no_op(self):
        update = _Update(GUEST, "der Hund", message_id=None)
        ctx = _Ctx()
        _run(Q._apply_answer_reaction(update, ctx, True))
        self.assertEqual(ctx.bot.reactions, [])


# --- input_err: lift the thumbs-down, never set a new reaction -----------------

class InputErrReactionTest(unittest.TestCase):
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
        from bot.storage import set_user_status
        set_user_status(self.db, GUEST, "approved")
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

    def _ctx_with_wrong_last_answer(self, message_id=99):
        entry = {"id": "w1", "type": "nomen", "german": "Hund", "article": "der", "plural": "Hunde"}
        from bot.written.state import establish_view
        ctx = _Ctx(user_data={"last_answered_entry": entry, "last_answered_mode": "ru_de",
            "last_answered_ok": False, "last_answered_message_id": message_id})
        frame = establish_view(ctx, "ru_de", QUIZ_RU_DE, entry)
        frame['last_receipt'] = Q._update_srs_for_entry(GUEST, entry, "ru_de", False,
            update=_Update(GUEST, "wrong", message_id=message_id), context=ctx, state=QUIZ_RU_DE)
        return ctx

    def test_successful_correction_lifts_the_reaction(self):
        update = _Update(GUEST, "я ошибся")
        ctx = self._ctx_with_wrong_last_answer(message_id=99)
        applied = _run(Q._apply_input_err(update, ctx, "ru_de"))
        self.assertTrue(applied)
        self.assertEqual(ctx.bot.reactions, [(GUEST, 99, [])])

    def test_nothing_to_fix_does_not_touch_reactions(self):
        update = _Update(GUEST, "я ошибся")
        ctx = _Ctx(user_data={})  # no last wrong answer at all
        applied = _run(Q._apply_input_err(update, ctx, "ru_de"))
        self.assertFalse(applied)
        self.assertEqual(ctx.bot.reactions, [])


# --- full handler wiring: one correct / one wrong pass through quiz_ru_de -----

class QuizHandlerReactionWiringTest(unittest.TestCase):
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
        from bot.storage import set_user_status
        set_user_status(self.db, GUEST, "approved")
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

    def test_correct_answer_through_quiz_ru_de_reacts_thumbs_up(self):
        update = _Update(GUEST, "der Hund Hunde", message_id=55)
        ctx = _Ctx(user_data={"quiz_entry": self._entry()})
        _run(Q.quiz_ru_de(update, ctx))
        self.assertIn((GUEST, 55, [THUMBS_UP]), ctx.bot.reactions)
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='form_stats'"
                ).fetchone()
            )

    def test_wrong_answer_through_quiz_ru_de_reacts_thumbs_down(self):
        update = _Update(GUEST, "die Katze Katzen", message_id=56)
        ctx = _Ctx(user_data={"quiz_entry": self._entry()})
        _run(Q.quiz_ru_de(update, ctx))
        self.assertIn((GUEST, 56, [THUMBS_DOWN]), ctx.bot.reactions)

    def test_article_quiz_also_reacts(self):
        entry = {"id": "w1", "type": "nomen", "german": "Hund", "article": "der", "plural": "Hunde"}
        update = _Update(GUEST, "der", message_id=57)
        ctx = _Ctx(user_data={"quiz_entry": entry})
        _run(Q.quiz_article(update, ctx))
        self.assertIn((GUEST, 57, [THUMBS_UP]), ctx.bot.reactions)


class LearningReactionsTest(unittest.TestCase):
    def test_learning_does_react(self):
        # Learning gained 👍/👎 reactions on 2026-07-09 (same contract as FSRS).
        import bot.handlers.learning as L

        self.assertIn("_set_reaction", inspect.getsource(L))


if __name__ == "__main__":
    unittest.main()
