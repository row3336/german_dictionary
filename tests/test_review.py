"""spec #5 — owner review submenu routes reports/feedback/requests to the inline
inbox engine (the reply-keyboard review flow was removed). Detailed list/card
behavior lives in test_owner_inbox_spec5 / test_access_tag_toggle."""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import OWNER_REVIEW_MENU, SETTINGS_MENU  # noqa: E402
from bot.handlers.review import owner_review_menu_choice  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import add_error_report, add_feedback, init_db, register_or_update_user, set_user_status  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        self.markups.append(reply_markup)


class _Update:
    def __init__(self, text, uid):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _run(coro):
    return asyncio.run(coro)


def _cbs(markup):
    return [b.callback_data for row in getattr(markup, "inline_keyboard", []) for b in row]


class ReviewMenuTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1','nomen','Haus','дом')")
        conn.commit()
        conn.close()
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

    def _choose(self, key, uid=OWNER):
        upd = _Update(t(_lang(uid), key), uid)
        state = _run(owner_review_menu_choice(upd, _Ctx()))
        return upd, state

    def test_reports_opens_inline_list(self):
        add_error_report(self.db, GUEST, "w1", "bad plural", mode="ru_de")
        upd, state = self._choose("review_reports")
        self.assertEqual(state, OWNER_REVIEW_MENU)
        # inline list carries a numbered item button for the report
        self.assertTrue(any(cb.startswith("inb:r:it:") for cb in _cbs(upd.message.markups[-1])))

    def test_feedback_opens_inline_list(self):
        add_feedback(self.db, GUEST, "nice bot")
        upd, state = self._choose("review_feedback")
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertTrue(any(cb.startswith("inb:f:it:") for cb in _cbs(upd.message.markups[-1])))

    def test_requests_opens_pending_list(self):
        register_or_update_user(self.db, 222, "amy", "")  # pending
        upd, state = self._choose("review_requests")
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertTrue(any(cb == "access:item:222" for cb in _cbs(upd.message.markups[-1])))

    def test_back_returns_to_settings(self):
        _, state = self._choose("back")
        self.assertEqual(state, SETTINGS_MENU)

    def test_non_owner_bounced(self):
        upd = _Update(t("en", "review_reports"), GUEST)
        state = _run(owner_review_menu_choice(upd, _Ctx()))
        self.assertEqual(state, SETTINGS_MENU)


def _lang(uid):
    from bot.common.lang import _user_interface_lang
    return _user_interface_lang(uid)


if __name__ == "__main__":
    unittest.main()
