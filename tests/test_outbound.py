"""P5.1 — outbound send helper + active-user listing.

`send_to_user` must tolerate a user who blocked the bot (Forbidden) and generic
TelegramErrors without raising, so a broadcast loop keeps going; SendStats must
aggregate results. `list_active_users` returns approved users plus the owner.
telegram is stubbed so the test runs without PTB installed.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

# Stub telegram before importing the outbound helper (PTB isn't installed). Build
# a superset stub idempotently so it coexists with other tests' stubs regardless
# of run order: ensure the telegram module + the error classes this module needs.
tg = sys.modules.get("telegram") or types.ModuleType("telegram")
sys.modules["telegram"] = tg
# ReplyKeyboardMarkup stores .rows (test_owner_gating reads it) so the stubs stay
# compatible whichever test installs telegram first.
if not hasattr(tg, "ReplyKeyboardMarkup"):
    tg.ReplyKeyboardMarkup = type(
        "ReplyKeyboardMarkup", (),
        {"__init__": lambda self, rows=None, **k: setattr(self, "rows", rows)},
    )
for _n in ("ReplyKeyboardRemove", "Update", "InlineKeyboardButton", "InlineKeyboardMarkup"):
    if not hasattr(tg, _n):
        setattr(tg, _n, type(_n, (), {"__init__": lambda self, *a, **k: None}))

err = sys.modules.get("telegram.error") or types.ModuleType("telegram.error")
if not hasattr(err, "TelegramError"):
    err.TelegramError = type("TelegramError", (Exception,), {})
for _n in ("NetworkError", "RetryAfter", "TimedOut", "Forbidden", "InvalidToken"):
    if not hasattr(err, _n):
        setattr(err, _n, type(_n, (err.TelegramError,), {}))
sys.modules["telegram.error"] = err
tg.error = err

if "telegram.ext" not in sys.modules:
    ext = types.ModuleType("telegram.ext")
    ext.ContextTypes = type("C", (), {"DEFAULT_TYPE": object})
    ext.ConversationHandler = type("CH", (), {"END": -1})
    sys.modules["telegram.ext"] = ext

from telegram.error import Forbidden, TelegramError  # noqa: E402

from bot.common.outbound import SendStats, send_to_user  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    list_active_users,
    register_or_update_user,
    set_user_status,
)

OWNER, U1, U2 = 999, 111, 222


class _FakeBot:
    """Records sends; can be told to raise on specific user ids."""

    def __init__(self, raise_for=None):
        self.sent = []
        self.raise_for = raise_for or {}

    async def send_message(self, chat_id, text, **kwargs):
        exc = self.raise_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text))


def _run(coro):
    return asyncio.run(coro)


class SendToUserTest(unittest.TestCase):
    def test_ok_send(self):
        bot = _FakeBot()
        stats = SendStats()
        result = _run(send_to_user(bot, U1, "hi", "test", stats=stats))
        self.assertEqual(result, "ok")
        self.assertEqual(bot.sent, [(U1, "hi")])
        self.assertEqual((stats.ok, stats.blocked, stats.error), (1, 0, 0))

    def test_blocked_user_is_skipped_not_raised(self):
        bot = _FakeBot(raise_for={U1: Forbidden("blocked")})
        stats = SendStats()
        result = _run(send_to_user(bot, U1, "hi", "test", stats=stats))
        self.assertEqual(result, "blocked")
        self.assertEqual(bot.sent, [])  # nothing delivered
        self.assertEqual((stats.ok, stats.blocked, stats.error), (0, 1, 0))

    def test_generic_error_is_swallowed(self):
        bot = _FakeBot(raise_for={U1: TelegramError("boom")})
        stats = SendStats()
        result = _run(send_to_user(bot, U1, "hi", "test", stats=stats))
        self.assertEqual(result, "error")
        self.assertEqual((stats.ok, stats.blocked, stats.error), (0, 0, 1))

    def test_loop_continues_past_blocked(self):
        # A blocked first user must not stop delivery to the rest.
        bot = _FakeBot(raise_for={U1: Forbidden("blocked")})
        stats = SendStats()
        for uid in (U1, U2):
            _run(send_to_user(bot, uid, "hi", "test", stats=stats))
        self.assertEqual(bot.sent, [(U2, "hi")])
        self.assertEqual(stats.sent, 2)
        self.assertEqual((stats.ok, stats.blocked), (1, 1))


class ListActiveUsersTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old_owner = os.environ.get("OWNER_ID")
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore_owner)

    def _restore_owner(self):
        if self._old_owner is None:
            os.environ.pop("OWNER_ID", None)
        else:
            os.environ["OWNER_ID"] = self._old_owner

    def test_owner_included_even_without_row(self):
        self.assertEqual(list_active_users(self.db), [OWNER])

    def test_only_approved_plus_owner(self):
        register_or_update_user(self.db, U1, "u1", "U1")  # pending
        register_or_update_user(self.db, U2, "u2", "U2")
        set_user_status(self.db, U2, "approved")
        # U1 pending -> excluded; U2 approved -> included; owner always included.
        self.assertEqual(list_active_users(self.db), sorted({U2, OWNER}))

    def test_rejected_excluded(self):
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "rejected")
        self.assertEqual(list_active_users(self.db), [OWNER])


if __name__ == "__main__":
    unittest.main()
