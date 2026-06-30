"""P3.1 — owner pending-requests digest + its search-tag toggle.

Two concerns, both verified live on the test server and pinned here:
  * the 2h digest lists *every* pending request (username + id) and offers the
    toggle button (no live hashtag in the body);
  * the toggle reveals/hides the searchable #user_confirmation_request on demand
    (idempotent, owner-only), so the open-hashtag search isn't polluted by every
    reminder.

telegram is stubbed (PTB isn't installed); the inline-keyboard classes are
replaced with capturing fakes bound onto the access module so callback_data and
labels are inspectable regardless of suite import order.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.access as A  # noqa: E402
import bot.jobs.access_reminders as AR  # noqa: E402
from bot.app_config import ACCESS_REQUEST_HASHTAG  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_status  # noqa: E402

OWNER, GUEST = 999, 111
_SUFFIX = f"\n\n{ACCESS_REQUEST_HASHTAG}"


class _FakeButton:
    def __init__(self, text, callback_data=None, **k):
        self.text = text
        self.callback_data = callback_data


class _FakeMarkup:
    def __init__(self, inline_keyboard, **k):
        self.inline_keyboard = inline_keyboard


# Bind capturing classes onto the access module (pending_tag_keyboard resolves
# these from module globals at call time, so this is order-independent).
A.InlineKeyboardButton = _FakeButton
A.InlineKeyboardMarkup = _FakeMarkup


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text):
        self.text = text


class _FakeQuery:
    def __init__(self, data, text, uid):
        self.data = data
        self.message = _FakeMessage(text)
        self.from_user = _FakeUser(uid)
        self.answered = False
        self.edits = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None):
        self.message.text = text
        self.edits.append((text, reply_markup))


class _FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _only_button(markup):
    return markup.inline_keyboard[0][0]


def _run(coro):
    return asyncio.run(coro)


class AccessTagToggleTest(unittest.TestCase):
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

    def test_keyboard_offers_show_when_hidden(self):
        lang = A._owner_lang()
        btn = _only_button(A.pending_tag_keyboard(lang, shown=False))
        self.assertEqual(btn.callback_data, A.ACCESS_TAG_SHOW)
        self.assertEqual(btn.text, A.t(lang, "access_reminder_show_tag"))

    def test_keyboard_offers_hide_when_shown(self):
        lang = A._owner_lang()
        btn = _only_button(A.pending_tag_keyboard(lang, shown=True))
        self.assertEqual(btn.callback_data, A.ACCESS_TAG_HIDE)
        self.assertEqual(btn.text, A.t(lang, "access_reminder_hide_tag"))

    def test_show_reveals_hashtag_and_flips_button(self):
        base = "⏰ 2 pending:\n• @a (id 1)\n• @b (id 2)"
        q = _FakeQuery(A.ACCESS_TAG_SHOW, base, OWNER)
        _run(A.access_decision_callback(_FakeUpdate(q), None))
        self.assertTrue(q.message.text.endswith(_SUFFIX))
        self.assertEqual(q.message.text, base + _SUFFIX)
        _, markup = q.edits[-1]
        self.assertEqual(_only_button(markup).callback_data, A.ACCESS_TAG_HIDE)

    def test_hide_strips_hashtag_back_to_base(self):
        base = "⏰ 1 pending:\n• @a (id 1)"
        q = _FakeQuery(A.ACCESS_TAG_HIDE, base + _SUFFIX, OWNER)
        _run(A.access_decision_callback(_FakeUpdate(q), None))
        self.assertEqual(q.message.text, base)
        _, markup = q.edits[-1]
        self.assertEqual(_only_button(markup).callback_data, A.ACCESS_TAG_SHOW)

    def test_show_is_idempotent_no_double_tag(self):
        # A second "show" on already-shown text must not append a second hashtag.
        base = "⏰ 1 pending:\n• @a (id 1)"
        q = _FakeQuery(A.ACCESS_TAG_SHOW, base + _SUFFIX, OWNER)
        _run(A.access_decision_callback(_FakeUpdate(q), None))
        self.assertEqual(q.message.text, base + _SUFFIX)
        self.assertEqual(q.message.text.count(ACCESS_REQUEST_HASHTAG), 1)

    def test_non_owner_cannot_toggle(self):
        base = "⏰ 1 pending:\n• @a (id 1)"
        q = _FakeQuery(A.ACCESS_TAG_SHOW, base, GUEST)
        _run(A.access_decision_callback(_FakeUpdate(q), None))
        self.assertEqual(q.message.text, base)  # unchanged
        self.assertEqual(q.edits, [])  # no edit happened


class PendingDigestTest(unittest.TestCase):
    """The 2h reminder lists every pending request and carries the toggle button.

    Pins the P3.1 closing criterion verified live (digest listed 4 pending with
    username + id): all pending users appear, approved/owner users do not, and
    the body carries no live hashtag (the toggle reveals it instead)."""

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

    def test_digest_lists_all_pending_only(self):
        register_or_update_user(self.db, 6941289070, "work_cn", "")
        register_or_update_user(self.db, 628411953, "M0i09", "")
        register_or_update_user(self.db, 222, "approved_guy", "")
        set_user_status(self.db, 222, "approved")  # must not appear in the digest
        bot = _FakeBot()
        _run(AR._access_pending_reminder_job(_Ctx(bot)))
        self.assertEqual(len(bot.sent), 1)
        chat_id, text, markup = bot.sent[0]
        self.assertEqual(chat_id, OWNER)
        self.assertTrue(text.startswith("⏰ 2 "))  # exactly the two pending
        self.assertIn("@work_cn (id 6941289070)", text)
        self.assertIn("@M0i09 (id 628411953)", text)
        self.assertNotIn("approved_guy", text)
        # No live hashtag in the body; the toggle button reveals it on demand.
        self.assertNotIn(ACCESS_REQUEST_HASHTAG, text)
        self.assertEqual(_only_button(markup).callback_data, A.ACCESS_TAG_SHOW)

    def test_no_digest_when_nothing_pending(self):
        register_or_update_user(self.db, 222, "approved_guy", "")
        set_user_status(self.db, 222, "approved")
        bot = _FakeBot()
        _run(AR._access_pending_reminder_job(_Ctx(bot)))
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
