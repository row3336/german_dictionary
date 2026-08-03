"""Mini App M8 — the app shell route and its static assets (miniapp.md §3.2, §6.2).

`/app` and `/static/*` sit outside the access gate on purpose: a user with no
access at all still has to load the shell so it can show them the "no access"
screen (§6.2). This pins down that both are reachable with no Authorization header
at all, and that a broken or missing static file is not silently served as 200.
"""

import unittest

from aiohttp.test_utils import AioHTTPTestCase

from bot.webapp.server import create_app

TOKEN = "123456:AAH-token"


class StaticAppTest(AioHTTPTestCase):
    async def get_application(self):
        return create_app(TOKEN)

    async def test_app_shell_serves_the_index_page_without_auth(self):
        resp = await self.client.get("/app")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        body = await resp.text()
        self.assertIn('id="app"', body)
        self.assertIn("/static/app.js", body)
        self.assertIn("/static/app.css", body)

    async def test_stylesheet_is_served_without_auth(self):
        resp = await self.client.get("/static/app.css")
        self.assertEqual(resp.status, 200)
        self.assertIn("css", resp.headers.get("Content-Type", ""))

    async def test_script_is_served_without_auth(self):
        resp = await self.client.get("/static/app.js")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertIn("Telegram", body)

    async def test_directory_listing_is_not_exposed(self):
        # §9.2 п.7: no directory listing on prod. show_index=False in static_app.py.
        resp = await self.client.get("/static/")
        self.assertNotEqual(resp.status, 200)

    async def test_unknown_static_file_is_a_plain_404(self):
        resp = await self.client.get("/static/does-not-exist.js")
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
