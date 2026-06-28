"""Outbound message helper + lightweight send log (P5).

When background jobs broadcast to ALL users we need observability without bloating
the disk. Policy (owner, 2026-06-28): log ONLY anomalies per message (blocked /
error) plus ONE aggregate line per job cycle; successes are not logged per-line.
Records go to a separate rotating file (`outbound.log`), never the DB — so the
schema is untouched and disk usage is bounded by rotation.

Telegram-side speed limiting (<=30 msg/s + 429/RetryAfter auto-retry) is handled
globally by AIORateLimiter on the Application; this module only covers what the
limiter does not: skipping users who blocked the bot and the send log.
"""

import logging
from logging.handlers import TimedRotatingFileHandler

from telegram.error import Forbidden, TelegramError

from ..app_config import get_log_path

_log = logging.getLogger("outbound")
_configured = False


def configure_outbound_logger():
    """Attach a dedicated rotating file handler to the 'outbound' logger.

    Idempotent. Writes next to the main bot log (`outbound.log`), small
    backupCount so disk stays bounded; does not propagate to the root logger.
    """
    global _configured
    if _configured:
        return
    path = get_log_path().parent / "outbound.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        filename=str(path),
        when="D",
        interval=1,
        backupCount=7,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False
    _configured = True


class SendStats:
    """Per-cycle counters for a broadcast; one aggregate line on `log()`."""

    def __init__(self):
        self.ok = 0
        self.blocked = 0
        self.error = 0

    @property
    def sent(self):
        """Total send attempts in this cycle."""
        return self.ok + self.blocked + self.error

    def add(self, result):
        """Tally one send result ('ok' | 'blocked' | 'error')."""
        if result == "ok":
            self.ok += 1
        elif result == "blocked":
            self.blocked += 1
        else:
            self.error += 1

    def log(self, kind):
        """Emit a single aggregate line for this cycle (skips empty cycles)."""
        if self.sent == 0:
            return
        _log.info(
            "type=%s sent=%d ok=%d blocked=%d error=%d",
            kind, self.sent, self.ok, self.blocked, self.error,
        )


async def send_to_user(bot, user_id, text, kind, stats=None, **kwargs):
    """Send `text` to `user_id`, tolerating blocks/errors; returns the result.

    Result is one of 'ok' | 'blocked' | 'error'. Anomalies are logged per-line;
    successes are not (only the per-cycle aggregate via SendStats). A blocked or
    failed send never raises out of here, so a broadcast loop keeps going.
    """
    result = "ok"
    try:
        await bot.send_message(chat_id=user_id, text=text, **kwargs)
    except Forbidden:
        # User blocked the bot (or deactivated): stop bothering them this cycle.
        result = "blocked"
        _log.info("user=%s type=%s result=blocked", user_id, kind)
    except TelegramError as exc:
        result = "error"
        _log.info("user=%s type=%s result=error:%s", user_id, kind, exc)
    if stats is not None:
        stats.add(result)
    return result
