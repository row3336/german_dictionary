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
from bot.app_config import MENU  # noqa: E402
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

    def test_approved_guest_gets_main_menu_and_lands_in_menu(self):
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        called = []
        R._guest_start = lambda *a, **k: called.append(1)

        update = _Update(GUEST)
        state = _run(R.rescue_handler(update, None))

        self.assertEqual(len(update.message.calls), 1)
        text, kw = update.message.calls[0]
        self.assertEqual(text, TEXTS["en"]["rescue_menu"])
        self.assertIsNotNone(kw.get("reply_markup"))
        self.assertEqual(called, [])  # onboarding not touched for an approved user
        # the bug was returning None (state ignored -> user never in MENU -> dead
        # buttons until /start). Must return the MENU state so the conversation
        # is actually re-entered.
        self.assertEqual(state, MENU)

    def test_owner_also_gets_main_menu_and_lands_in_menu(self):
        called = []
        R._guest_start = lambda *a, **k: called.append(1)

        update = _Update(OWNER)
        state = _run(R.rescue_handler(update, None))

        self.assertEqual(len(update.message.calls), 1)
        self.assertEqual(called, [])
        self.assertEqual(state, MENU)

    def test_non_approved_user_goes_through_onboarding_and_propagates_state(self):
        register_or_update_user(self.db, GUEST, "g", "G")  # left pending

        async def fake_guest_start(update, context):
            called.append((update, context))
            return "ONBOARDING_STATE"

        called = []
        R._guest_start = fake_guest_start

        update = _Update(GUEST)
        ctx = object()
        state = _run(R.rescue_handler(update, ctx))

        self.assertEqual(update.message.calls, [])  # rescue itself sent nothing
        self.assertEqual(len(called), 1)
        self.assertIs(called[0][0], update)
        self.assertIs(called[0][1], ctx)
        # rescue must forward whatever state onboarding decides on (so the guest
        # flow is actually entered, not dropped)
        self.assertEqual(state, "ONBOARDING_STATE")

    def test_brand_new_unregistered_user_goes_through_onboarding(self):
        async def fake_guest_start(update, context):
            called.append(1)
            return "ONBOARDING_STATE"

        called = []
        R._guest_start = fake_guest_start

        update = _Update(GUEST)  # never registered at all
        state = _run(R.rescue_handler(update, None))

        self.assertEqual(update.message.calls, [])
        self.assertEqual(called, [1])
        self.assertEqual(state, "ONBOARDING_STATE")


class RescueI18nTest(unittest.TestCase):
    def test_all_three_languages_have_a_non_empty_rescue_text(self):
        for lang in ("en", "ru", "uk"):
            self.assertTrue(TEXTS[lang]["rescue_menu"].strip())


class RescueRegistrationTest(unittest.TestCase):
    """Two invariants, guarded against bot.main's source (a real PTB Application
    can't be exercised here — telegram is stubbed process-wide):

    1. rescue_handler is the LAST *entry_point* of the ConversationHandler (not a
       standalone handler). As an entry_point it only runs when no conversation
       is active, and the state it returns re-enters the conversation. Being last
       means /start, /undo and the start button match first.
    2. It is NOT also registered standalone (the old design that swallowed the
       returned state -> dead menu buttons until /start)."""

    def test_rescue_is_last_entry_point_and_not_standalone(self):
        import bot.main as M

        src = inspect.getsource(M.main)
        ep_open = src.index("entry_points=[")
        states_open = src.index("states=", ep_open)
        conv_added = src.index("app.add_handler(conv)", states_open)
        rescue = "MessageHandler(filters.TEXT & ~filters.COMMAND, learning_resume_or_rescue)"

        # rescue is inside the entry_points block (before states) ...
        self.assertIn(rescue, src)
        self.assertLess(ep_open, src.index(rescue))
        self.assertLess(src.index(rescue), states_open)
        # ... and after the start-button entry_point (so the button wins)
        self.assertLess(src.index("START_TEXT"), src.index(rescue))
        # ... and it's the LAST entry (nothing else between it and `states={`
        # except the closing `],`)
        tail = src[src.index(rescue) + len(rescue):states_open]
        self.assertNotIn("Handler(", tail)  # no further handler after rescue in entry_points
        # not re-registered as a standalone handler after the conversation
        self.assertNotIn("rescue_handler", src[conv_added:])


if __name__ == "__main__":
    unittest.main()
