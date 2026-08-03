"""bug #25 — a clarify card opened from the reports inbox exits to the MAIN menu.

The owner reaches the card from Settings > reports (or the digest), not from a
quiz/tests screen, so its back/after-edit exit must land in the main menu instead
of dumping into the unrelated tests menu. `open_clarify_from_report` marks the
card (`clarify_from_report`); `_exit_clarify` consumes the mark and routes home.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:  # some bot modules do `from dotenv import load_dotenv`
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from bot.app_config import ADD_EDIT, MENU, TESTS_MENU  # noqa: E402
from bot.handlers.clarify_fields import _exit_clarify, open_clarify_from_report
from bot.storage import init_db

OWNER = 999


class _Msg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid=OWNER):
        self.message = _Msg()
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": uid})()
        self.callback_query = type("Q", (), {"message": self.message})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _run(coro):
    return asyncio.run(coro)


class ReportCardExitTest(unittest.TestCase):
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

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_report_flag_routes_exit_to_main_menu(self):
        upd, ctx = _Update(), _Ctx()
        ctx.user_data["clarify_from_report"] = True
        state = _run(_exit_clarify(upd, ctx))
        self.assertEqual(state, MENU)
        self.assertNotIn("clarify_from_report", ctx.user_data)  # consumed, not sticky

    def test_no_flag_routes_exit_to_tests(self):
        upd, ctx = _Update(), _Ctx()
        state = _run(_exit_clarify(upd, ctx))
        self.assertEqual(state, TESTS_MENU)

    def test_open_from_report_marks_card(self):
        upd, ctx = _Update(), _Ctx()
        entry = {"id": "w1", "german": "Buch", "type": "nomen", "article": "das"}
        state = _run(open_clarify_from_report(upd, ctx, entry))
        self.assertEqual(state, ADD_EDIT)
        self.assertTrue(ctx.user_data.get("clarify_from_report"))


if __name__ == "__main__":
    unittest.main()
