"""multiuser bug #5 — the 'no due words' notice must ride in the SAME message as
the session summary (with its effect), not arrive as a second message that breaks
the confetti/fire animation."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.quiz as Q  # noqa: E402
from bot.storage import init_db  # noqa: E402


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))


class _FakeUpdate:
    def __init__(self, uid=7):
        self.message = _FakeMsg()
        self.effective_user = _FakeUser(uid)


class _Ctx:
    def __init__(self):
        self.user_data = {}


class SessionSummaryMergeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(lambda: os.environ.__setitem__("DB_PATH", self._old) if self._old else os.environ.pop("DB_PATH", None))

    def test_tail_folded_into_single_message(self):
        upd = _FakeUpdate()
        asyncio.run(Q._send_session_summary(upd, _Ctx(), "ru_de", tail_text="NO DUE WORDS", reply_markup="KB"))
        self.assertEqual(len(upd.message.calls), 1)  # one message, not two
        text, kwargs = upd.message.calls[0]
        self.assertIn("NO DUE WORDS", text)  # notice folded in
        self.assertEqual(kwargs.get("reply_markup"), "KB")
        self.assertIn("message_effect_id", kwargs)  # effect stays on this message

    def test_no_tail_plain_summary(self):
        upd = _FakeUpdate()
        asyncio.run(Q._send_session_summary(upd, _Ctx(), "ru_de"))
        self.assertEqual(len(upd.message.calls), 1)
        text, kwargs = upd.message.calls[0]
        self.assertNotIn("NO DUE", text)
        self.assertIsNone(kwargs.get("reply_markup"))


if __name__ == "__main__":
    unittest.main()
