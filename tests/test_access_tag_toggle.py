"""spec #5 — owner pending-access requests: digest + inline [show requests] flow.

Replaces the old #user_confirmation_request search-tag toggle (removed). Two
concerns pinned here:
  * the reminder digest lists every pending request (username + id) and carries a
    [show requests] button (no hashtag);
  * that button opens an inline, actionable list -> user card -> approve/reject,
    owner-only, editing the same message in place.

telegram is stubbed; inline-keyboard classes are capturing fakes bound onto the
access module so callback_data is inspectable regardless of import order.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.access as A  # noqa: E402
import bot.jobs.access_reminders as AR  # noqa: E402
from bot.storage import get_user_status, init_db, register_or_update_user, set_user_status  # noqa: E402

OWNER, GUEST = 999, 111


class _FakeButton:
    def __init__(self, text, callback_data=None, **k):
        self.text = text
        self.callback_data = callback_data


class _FakeMarkup:
    def __init__(self, inline_keyboard, **k):
        self.inline_keyboard = inline_keyboard


A.InlineKeyboardButton = _FakeButton
A.InlineKeyboardMarkup = _FakeMarkup


class _FakeQuery:
    def __init__(self, data, uid, text=""):
        self.data = data
        self.message = type("M", (), {"text": text})()
        self.from_user = type("U", (), {"id": uid})()
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.message.text = text
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append((None, reply_markup))


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))


class _Ctx:
    def __init__(self, bot=None):
        self.bot = bot or _FakeBot()


def _flat_cb(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
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


class ShowRequestsFlowTest(_Base):
    def test_button_opens_list(self):
        btn = A.pending_requests_button(A._owner_lang()).inline_keyboard[0][0]
        self.assertEqual(btn.callback_data, A.ACCESS_LIST)

    def test_list_shows_pending_as_item_buttons(self):
        register_or_update_user(self.db, 111, "bob", "")
        register_or_update_user(self.db, 222, "amy", "")
        register_or_update_user(self.db, 333, "appr", "")
        set_user_status(self.db, 333, "approved")  # not pending -> not listed
        q = _FakeQuery(A.ACCESS_LIST, OWNER)
        _run(A.access_decision_callback(type("U", (), {"callback_query": q})(), _Ctx()))
        cbs = _flat_cb(q.edits[-1][1])
        self.assertIn(f"{A.ACCESS_CALLBACK_PREFIX}:item:111", cbs)
        self.assertIn(f"{A.ACCESS_CALLBACK_PREFIX}:item:222", cbs)
        self.assertNotIn(f"{A.ACCESS_CALLBACK_PREFIX}:item:333", cbs)

    def test_empty_list_message(self):
        q = _FakeQuery(A.ACCESS_LIST, OWNER)
        _run(A.access_decision_callback(type("U", (), {"callback_query": q})(), _Ctx()))
        self.assertEqual(q.edits[-1][0], A.t(A._owner_lang(), "access_no_pending"))

    def test_item_card_has_approve_reject_back(self):
        register_or_update_user(self.db, 111, "bob", "")
        q = _FakeQuery(f"{A.ACCESS_CALLBACK_PREFIX}:item:111", OWNER)
        _run(A.access_decision_callback(type("U", (), {"callback_query": q})(), _Ctx()))
        cbs = _flat_cb(q.edits[-1][1])
        self.assertIn(f"{A.ACCESS_CALLBACK_PREFIX}:approve:111", cbs)
        self.assertIn(f"{A.ACCESS_CALLBACK_PREFIX}:reject:111", cbs)
        self.assertIn(A.ACCESS_LIST, cbs)  # back to list

    def test_approve_from_card_sets_status_and_notifies(self):
        register_or_update_user(self.db, 111, "bob", "")
        bot = _FakeBot()
        q = _FakeQuery(f"{A.ACCESS_CALLBACK_PREFIX}:approve:111", OWNER)
        _run(A.access_decision_callback(type("U", (), {"callback_query": q})(), _Ctx(bot)))
        self.assertEqual(get_user_status(self.db, 111), "approved")
        self.assertTrue(any(cid == 111 for cid, _t, _m in bot.sent))  # guest notified

    def test_non_owner_denied(self):
        register_or_update_user(self.db, 111, "bob", "")
        q = _FakeQuery(A.ACCESS_LIST, GUEST)
        _run(A.access_decision_callback(type("U", (), {"callback_query": q})(), _Ctx()))
        self.assertEqual(q.edits, [])  # nothing shown to a non-owner

    def test_no_confirmation_tag_in_outgoing(self):
        # spec #5: no #user_confirmation_request / _done tags anywhere.
        register_or_update_user(self.db, 111, "bob", "")
        q = _FakeQuery(f"{A.ACCESS_CALLBACK_PREFIX}:approve:111", OWNER)
        _run(A.access_decision_callback(type("U", (), {"callback_query": q})(), _Ctx()))
        self.assertNotIn("#user_confirmation", q.edits[-1][0])


class PendingDigestTest(_Base):
    def test_digest_lists_all_pending_with_show_button(self):
        register_or_update_user(self.db, 6941289070, "work_cn", "")
        register_or_update_user(self.db, 628411953, "M0i09", "")
        register_or_update_user(self.db, 222, "approved_guy", "")
        set_user_status(self.db, 222, "approved")
        bot = _FakeBot()
        _run(AR._access_pending_reminder_job(_Ctx(bot)))
        self.assertEqual(len(bot.sent), 1)
        chat_id, text, markup = bot.sent[0]
        self.assertEqual(chat_id, OWNER)
        self.assertTrue(text.startswith("⏰ 2 "))
        self.assertIn("@work_cn (id 6941289070)", text)
        self.assertNotIn("approved_guy", text)
        self.assertNotIn("#user_confirmation", text)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, A.ACCESS_LIST)

    def test_no_digest_when_nothing_pending(self):
        register_or_update_user(self.db, 222, "approved_guy", "")
        set_user_status(self.db, 222, "approved")
        bot = _FakeBot()
        _run(AR._access_pending_reminder_job(_Ctx(bot)))
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
