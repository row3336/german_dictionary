"""Authentication and the access gate for /api/v1/ (miniapp.md §6.2).

One middleware, one place. Every authenticated route can then assume the request
carries a real, approved user id — no handler repeats the check, and no handler
can forget it.

The access matrix is the bot's own (multiuser.md §2), read through the same
storage function the bot uses. Deliberately NOT imported from bot/handlers/access.py:
that module pulls in `telegram` at import time, and the web layer must stay
runnable (and testable) without PTB.
"""

import logging

from aiohttp import web

from ..app_config import get_db_path, is_owner
from ..storage import get_user_status
from .auth import AUTH_HEADER, InitDataError, parse_auth_header, validate_init_data

logger = logging.getLogger(__name__)

# Everything under this prefix is authenticated and gated. Static files and
# /healthz sit outside it: the shell has to load in order to render the "no
# access" screen at all, so gating it would leave the user a white rectangle.
API_PREFIX = "/api/v1/"

# aiohttp application key — the token used to verify initData signatures.
BOT_TOKEN_KEY = web.AppKey("bot_token", str)

# Request keys filled in by the middleware for downstream handlers.
REQUEST_USER_ID = "webapp_user_id"
REQUEST_USER = "webapp_user"

ACCESS_OK = "ok"
ACCESS_PENDING = "access_pending"
ACCESS_REJECTED = "access_rejected"
ACCESS_UNKNOWN = "access_unknown"


def resolve_access(user_id):
    """Map a user id onto the gate outcome (§6.2). Read-only.

    The owner is always allowed, regardless of what the `users` row says — the same
    rule the bot applies, and it keeps a mis-set owner row from locking the owner out.
    An unrecognised status is treated as `pending`: the safe direction is "not yet",
    never "sure, come in".
    """
    if is_owner(user_id):
        return ACCESS_OK
    status = get_user_status(get_db_path(), user_id)
    if status is None:
        return ACCESS_UNKNOWN
    if status == "approved":
        return ACCESS_OK
    if status == "rejected":
        return ACCESS_REJECTED
    return ACCESS_PENDING


@web.middleware
async def auth_middleware(request, handler):
    """Verify initData, then apply the access gate. Only for /api/v1/."""
    if not request.path.startswith(API_PREFIX):
        return await handler(request)

    try:
        raw = parse_auth_header(request.headers.get(AUTH_HEADER))
        data = validate_init_data(raw, request.app[BOT_TOKEN_KEY])
    except InitDataError as exc:
        # The reason is ours to keep; the client gets one opaque code (§6.1).
        logger.info("webapp: init data refused on %s (%s)", request.path, exc.reason)
        return web.json_response({"error": exc.code}, status=401)

    user_id = data["user_id"]
    outcome = resolve_access(user_id)
    if outcome != ACCESS_OK:
        return web.json_response({"error": outcome}, status=403)

    request[REQUEST_USER_ID] = user_id
    request[REQUEST_USER] = data["user"]
    return await handler(request)
