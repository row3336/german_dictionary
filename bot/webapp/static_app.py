"""The app shell: /app and its static assets (miniapp.md §3.2, §6.2).

Deliberately outside the /api/v1/ prefix and outside auth_middleware: the shell has
to load for a user with no access at all, so it can render the "no access" screen
itself instead of leaving a blank WebView (§6.2). Every human-visible decision -
which screen, which string - happens client-side once app.js has called `/me` and
read the gate's answer from the response status.
"""

import pathlib

from aiohttp import web

STATIC_DIR = pathlib.Path(__file__).parent / "static"

routes = web.RouteTableDef()


@routes.get("/app")
async def app_shell(request):
    """The single entry point BotFather's menu button and `/app` link to."""
    return web.FileResponse(STATIC_DIR / "index.html")


def add_static(app):
    """Register the CSS/JS directory. Kept out of create_app for readability."""
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
