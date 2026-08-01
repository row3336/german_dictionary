"""Mini App M1 — PTB lifecycle wiring (miniapp.md §3.3, risk #7 in §12).

Exercised against a stand-in for PTB's Application: the wrappers only ever touch
`.bot.token` and `.bot_data`, so the real object is not needed and no bot has to
run. What matters here is that the bot's own startup is untouched, that the flag
really keeps the server down, and that a broken web layer cannot take the bot with
it.
"""

import asyncio
import os
import unittest
from unittest import mock

from bot.webapp.integration import (
    WEBAPP_SERVER_KEY,
    build_post_init,
    build_post_shutdown,
)


class FakeBot:
    def __init__(self, token="123456:AAH-token"):
        self.token = token


class FakeApp:
    """Minimal stand-in for telegram.ext.Application."""

    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}


class FakeServer:
    def __init__(self, *args, **kwargs):
        self.token = args[0] if args else None
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


class WebappFlagCase(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("WEBAPP_ENABLED")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("WEBAPP_ENABLED", None)
        else:
            os.environ["WEBAPP_ENABLED"] = self._old

    def set_flag(self, value):
        os.environ["WEBAPP_ENABLED"] = value

    def patch_server(self, factory=FakeServer):
        # build_post_init imports WebAppServer lazily, so the patch lands on the
        # module attribute it will look up at call time.
        patcher = mock.patch("bot.webapp.server.WebAppServer", factory)
        self.addCleanup(patcher.stop)
        return patcher.start()


class PostInitTest(WebappFlagCase):
    def test_runs_the_existing_callback_first(self):
        calls = []

        async def inner(app):
            calls.append(app)

        self.set_flag("0")
        app = FakeApp()
        asyncio.run(build_post_init(inner)(app))
        self.assertEqual(calls, [app])

    def test_disabled_flag_starts_no_server(self):
        self.patch_server()
        self.set_flag("0")
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        self.assertNotIn(WEBAPP_SERVER_KEY, app.bot_data)

    def test_enabled_flag_starts_the_server_with_the_bot_token(self):
        self.patch_server()
        self.set_flag("1")
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        server = app.bot_data[WEBAPP_SERVER_KEY]
        self.assertEqual(server.started, 1)
        self.assertEqual(server.token, app.bot.token)

    def test_a_failing_server_does_not_break_startup(self):
        class Exploding(FakeServer):
            async def start(self):
                raise RuntimeError("port already in use")

        self.patch_server(Exploding)
        self.set_flag("1")
        app = FakeApp()
        with self.assertLogs("bot.webapp.integration", level="ERROR"):
            asyncio.run(build_post_init()(app))
        # The bot is up; the web layer simply is not registered.
        self.assertNotIn(WEBAPP_SERVER_KEY, app.bot_data)

    def test_missing_aiohttp_does_not_break_startup(self):
        # Mirrors a deploy where requirements were not reinstalled but the flag was
        # flipped: the lazy import raises, and the bot must still come up.
        patcher = mock.patch.dict("sys.modules", {"bot.webapp.server": None})
        self.addCleanup(patcher.stop)
        patcher.start()
        self.set_flag("1")
        app = FakeApp()
        with self.assertLogs("bot.webapp.integration", level="ERROR"):
            asyncio.run(build_post_init()(app))
        self.assertNotIn(WEBAPP_SERVER_KEY, app.bot_data)

    def test_inner_failure_still_propagates(self):
        # Behaviour of the bot's own post_init must not change (§5.3).
        async def inner(app):
            raise ValueError("resume_jobs blew up")

        self.set_flag("0")
        with self.assertRaises(ValueError):
            asyncio.run(build_post_init(inner)(FakeApp()))


class PostShutdownTest(WebappFlagCase):
    def test_stops_a_running_server_and_forgets_it(self):
        self.patch_server()
        self.set_flag("1")
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        server = app.bot_data[WEBAPP_SERVER_KEY]
        asyncio.run(build_post_shutdown()(app))
        self.assertEqual(server.stopped, 1)
        self.assertNotIn(WEBAPP_SERVER_KEY, app.bot_data)

    def test_is_a_no_op_when_the_server_never_started(self):
        asyncio.run(build_post_shutdown()(FakeApp()))  # must not raise

    def test_runs_the_existing_callback(self):
        calls = []

        async def inner(app):
            calls.append(app)

        app = FakeApp()
        asyncio.run(build_post_shutdown(inner)(app))
        self.assertEqual(calls, [app])

    def test_a_failing_stop_does_not_break_shutdown(self):
        class Stubborn(FakeServer):
            async def stop(self):
                raise RuntimeError("cleanup failed")

        self.patch_server(Stubborn)
        self.set_flag("1")
        app = FakeApp()
        asyncio.run(build_post_init()(app))
        calls = []

        async def inner(app):
            calls.append(app)

        with self.assertLogs("bot.webapp.integration", level="ERROR"):
            asyncio.run(build_post_shutdown(inner)(app))
        self.assertEqual(calls, [app])


if __name__ == "__main__":
    unittest.main()
