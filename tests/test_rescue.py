"""Spec #13 — rescue handler for a bot restart (conversation state lives only
in memory, so a lost/stale chat state must not leave the bot silent).

Two things are verified: (1) `rescue_handler`'s own branching (approved/owner
-> main menu + heads-up; anyone else -> normal onboarding), and (2) the
critical ordering invariant this whole feature depends on — `rescue_handler`
must be the LAST handler registered in `bot/main.py`'s handler group, so it
only ever fires when nothing above it (the conversation, every
CallbackQueryHandler, the payment handler) already claimed the update. A
live conversation always matches its own `TEXT & ~COMMAND` catch-all first and
wins, so this handler never intercepts one — but only if the registration
order stays intact, which is why that order is asserted directly against
`bot/main.py`'s source rather than left to hope.
"""

import asyncio
import inspect
import os
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

import bot.handlers.rescue as R  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_status  # noqa: E402

OWNER, GUEST = 999, 111


def _run(coro):
    return asyncio.run(coro)


class _Msg:
    def __init__(self, text="hello"):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))


class _Update:
    def __init__(self, uid, text="hello"):
        self.message = _Msg(text)
        self.effective_user = types.SimpleNamespace(id=uid, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=uid)


class RescueHandlerTest(unittest.TestCase):
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
        self._old_guest_start = R._guest_start
        self.addCleanup(lambda: setattr(R, "_guest_start", self._old_guest_start))

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_approved_guest_gets_main_menu_and_heads_up(self):
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        called = []
        R._guest_start = lambda *a, **k: called.append(1)

        update = _Update(GUEST)
        _run(R.rescue_handler(update, None))

        self.assertEqual(len(update.message.calls), 1)
        text, kw = update.message.calls[0]
        self.assertEqual(text, TEXTS["en"]["rescue_menu"])
        self.assertIsNotNone(kw.get("reply_markup"))
        self.assertEqual(called, [])  # onboarding not touched for an approved user

    def test_owner_also_gets_main_menu(self):
        called = []
        R._guest_start = lambda *a, **k: called.append(1)

        update = _Update(OWNER)
        _run(R.rescue_handler(update, None))

        self.assertEqual(len(update.message.calls), 1)
        self.assertEqual(called, [])

    def test_non_approved_user_goes_through_onboarding(self):
        register_or_update_user(self.db, GUEST, "g", "G")  # left pending

        async def fake_guest_start(update, context):
            called.append((update, context))

        called = []
        R._guest_start = fake_guest_start

        update = _Update(GUEST)
        ctx = object()
        _run(R.rescue_handler(update, ctx))

        self.assertEqual(update.message.calls, [])  # rescue itself sent nothing
        self.assertEqual(len(called), 1)
        self.assertIs(called[0][0], update)
        self.assertIs(called[0][1], ctx)

    def test_brand_new_unregistered_user_goes_through_onboarding(self):
        async def fake_guest_start(update, context):
            called.append(1)

        called = []
        R._guest_start = fake_guest_start

        update = _Update(GUEST)  # never registered at all
        _run(R.rescue_handler(update, None))

        self.assertEqual(update.message.calls, [])
        self.assertEqual(called, [1])


class RescueI18nTest(unittest.TestCase):
    def test_all_three_languages_have_a_non_empty_rescue_text(self):
        for lang in ("en", "ru", "uk"):
            self.assertTrue(TEXTS[lang]["rescue_menu"].strip())


class RescueRegistrationOrderTest(unittest.TestCase):
    """The whole "never intercepts a live conversation" guarantee rests on
    rescue_handler being registered strictly LAST among bot.main's handlers.
    Guard that invariant directly against the source, since it can't be
    exercised through a real PTB Application in this test suite (telegram is
    stubbed process-wide for the other ~360 tests)."""

    def test_rescue_handler_is_registered_after_every_other_handler(self):
        import bot.main as M

        src = inspect.getsource(M.main)
        markers = [
            "app.add_handler(conv)",
            "access_decision_callback",
            "topic_filter_toggle_callback",
            "broadcast_menu_callback",
            "stat_toggle_callback",
            "precheckout_callback",
            "successful_payment_callback",
            "MessageHandler(filters.TEXT & ~filters.COMMAND, rescue_handler)",
        ]
        positions = [src.index(m) for m in markers]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
