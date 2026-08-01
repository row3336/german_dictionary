"""Mini App M1 — aiohttp app factory, /healthz and the error middleware
(miniapp.md §7.1, §3.3, risk #7 in §12).

/healthz is the one route with no authentication, so the test pins down what it is
allowed to say: liveness and our own version, nothing about the database or the
users. The middleware test pins down that a handler blowing up returns 500 instead
of escaping into the event loop the bot shares.
"""

import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from bot.app_config import APP_VERSION
from bot.webapp.config import WEBAPP_HOST, get_webapp_port
from bot.webapp.server import BOT_TOKEN_KEY, WebAppServer, create_app, error_middleware

TOKEN = "123456:AAH-token"


class HealthzTest(AioHTTPTestCase):
    async def get_application(self):
        return create_app(TOKEN)

    async def test_reports_liveness_and_version(self):
        resp = await self.client.get("/healthz")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"ok": True, "version": APP_VERSION})

    async def test_says_nothing_about_data_or_the_token(self):
        body = await (await self.client.get("/healthz")).text()
        for leak in ("user", "words", "db", TOKEN):
            self.assertNotIn(leak, body.lower())

    async def test_unknown_route_is_a_plain_404(self):
        self.assertEqual((await self.client.get("/api/v1/nope")).status, 404)


class ErrorMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def _client(self, app):
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_an_unhandled_exception_becomes_a_500_json_body(self):
        app = web.Application(middlewares=[error_middleware])

        async def boom(request):
            raise RuntimeError("kaboom")

        app.router.add_get("/boom", boom)
        client = await self._client(app)
        with self.assertLogs("bot.webapp.server", level="ERROR"):
            resp = await client.get("/boom")
        self.assertEqual(resp.status, 500)
        # No traceback, no exception text - the client learns nothing useful.
        self.assertEqual(await resp.json(), {"error": "internal_error"})

    async def test_http_exceptions_pass_through_untouched(self):
        app = web.Application(middlewares=[error_middleware])

        async def gone(request):
            raise web.HTTPConflict(text="session_taken_over")

        app.router.add_get("/gone", gone)
        client = await self._client(app)
        resp = await client.get("/gone")
        self.assertEqual(resp.status, 409)


class AppFactoryTest(unittest.TestCase):
    def test_stores_the_bot_token_for_request_handlers(self):
        self.assertEqual(create_app(TOKEN)[BOT_TOKEN_KEY], TOKEN)


class ServerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_binds_loopback_and_stops_idempotently(self):
        # Port 0 lets the OS pick a free one, so the test never fights the real
        # 8081 or a running bot.
        server = WebAppServer(TOKEN, port=0)
        await server.start()
        try:
            await server.start()  # second call is a no-op, not a second socket
        finally:
            await server.stop()
        await server.stop()  # post_shutdown may call this blindly

    async def test_defaults_to_the_loopback_interface(self):
        # §9.2 п.4: binding 0.0.0.0 would put the API on the public interface,
        # next to production and alpha on the same host.
        server = WebAppServer(TOKEN)
        self.assertEqual(WEBAPP_HOST, "127.0.0.1")
        self.assertEqual(server.port, get_webapp_port())


if __name__ == "__main__":
    unittest.main()
