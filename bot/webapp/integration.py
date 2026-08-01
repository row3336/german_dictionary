"""Wiring between PTB's lifecycle and the Mini App server (miniapp.md §3.3).

All of it lives here rather than in bot/main.py so that connecting the web layer
is a two-line edit there: an import, and `.post_init(build_post_init(resume_jobs))`
plus `.post_shutdown(build_post_shutdown())` on the builder. That keeps this branch
from carrying logic in the one file feature/multiuser also edits.

Both wrappers preserve the bot's existing behaviour exactly: the inner callback is
awaited as before, and its exceptions still propagate. Only the web part is
defensive — risk #7 in §12 is "the web part takes the bot down with it", so a
server that fails to start is logged and skipped, never raised.
"""

import logging

from .config import webapp_enabled

logger = logging.getLogger(__name__)

# The running server is parked in bot_data so post_shutdown can find it without a
# module-level global (and so tests can assert on it).
WEBAPP_SERVER_KEY = "webapp_server"


def build_post_init(inner=None):
    """Wrap an existing post_init callback with the Mini App server start.

    `inner` is the bot's current post_init (resume_jobs, spec #2). It runs first
    and unchanged: the bot's own startup must not depend on the web layer.
    """

    async def post_init(app):
        if inner is not None:
            await inner(app)
        if not webapp_enabled():
            return
        try:
            # Imported lazily: with the flag off, a deploy that has no aiohttp
            # installed still starts normally.
            from .server import WebAppServer

            # Handing the Application over is what lets the web layer clear the
            # bot's quiz state and post the takeover notice (§4.2).
            server = WebAppServer(app.bot.token, application=app)
            await server.start()
        except Exception:
            logger.exception("webapp: failed to start, bot continues without it")
            return
        app.bot_data[WEBAPP_SERVER_KEY] = server

    return post_init


def build_post_shutdown(inner=None):
    """Symmetric teardown: release the socket, then run the bot's own callback."""

    async def post_shutdown(app):
        server = app.bot_data.pop(WEBAPP_SERVER_KEY, None)
        if server is not None:
            try:
                await server.stop()
            except Exception:
                logger.exception("webapp: failed to stop cleanly")
        if inner is not None:
            await inner(app)

    return post_shutdown
