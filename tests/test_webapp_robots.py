"""Mini App — the app must not be indexable (bot/webapp/robots.py).

The hostname is public as soon as a certificate exists for it, so "nobody links
to it" is not a defence. What these pin down is that the refusal is complete: not
just the shell, but the assets, the API's own error bodies and the router's 404 -
every one of them is a response a crawler can keep if we do not say otherwise.

They also pin the refusal to the code rather than to a CDN setting, because the
host in front of the app changes (a dev tunnel today, a bought domain later) and
a panel toggle does not travel with a deploy.
"""

import unittest

from aiohttp.test_utils import AioHTTPTestCase

from bot.webapp.robots import ROBOTS_TAG
from bot.webapp.server import create_app

TOKEN = "123456:AAH-token"


class RobotsTest(AioHTTPTestCase):
    async def get_application(self):
        return create_app(TOKEN)

    async def test_robots_txt_disallows_everything_without_auth(self):
        resp = await self.client.get("/robots.txt")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/plain", resp.headers.get("Content-Type", ""))
        body = await resp.text()
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)

    async def test_app_shell_carries_the_header(self):
        resp = await self.client.get("/app")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Robots-Tag"), ROBOTS_TAG)

    async def test_static_asset_carries_the_header(self):
        resp = await self.client.get("/static/app.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("X-Robots-Tag"), ROBOTS_TAG)

    async def test_api_rejection_carries_the_header(self):
        """A 401 body is still indexable text until the header says otherwise."""
        resp = await self.client.get("/api/v1/me")
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.headers.get("X-Robots-Tag"), ROBOTS_TAG)

    async def test_unknown_path_carries_the_header(self):
        """aiohttp raises its 404 instead of returning it — the header still lands."""
        resp = await self.client.get("/no-such-page")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.headers.get("X-Robots-Tag"), ROBOTS_TAG)


if __name__ == "__main__":
    unittest.main()
