"""Mini App M8 — per-user rate limiting (miniapp.md §9.3, §7.7).

The limiter itself is tested directly (no event loop needed for a fixed-window
counter), and once as a full HTTP round trip so the 429 contract - the wire code
`rate_limited`, and the fact that /healthz and a refused/unauthenticated caller are
never counted - is pinned down through the real middleware stack.
"""

import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.webapp.access import API_PREFIX
from bot.webapp.rate_limit import RATE_LIMITED, RateLimiter
from bot.webapp.server import create_app

USER = 111
OTHER = 222


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RateLimiterUnitTest(unittest.TestCase):
    def test_allows_up_to_the_limit_within_one_window(self):
        clock = FakeClock()
        limiter = RateLimiter(limit=3, window_seconds=60, time_source=clock)
        self.assertTrue(limiter.allow(USER))
        self.assertTrue(limiter.allow(USER))
        self.assertTrue(limiter.allow(USER))

    def test_refuses_the_request_past_the_limit(self):
        clock = FakeClock()
        limiter = RateLimiter(limit=2, window_seconds=60, time_source=clock)
        self.assertTrue(limiter.allow(USER))
        self.assertTrue(limiter.allow(USER))
        self.assertFalse(limiter.allow(USER))

    def test_counters_are_independent_per_user(self):
        clock = FakeClock()
        limiter = RateLimiter(limit=1, window_seconds=60, time_source=clock)
        self.assertTrue(limiter.allow(USER))
        self.assertFalse(limiter.allow(USER))
        # A different user is not affected by USER's window.
        self.assertTrue(limiter.allow(OTHER))

    def test_a_new_window_resets_the_count(self):
        clock = FakeClock()
        limiter = RateLimiter(limit=1, window_seconds=60, time_source=clock)
        self.assertTrue(limiter.allow(USER))
        self.assertFalse(limiter.allow(USER))
        clock.advance(60)
        self.assertTrue(limiter.allow(USER))

    def test_just_under_the_window_boundary_still_counts_against_the_old_window(self):
        clock = FakeClock()
        limiter = RateLimiter(limit=1, window_seconds=60, time_source=clock)
        self.assertTrue(limiter.allow(USER))
        clock.advance(59)
        self.assertFalse(limiter.allow(USER))


class RateLimitHttpTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.clock = FakeClock()
        self.limiter = RateLimiter(limit=2, window_seconds=60, time_source=self.clock)
        return create_app(TOKEN, rate_limiter=self.limiter)

    async def test_the_third_request_in_a_window_is_refused(self):
        for _ in range(2):
            resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
            self.assertEqual(resp.status, 200)
        resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.json(), {"error": RATE_LIMITED})

    async def test_a_refused_unauthenticated_caller_is_never_counted(self):
        for _ in range(5):
            resp = await self.client.get(f"{API_PREFIX}me")
            self.assertEqual(resp.status, 401)
        # The limiter never saw a user id, so USER's own window is still fresh.
        resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
        self.assertEqual(resp.status, 200)

    async def test_healthz_is_never_rate_limited(self):
        for _ in range(5):
            resp = await self.client.get("/healthz")
            self.assertEqual(resp.status, 200)

    async def test_the_window_resets_after_it_elapses(self):
        for _ in range(2):
            await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
        self.clock.advance(61)
        resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
        self.assertEqual(resp.status, 200)

    async def test_each_user_has_an_independent_budget(self):
        self.make_user(OTHER, "approved")
        for _ in range(2):
            await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
        resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(OTHER))
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
