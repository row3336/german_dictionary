"""aiohttp application factory and lifecycle for the Mini App (miniapp.md §3.3).

Two ways to run it:

  * inside the bot — `WebAppServer` is started from PTB's post_init and stopped
    from post_shutdown, sharing the bot's event loop and process;
  * standalone — `python -m bot.webapp.server`, for local development while the
    web layer is not yet wired into bot/main.py.

Risk #7 in §12 is "the web part takes the bot down with it". Hence the error
middleware below: no request is allowed to raise past it, and a failure to start
the server is logged rather than propagated by the caller.
"""

import asyncio
import contextlib
import logging

from aiohttp import web

from . import dict_api, meta_api, session_api
from .access import auth_middleware
from .app_keys import BOT_TOKEN_KEY, BRIDGE_KEY, REGISTRY_KEY
from .bot_bridge import BotBridge
from .config import WEBAPP_HOST, get_webapp_port
from .session_api import session_error_middleware
from .sessions import SessionRegistry

logger = logging.getLogger(__name__)

# How often idle sessions are swept (§4.3). Far below the two-hour TTL, and cheap:
# the sweep walks a dict of at most one entry per active user.
SWEEP_INTERVAL_SECONDS = 5 * 60

# Re-exported for callers that build an app (the token key in particular: it is
# where the initData secret lives, and it is never logged or served — §6.1 п.4).
__all__ = ["BOT_TOKEN_KEY", "BRIDGE_KEY", "REGISTRY_KEY", "WebAppServer", "create_app"]


@web.middleware
async def error_middleware(request, handler):
    """Turn anything unexpected into a 500 JSON body, and keep the loop alive.

    HTTPException subclasses are aiohttp's own control flow (404, 405, and later
    our 401/403/409), so they pass through untouched.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        # The path is safe to log; query strings are not used by this API (§6.1 п.5).
        logger.exception("webapp: unhandled error on %s %s", request.method, request.path)
        return web.json_response({"error": "internal_error"}, status=500)


def create_app(bot_token, application=None, registry=None):
    """Build the aiohttp application. Pure — no sockets are opened here.

    `application` is the running PTB Application when the web layer lives inside
    the bot, and None when it runs standalone; the bridge turns the second case
    into a set of no-ops rather than a special case at every call site.

    Middleware order matters: the error net is outermost, so a failure inside the
    auth middleware itself still comes back as a 500 rather than escaping into the
    event loop the bot is running on.
    """
    app = web.Application(
        middlewares=[error_middleware, auth_middleware, session_error_middleware]
    )
    app[BOT_TOKEN_KEY] = bot_token
    app[BRIDGE_KEY] = BotBridge(application)
    app[REGISTRY_KEY] = SessionRegistry() if registry is None else registry
    app.add_routes(meta_api.routes)
    app.add_routes(session_api.routes)
    app.add_routes(dict_api.routes)
    app.cleanup_ctx.append(_session_sweeper)
    return app


async def _session_sweeper(app):
    """Background task dropping idle sessions for as long as the app runs (§4.3)."""

    async def run():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                dropped = app[REGISTRY_KEY].sweep()
            except Exception:
                # A sweep that throws must not end the task and leave sessions to
                # pile up for the rest of the process's life.
                logger.exception("webapp: session sweep failed")
                continue
            if dropped:
                logger.info("webapp: swept %s idle session(s)", dropped)

    task = asyncio.create_task(run())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class WebAppServer:
    """Owns the runner/site pair so start and stop stay symmetric."""

    def __init__(self, bot_token, host=WEBAPP_HOST, port=None, application=None, registry=None):
        self._bot_token = bot_token
        self._host = host
        self._port = get_webapp_port() if port is None else port
        self._application = application
        self._registry = registry
        self._runner = None

    @property
    def port(self):
        return self._port

    async def start(self):
        """Bind the loopback socket. Safe to call once per process."""
        if self._runner is not None:
            return
        runner = web.AppRunner(
            create_app(self._bot_token, application=self._application, registry=self._registry),
            access_log=None,
        )
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._runner = runner
        logger.info("webapp: listening on http://%s:%s", self._host, self._port)

    async def stop(self):
        """Release the socket. Idempotent, so post_shutdown can call it blindly."""
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        logger.info("webapp: stopped")


def _standalone():
    """Development entry point: run the web layer without the bot.

    Reads BOT_TOKEN from the environment/.env exactly like the bot does, because
    initData is signed with it — a server started with the wrong token rejects
    every request with 401 and looks broken for no visible reason.
    """
    import asyncio
    import os

    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise SystemExit("BOT_TOKEN is not set - initData could not be verified")

    async def run():
        server = WebAppServer(token)
        await server.start()
        try:
            await asyncio.Event().wait()  # serve until Ctrl-C
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _standalone()
