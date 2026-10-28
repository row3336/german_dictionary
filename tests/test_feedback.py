"""P6.3 — general feedback flow: input stored + owner notified; back cancels."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.feedback as F  # noqa: E402
from bot.app_config import SETTINGS_MENU  # noqa: E402
from bot.storage import init_db, list_feedback  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _User:
    def __init__(self, uid, username=None, first_name=""):
        self.id = uid
        self.username = username
        self.first_name = first_name


class _Update:
    def __init__(self, text, user):
        self.message = _Msg(text)
        self.effective_user = user


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _run(coro):
    return asyncio.run(coro)


class FeedbackTest(unittest.TestCase):
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

    def test_feedback_button_placement(self):
        # P6.3: a guest submits feedback; the owner gets the review/feedback submenu
        # (reviews instead of submitting), so no plain feedback-input button.
        flat_owner = [c for row in K._settings_menu_keys(OWNER) for c in row]
        flat_guest = [c for row in K._settings_menu_keys(GUEST) for c in row]
        self.assertIn("feedback", flat_guest)
        self.assertIn("Users", flat_owner)
        self.assertNotIn("feedback", flat_owner)

    def test_input_stores_no_instant_owner_ping(self):
        # spec #5: feedback is persisted, but NOT pushed to the owner instantly
        # (no message, no #feedback tag) — the owner sees it in the digest/inbox.
        upd = _Update("please add dark mode", _User(GUEST, username="bob"))
        bot = _Bot()
        state = _run(F.feedback_input(upd, _Ctx(bot)))
        self.assertEqual(state, SETTINGS_MENU)
        fb = list_feedback(self.db, status="pending")
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]["text"], "please add dark mode")
        # no instant owner notification, no tag
        self.assertFalse(any("#feedback" in m for _cid, m in bot.sent))

    def test_back_cancels_without_saving(self):
        upd = _Update("back", _User(GUEST))
        state = _run(F.feedback_input(upd, _Ctx(_Bot())))
        self.assertEqual(state, SETTINGS_MENU)
        self.assertEqual(list_feedback(self.db, status="pending"), [])


if __name__ == "__main__":
    unittest.main()
