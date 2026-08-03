"""Learning-batch settings, in plain language (miniapp.md §7.6a).

Owner decision, 2026-08-03: this is the one algorithm settings screen carved out of
the bot for the app, because the bot's own menu (`handlers/settings.py:181`) labels
these five fields with the raw `user_settings` key name - `daily_qty`,
`intervals_sec`, `reminder_interval_sec`... - and the English locale does not even
translate that into a sentence. Everything else algorithmic (FSRS/weak/drill) stays
bot-only per miniapp.md §2.

The five values are read and written through the exact accessors and
`user_settings` keys the bot uses (`common/settings.py`, `app_config.py`), so a
change here is the same change the bot sees - one setting, two clients, like
`topic_filter` (§7.6). All this module adds is unit conversion (seconds <-> minutes,
for a human to type) and validation copied from the bot's own edit flow
(`handlers/settings.py:594`), not a second copy of the algorithm.
"""

from aiohttp import web

from ..app_config import (
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_KEY,
    LEARNING_READY_QUORUM_KEY,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_REMINDER_INTERVAL_KEY,
    get_db_path,
)
from ..common.settings import (
    _learning_daily_qty,
    _learning_intervals_seconds,
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
)
from ..storage import _parse_learning_intervals, set_user_setting  # noqa: PLC2701 - deliberate reuse, see module docstring
from .access import API_PREFIX, REQUEST_USER_ID
from .session_api import _json_body  # noqa: PLC2701 - deliberate reuse, see module docstring

routes = web.RouteTableDef()

INVALID_VALUE = "invalid_value"


def _settings_view(user_id):
    """The GET/POST response shape: everything in minutes, nothing in seconds."""
    db = get_db_path()
    return {
        "daily_qty": _learning_daily_qty(db, user_id),
        "quorum_pct": _learning_ready_quorum(db, user_id),
        "intervals_minutes": [round(s / 60, 3) for s in _learning_intervals_seconds(db, user_id)],
        "reminder_delay_minutes": round(_learning_reminder_interval(db, user_id) / 60, 3),
        "reminder_cooldown_minutes": round(_learning_reminder_cooldown(db, user_id) / 60, 3),
    }


def _int_in_range(value, low, high=None):
    """Parse a JSON number/string into an int, or raise ValueError.

    Loose on the input type (json can hand back an int, a float, or a numeric
    string) and strict on the range - the same trade-off `int(text)` in the bot's
    own edit flow makes, just without requiring the value to already be text.
    """
    ivalue = int(round(float(value)))
    if ivalue < low or (high is not None and ivalue > high):
        raise ValueError
    return ivalue


def _minutes_to_seconds(value, minimum_seconds):
    """A minutes value from the UI, converted and floor-checked in seconds.

    Fractional minutes are allowed (the default shortest interval is 30s = 0.5
    min), so this rounds rather than truncates.
    """
    seconds = int(round(float(value) * 60))
    if seconds < minimum_seconds:
        raise ValueError
    return seconds


@routes.get(f"{API_PREFIX}learning/settings")
async def get_learning_settings(request):
    return web.json_response(_settings_view(request[REQUEST_USER_ID]))


@routes.post(f"{API_PREFIX}learning/settings")
async def update_learning_settings(request):
    """Partial update: any subset of the five fields, same shape as GET back."""
    body = await _json_body(request)
    user_id = request[REQUEST_USER_ID]
    db = get_db_path()

    try:
        if "daily_qty" in body:
            set_user_setting(db, user_id, LEARNING_DAILY_QTY_KEY, str(_int_in_range(body["daily_qty"], 1)))

        if "quorum_pct" in body:
            set_user_setting(db, user_id, LEARNING_READY_QUORUM_KEY, str(_int_in_range(body["quorum_pct"], 1, 100)))

        if "reminder_delay_minutes" in body:
            # _learning_reminder_interval floors reads at 10s regardless of what
            # is stored (common/settings.py); the write-side floor matches the
            # bot's own edit flow (>= 1s) rather than pre-empting that clamp.
            seconds = _minutes_to_seconds(body["reminder_delay_minutes"], 1)
            set_user_setting(db, user_id, LEARNING_REMINDER_INTERVAL_KEY, str(seconds))

        if "reminder_cooldown_minutes" in body:
            seconds = _minutes_to_seconds(body["reminder_cooldown_minutes"], 1)
            set_user_setting(db, user_id, LEARNING_REMINDER_COOLDOWN_KEY, str(seconds))

        if "intervals_minutes" in body:
            raw = body["intervals_minutes"]
            if not isinstance(raw, list) or not raw:
                raise ValueError
            seconds = [_minutes_to_seconds(v, 1) for v in raw]
            text = ",".join(str(v) for v in seconds)
            if not _parse_learning_intervals(text):
                raise ValueError
            set_user_setting(db, user_id, LEARNING_INTERVALS_KEY, text)
    except (TypeError, ValueError):
        return web.json_response({"error": INVALID_VALUE}, status=400)

    return web.json_response(_settings_view(user_id))
