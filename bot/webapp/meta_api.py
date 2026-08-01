"""Meta routes (miniapp.md §7.1).

`/healthz` sits outside `/api/v1/` and outside the access gate: it answers whether
this process is up, and nothing else. No database probe, no user counts, no
version of anything but our own code — an open endpoint should not become a way to
ask questions about the data.

`/api/v1/me` is the opposite: gated and per-user. It never reads a user id from the
request — only the one the middleware derived from a verified signature — so there
is no way to ask this endpoint about somebody else (risk #6 in §12).
"""

from aiohttp import web

from ..app_config import APP_VERSION
from .access import API_PREFIX, REQUEST_USER_ID
from .overview import build_me

routes = web.RouteTableDef()


@routes.get("/healthz")
async def healthz(request):
    """Liveness for nginx/systemd checks. Deliberately boring."""
    return web.json_response({"ok": True, "version": APP_VERSION})


@routes.get(f"{API_PREFIX}me")
async def me(request):
    """Everything the main screen needs, in one round-trip."""
    return web.json_response(build_me(request[REQUEST_USER_ID]))
