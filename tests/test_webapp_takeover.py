"""Mini App M3 step 3 — the chat reclaims the session (miniapp.md §4.2 п.2, §11).

The user's own description of the round trip: poke the bot, open the app (takeover
one way), go back to the bot and answer (takeover the other way). This file covers
the second half - the one with no button, where typing in the chat is the whole
gesture.

Two properties matter beyond "it flips the flag". The hook must not disturb the
update on its way to the handler that was always going to serve it, and it must not
grow a record for every user who ever wrote to the bot.
"""

import asyncio
import os
import unittest
import unittest.mock
from types import SimpleNamespace

from _telegram_stub import install

install()

from bot.webapp.integration import (  # noqa: E402
    WEBAPP_REGISTRY_KEY,
    WEBAPP_SERVER_KEY,
    build_post_init,
)
from bot.webapp.sessions import CLIENT_APP, CLIENT_BOT, SessionRegistry, SessionTakenOver  # noqa: E402
from bot.webapp.takeover_hook import (  # noqa: E402
    TAKEOVER_HANDLER_GROUP,
    build_takeover_callback,
)

USER = 111
OTHER = 222
MODE = "l1_de"


def update_from(user_id):
    """Barely an Update: the callback only ever reads effective_user.id."""
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


class TakeoverCallbackTest(unittest.TestCase):
    def setUp(self):
        self.registry = SessionRegistry()
        self.callback = build_takeover_callback(self.registry)

    def fire(self, update):
        return asyncio.run(self.callback(update, None))

    def test_typing_in_the_chat_takes_the_session_from_the_app(self):
        session = self.registry.start(USER, MODE)
        self.fire(update_from(USER))
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)
        with self.assertRaises(SessionTakenOver):
            self.registry.require(USER, session.session_id)

    def test_a_user_who_was_never_in_the_app_is_left_alone(self):
        # No record is created, so a busy bot does not accumulate one entry per
        # user for the life of the process.
        self.fire(update_from(USER))
        self.assertIsNone(self.registry.active_client(USER))
        self.assertEqual(len(self.registry), 0)

    def test_a_user_already_owned_by_the_chat_is_left_alone(self):
        self.registry.claim_for_bot(USER)
        self.fire(update_from(USER))
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)

    def test_only_the_author_of_the_update_loses_the_session(self):
        other = self.registry.start(OTHER, MODE)
        self.registry.start(USER, MODE)
        self.fire(update_from(USER))
        self.assertIs(self.registry.require(OTHER), other)

    def test_an_update_without_a_user_is_ignored(self):
        for update in (SimpleNamespace(effective_user=None), SimpleNamespace()):
            with self.subTest(update=update):
                self.fire(update)  # must not raise

    def test_a_broken_registry_does_not_break_the_users_message(self):
        class Exploding:
            def active_client(self, user_id):
                raise RuntimeError("registry is on fire")

        callback = build_takeover_callback(Exploding())
        with self.assertLogs("bot.webapp.takeover_hook", level="ERROR"):
            asyncio.run(callback(update_from(USER), None))

    def test_the_round_trip_ends_with_the_app_owning_the_session_again(self):
        # bot -> app -> bot -> app, the sequence the owner asked about.
        self.registry.start(USER, MODE)
        self.fire(update_from(USER))
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)
        self.registry.start(USER, MODE)
        self.assertEqual(self.registry.active_client(USER), CLIENT_APP)


class FakeBot:
    token = "123456:AAH-token"


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))


class FakeServer:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def start(self):
        pass

    async def stop(self):
        pass


class HookInstallationTest(unittest.TestCase):
    """The hook has to be wired at startup, or the chat can never win a session."""

    def setUp(self):
        self._old = os.environ.get("WEBAPP_ENABLED")
        self.addCleanup(self._restore)
        os.environ["WEBAPP_ENABLED"] = "1"
        patcher = unittest.mock.patch("bot.webapp.server.WebAppServer", FakeServer)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _restore(self):
        if self._old is None:
            os.environ.pop("WEBAPP_ENABLED", None)
        else:
            os.environ["WEBAPP_ENABLED"] = self._old

    def test_installs_the_handler_ahead_of_the_bots_own(self):
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        self.assertEqual(len(app.handlers), 1)
        group, handler = app.handlers[0]
        self.assertEqual(group, TAKEOVER_HANDLER_GROUP)
        self.assertLess(group, 0)
        self.assertTrue(callable(handler.callback))

    def test_the_hook_and_the_server_share_one_registry(self):
        # Two registries would mean the chat takes a session away from a registry
        # nobody is reading - the bug this test exists to prevent.
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        registry = app.bot_data[WEBAPP_REGISTRY_KEY]
        self.assertIs(app.bot_data[WEBAPP_SERVER_KEY].kwargs["registry"], registry)

        registry.start(USER, MODE)
        _, handler = app.handlers[0]
        asyncio.run(handler.callback(update_from(USER), None))
        self.assertEqual(registry.active_client(USER), CLIENT_BOT)

    def test_no_hook_when_the_flag_is_off(self):
        os.environ["WEBAPP_ENABLED"] = "0"
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        self.assertEqual(app.handlers, [])

    def test_no_hook_when_the_server_failed_to_start(self):
        class Exploding(FakeServer):
            async def start(self):
                raise RuntimeError("port already in use")

        with unittest.mock.patch("bot.webapp.server.WebAppServer", Exploding):
            app = FakeApp()
            with self.assertLogs("bot.webapp.integration", level="ERROR"):
                asyncio.run(build_post_init()(app))
        self.assertEqual(app.handlers, [])


if __name__ == "__main__":
    unittest.main()
