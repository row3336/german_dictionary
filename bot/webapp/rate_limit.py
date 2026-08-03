"""Per-user rate limiting for /api/v1/ (miniapp.md §9.3).

Quiz traffic is one request per answer, but a flaky connection retries and a
double tap doubles that - 60 requests/minute per user is generous headroom, not a
throttle a normal session should ever feel. The limiter runs after the access
gate, so it counts real, approved users; an anonymous or refused caller never
reaches it (401/403 happen first, in auth_middleware).
"""

import time

from aiohttp import web

from .access import API_PREFIX, REQUEST_USER_ID
from .app_keys import RATE_LIMITER_KEY

REQUESTS_PER_WINDOW = 60
WINDOW_SECONDS = 60.0

RATE_LIMITED = "rate_limited"


class RateLimiter:
    """Fixed-window request counter, one window per user.

    A fixed window is simpler than a sliding one, and the difference does not
    matter for this purpose: the limit exists to catch a runaway or looping
    client, not to police the exact 61st request of a legitimate burst.
    """

    def __init__(self, limit=REQUESTS_PER_WINDOW, window_seconds=WINDOW_SECONDS, time_source=time.time):
        self._limit = limit
        self._window = window_seconds
        self._now = time_source
        self._windows = {}  # user_id -> (window_start, count)

    def allow(self, user_id):
        """True and counted, or False when this user is over the limit."""
        now = self._now()
        start, count = self._windows.get(user_id, (now, 0))
        if now - start >= self._window:
            start, count = now, 0
        count += 1
        self._windows[user_id] = (start, count)
        return count <= self._limit


@web.middleware
async def rate_limit_middleware(request, handler):
    """429 once a user is past the per-minute limit (§9.3, §7.7).

    Placed after auth_middleware in the middleware list, so it runs once the
    request already carries a verified user id - only /api/v1/ routes ever do.
    """
    if not request.path.startswith(API_PREFIX):
        return await handler(request)
    user_id = request.get(REQUEST_USER_ID)
    if user_id is None:
        return await handler(request)
    if not request.app[RATE_LIMITER_KEY].allow(user_id):
        return web.json_response({"error": RATE_LIMITED}, status=429)
    return await handler(request)
