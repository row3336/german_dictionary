"""P5.3 — per-user retention broadcast.

Each active user gets their OWN daily retention numbers in their own language,
once per day (per-user dedup); the night window is drained in small batches per
tick; an empty-data day still sends a localized 'no data' line. Module functions
are monkeypatched so no full quiz-event fixture is needed; telegram is stubbed.
"""

import asyncio
import datetime as _dt
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


class _FrozenDatetime:
    """Stand-in for reports.datetime so .now(tz) is deterministic (bug #22:
    the job branches on now.weekday(), so tests must pin the day of week)."""

    def __init__(self, value):
        self._value = value

    def now(self, tz=None):
        return self._value


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
            "get_daily_first_touch_retention", "get_weekly_first_touch_retention",
            "count_learning_done_in_week", "_fsrs_report_hour_utc",
            "_RETENTION_BATCH_PER_TICK", "datetime",
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
        # Per-user weekly numbers: U1 has data, U2 empty, U3 has data.
        self._weekly = {
            U1: {"week_start": "2026-06-15", "week_end": "2026-06-21", "total": 50, "correct": 47, "rate": 94.0},
            U2: {"week_start": "2026-06-15", "week_end": "2026-06-21", "total": 0, "correct": 0, "rate": 0.0},
            U3: {"week_start": "2026-06-15", "week_end": "2026-06-21", "total": 20, "correct": 14, "rate": 70.0},
        }
        R.get_weekly_first_touch_retention = (
            lambda db, user_id, week_start=None, modes=None: self._weekly[user_id]
        )
        R.count_learning_done_in_week = lambda db, user_id, week_start=None: 5
        # Bug #22: pin the clock to a NON-Monday so the daily-path tests don't also
        # trigger the Monday weekly broadcast (its functions are stubbed above, but
        # the day of week must be deterministic regardless of when the suite runs).
        self._frozen = _dt.datetime(2026, 6, 24, 12, 0, tzinfo=_dt.timezone.utc)  # Wednesday
        R.datetime = _FrozenDatetime(self._frozen)

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
        today = self._frozen.date().isoformat()
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

    def test_weekly_on_monday(self):
        # On Monday each user gets BOTH daily and weekly (bug #22 regression guard:
        # the weekly path used to be unstubbed -> the suite went red every Monday).
        R.datetime = _FrozenDatetime(_dt.datetime(2026, 6, 29, 12, 0, tzinfo=_dt.timezone.utc))  # Monday
        bot = _FakeBot()
        _run(R._retention_broadcast_job(_Ctx(bot)))
        self.assertEqual(len(bot.sent), 6)  # 3 users x (daily + weekly)
        weekly = {cid: text for cid, text in bot.sent if "#weekly" in text}
        self.assertEqual(set(weekly), {U1, U2, U3})
        self.assertIn("47/50", weekly[U1])    # U1 ru weekly numbers
        self.assertIn("no data", weekly[U2])  # U2 en empty weekly -> localized no-data
        self.assertIn("14/20", weekly[U3])    # U3 uk weekly numbers

    def test_window_closed_before_report_hour(self):
        # Frozen hour is 12 (< 23) -> window closed -> nothing sent.
        R._fsrs_report_hour_utc = lambda db: 23
        bot = _FakeBot()
        _run(R._retention_broadcast_job(_Ctx(bot)))
        self.assertEqual(bot.sent, [])


class RetentionZoneTest(unittest.TestCase):
    """Bug #23: five directional zones replace the old catch-all 'outside'.

    Thresholds: <70 overload / 70-80 below_optimal / 80-90 optimal /
    90-95 slightly_easy / >95 too_easy. We pin every boundary so a future tweak
    can't silently fold two zones together (which is the bug we are fixing)."""

    # (rate, expected i18n key) across the boundaries owner asked to verify.
    CASES = [
        (0.0, "report_zone_overload"),
        (69.9, "report_zone_overload"),
        (70.0, "report_zone_below_optimal"),
        (79.9, "report_zone_below_optimal"),
        (80.0, "report_zone_optimal"),
        (90.0, "report_zone_optimal"),
        (90.2, "report_zone_slightly_easy"),  # the value from the live report
        (95.0, "report_zone_slightly_easy"),
        (95.1, "report_zone_too_easy"),
        (100.0, "report_zone_too_easy"),
    ]

    def test_zone_boundaries_all_languages(self):
        for lang in ("en", "ru", "uk"):
            for rate, key in self.CASES:
                with self.subTest(lang=lang, rate=rate):
                    self.assertEqual(R._retention_zone(lang, rate), R.t(lang, key))

    def test_old_catch_all_key_removed(self):
        # The directionless keys must be gone so nothing falls back to them.
        for lang in ("en", "ru", "uk"):
            self.assertEqual(R.t(lang, "report_zone_outside"), "report_zone_outside")
            self.assertEqual(R.t(lang, "report_zone_easy"), "report_zone_easy")

    def test_zone_appears_in_daily_and_weekly_text(self):
        # One function feeds both reports; a slightly-easy rate must surface the
        # directional label (not the old "outside") in each.
        retention = {
            "date": "2026-06-24", "week_start": "2026-06-22", "week_end": "2026-06-28",
            "total": 50, "correct": 45, "rate": 90.2,
        }
        daily = R._format_retention_daily_text(retention, "en")
        weekly = R._format_retention_weekly_text(retention, 3, "en")
        zone = R.t("en", "report_zone_slightly_easy")
        self.assertIn(zone, daily)
        self.assertIn(zone, weekly)


if __name__ == "__main__":
    unittest.main()
