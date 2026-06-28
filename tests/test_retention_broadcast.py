"""P5.3 — per-user retention broadcast.

Each active user gets their OWN daily retention numbers in their own language,
once per day (per-user dedup); the night window is drained in small batches per
tick; an empty-data day still sends a localized 'no data' line. Module functions
are monkeypatched so no full quiz-event fixture is needed; telegram is stubbed.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.jobs.reports as R  # noqa: E402
from bot.storage import get_user_setting, init_db  # noqa: E402

U1, U2, U3 = 111, 222, 333


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _run(coro):
    return asyncio.run(coro)


class RetentionBroadcastTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old_env = {k: os.environ.get(k) for k in ("DB_PATH",)}
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore_env)

        self._orig = {k: getattr(R, k) for k in (
            "list_active_users", "_user_interface_lang", "_mode_l1_de", "_mode_de_l1",
            "get_daily_first_touch_retention", "_fsrs_report_hour_utc",
            "_RETENTION_BATCH_PER_TICK",
        )}
        self.addCleanup(self._restore_globals)
        R._fsrs_report_hour_utc = lambda db: 0  # window always open
        R.list_active_users = lambda db: [U1, U2, U3]
        self._lang = {U1: "ru", U2: "en", U3: "uk"}
        R._user_interface_lang = lambda uid: self._lang[uid]
        R._mode_l1_de = lambda uid: "ru_de"
        R._mode_de_l1 = lambda uid: "de_ru"
        # Per-user numbers: U1 has data, U2 empty, U3 has data.
        self._daily = {
            U1: {"date": "2026-06-27", "total": 10, "correct": 9, "rate": 90.0},
            U2: {"date": "2026-06-27", "total": 0, "correct": 0, "rate": 0.0},
            U3: {"date": "2026-06-27", "total": 4, "correct": 2, "rate": 50.0},
        }
        R.get_daily_first_touch_retention = (
            lambda db, user_id, target_date=None, modes=None: self._daily[user_id]
        )

    def _restore_env(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _restore_globals(self):
        for k, v in self._orig.items():
            setattr(R, k, v)

    def test_per_user_numbers_and_language(self):
        bot = _FakeBot()
        _run(R._retention_broadcast_job(_Ctx(bot)))
        sent = dict(bot.sent)
        self.assertEqual(set(sent), {U1, U2, U3})
        self.assertIn("Ретеншн", sent[U1])      # ru
        self.assertIn("9/10", sent[U1])          # U1's own numbers
        self.assertIn("оптимально", sent[U1])    # ru zone for 90%
        self.assertIn("Retention", sent[U2])     # en
        self.assertIn("no data", sent[U2])       # U2 empty -> localized no-data
        self.assertIn("Ретеншн", sent[U3])       # uk
        self.assertIn("2/4", sent[U3])           # U3's own numbers

    def test_dedup_no_resend_same_day(self):
        bot = _FakeBot()
        _run(R._retention_broadcast_job(_Ctx(bot)))
        first = list(bot.sent)
        today = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat()
        self.assertEqual(get_user_setting(self.db, U1, R.FSRS_REPORT_DATE_KEY, ""), today)
        _run(R._retention_broadcast_job(_Ctx(bot)))
        self.assertEqual(bot.sent, first)  # no re-send same day

    def test_night_window_batches_per_tick(self):
        # Batch size 2 -> first tick sends 2 users, second tick the remaining one.
        R._RETENTION_BATCH_PER_TICK = 2
        bot = _FakeBot()
        _run(R._retention_broadcast_job(_Ctx(bot)))
        self.assertEqual(len(bot.sent), 2)
        _run(R._retention_broadcast_job(_Ctx(bot)))
        self.assertEqual(len(bot.sent), 3)  # remaining user delivered next tick

    def test_window_closed_before_report_hour(self):
        R._fsrs_report_hour_utc = lambda db: 23  # effectively closed most of the day
        import datetime as _dt
        if _dt.datetime.now(_dt.timezone.utc).hour >= 23:
            self.skipTest("current UTC hour is inside the window")
        bot = _FakeBot()
        _run(R._retention_broadcast_job(_Ctx(bot)))
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
