"""multiuser bug #1 — pl>sg input_err must re-show the current word, not advance.

_repeat_current_pl_sg (mirror of _repeat_current_ru_de) re-renders the current
quiz_entry instead of popping the next word off the FSRS queue.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from _telegram_stub import install

install()

import bot.handlers.quiz as Q  # noqa: E402
from bot.app_config import QUIZ_PL_SG  # noqa: E402
from bot.storage import init_db  # noqa: E402

ENTRY = {"id": "w1", "type": "nomen", "article": "das", "german": "Haus", "plural": "Häuser", "russian": "дом"}


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    def __init__(self):
        self.text = ""
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, uid=7):
        self.message = _FakeMsg()
        self.effective_user = _FakeUser(uid)


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


class PlSgInputErrTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(lambda: os.environ.__setitem__("DB_PATH", self._old) if self._old else os.environ.pop("DB_PATH", None))
        self._real_prompt = Q._send_pl_sg_prompt
        self.addCleanup(lambda: setattr(Q, "_send_pl_sg_prompt", self._real_prompt))

    def test_repeat_keeps_current_word(self):
        picked = []

        async def _spy(update, context, prefix):
            picked.append(1)
            return QUIZ_PL_SG

        Q._send_pl_sg_prompt = _spy
        ctx = _Ctx({"quiz_entry": ENTRY})
        upd = _FakeUpdate()
        original = dict(ENTRY)
        with patch.object(Q, "_pick_due_entry", side_effect=AssertionError("repeat picked a new word")):
            state = asyncio.run(Q._repeat_current_pl_sg(upd, ctx, "✅ fixed"))
        self.assertEqual(state, QUIZ_PL_SG)
        current = ctx.user_data["quiz_entry"]
        self.assertEqual(current["id"], original["id"])
        self.assertEqual({key: current[key] for key in original}, original)
        self.assertEqual(ENTRY, original)  # freezing must not mutate the source entry
        self.assertEqual(picked, [])  # did not pop a new word off the queue
        self.assertTrue(upd.message.replies)  # re-showed the question

    def test_completed_without_entry_does_not_pick(self):
        picked = []

        async def _spy(update, context, prefix):
            picked.append(1)
            return QUIZ_PL_SG

        Q._send_pl_sg_prompt = _spy
        state = asyncio.run(Q._repeat_current_pl_sg(_FakeUpdate(), _Ctx({}), None))
        self.assertEqual(state, QUIZ_PL_SG)
        self.assertEqual(picked, [])  # a completed view must not choose another word


if __name__ == "__main__":
    unittest.main()
