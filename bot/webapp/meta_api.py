"""Unauthenticated meta routes (miniapp.md §7.1).

`/healthz` sits outside `/api/v1/` and outside the access gate: it answers whether
this process is up, and nothing else. No database probe, no user counts, no
version of anything but our own code — an open endpoint should not become a way to
ask questions about the data.
"""

from aiohttp import web

from ..app_config import APP_VERSION

routes = web.RouteTableDef()


@routes.get("/healthz")
async def healthz(request):
    """Liveness for nginx/systemd checks. Deliberately boring."""
    return web.json_response({"ok": True, "version": APP_VERSION})
