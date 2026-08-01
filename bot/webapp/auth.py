"""Validation of Telegram WebApp `initData` (miniapp.md §6.1).

Deliberately stdlib-only — no aiohttp, no telegram imports. Signature checking is
the one place where a mistake means "log in as anybody", so it stays a small pure
module that can be imported and tested on its own.

Rules that are not negotiable (§6.1):
  * hmac.compare_digest, never `==` — the naive compare leaks the hash by timing;
  * auth_date older than WEBAPP_INIT_DATA_TTL_SECONDS is refused;
  * `initDataUnsafe` (the client-side copy) is never read on the server — the
    name is literal, it carries no signature at all;
  * the bot token never reaches a log record, not even on failure.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from .config import WEBAPP_INIT_DATA_TTL_SECONDS

# initData travels in a header, not the query string: a URL would put the user's
# name and id into nginx access logs (§6.1 п.5).
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "tma"

# Single wire code for every failure (§7.7). The caller learns *that* the data is
# bad, never *which* check caught it — probing one field at a time is how a forger
# narrows down what to fix. The precise reason goes to our log instead.
INVALID_INIT_DATA = "invalid_init_data"

# Tolerance for a client clock running ahead of ours. A signature dated in the
# future is not a normal event, but a minute of skew is not an attack either.
_FUTURE_SKEW_SECONDS = 60


class InitDataError(Exception):
    """initData failed validation.

    `code` is what the HTTP layer returns; `reason` is for our log only and must
    never be sent to the client.
    """

    def __init__(self, reason, code=INVALID_INIT_DATA):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def parse_auth_header(header_value):
    """Extract raw initData from an `Authorization: tma <initData>` header."""
    if not header_value:
        raise InitDataError("missing authorization header")
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME:
        raise InitDataError("unexpected authorization scheme")
    raw = parts[1].strip()
    if not raw:
        raise InitDataError("empty init data")
    return raw


def _parse_pairs(raw):
    """Decode initData into ordered key/value pairs.

    strict_parsing rejects malformed junk outright, and duplicate keys are refused
    on purpose: `hash` appearing twice lets a forger sign one copy while the reader
    picks the other.
    """
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed init data") from exc
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise InitDataError("duplicate key in init data")
    return pairs


def _expected_hash(pairs, bot_token):
    """Telegram's scheme: the token keys an HMAC, it is not hashed as a message."""
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _check_auth_date(value, ttl_seconds, now):
    try:
        auth_date = int(value)
    except (TypeError, ValueError) as exc:
        raise InitDataError("auth_date is not an integer") from exc
    age = now - auth_date
    if age > ttl_seconds:
        raise InitDataError("init data expired")
    if age < -_FUTURE_SKEW_SECONDS:
        raise InitDataError("auth_date in the future")
    return auth_date


def _parse_user(value):
    """The `user` field is a JSON blob inside the signed query string."""
    if not value:
        raise InitDataError("no user in init data")
    try:
        user = json.loads(value)
    except ValueError as exc:
        raise InitDataError("user is not valid json") from exc
    if not isinstance(user, dict):
        raise InitDataError("user is not an object")
    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InitDataError("user has no usable id") from exc
    return user_id, user


def validate_init_data(raw, bot_token, ttl_seconds=None, now=None):
    """Verify the signature and freshness of `raw`, returning the signed payload.

    Returns a dict with `user_id`, `auth_date`, the parsed `user` object and the
    remaining signed fields. Raises InitDataError on anything that does not check
    out — there is no partial success.
    """
    if not bot_token:
        raise InitDataError("no bot token configured")
    ttl_seconds = WEBAPP_INIT_DATA_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = int(time.time()) if now is None else int(now)

    pairs = _parse_pairs(raw)
    fields = dict(pairs)
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("no hash in init data")

    signed_pairs = [(key, value) for key, value in pairs if key != "hash"]
    if not hmac.compare_digest(_expected_hash(signed_pairs, bot_token), received_hash.lower()):
        raise InitDataError("signature mismatch")

    auth_date = _check_auth_date(fields.get("auth_date"), ttl_seconds, now)
    user_id, user = _parse_user(fields.get("user"))

    return {
        "user_id": user_id,
        "user": user,
        "auth_date": auth_date,
        "fields": fields,
    }
