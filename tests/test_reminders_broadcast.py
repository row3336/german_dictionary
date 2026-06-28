"""P5.2 — FSRS-afternoon reminder broadcasts to all users.

Each active user gets at most one reminder per day, in their own language, only
when they have due words; a user with an empty queue is skipped, and a repeat job
tick the same day does not re-send (per-user dedup). Module functions are
monkeypatched so we don't need a full FSRS due fixture; telegram is stubbed.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.jobs.reminders as R  # noqa: E402
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


class FsrsBroadcastTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old_env = {k: os.environ.get(k) for k in ("DB_PATH",)}
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore_env)

        # Patch module globals; restore on teardown.
        self._orig = {
            "_FSRS_REMINDER_HOUR_BERLIN": R._FSRS_REMINDER_HOUR_BERLIN,
            "list_active_users": R.list_active_users,
            "fsrs_due_forecast": R.fsrs_due_forecast,
            "_user_interface_lang": R._user_interface_lang,
            "_mode_l1_de": R._mode_l1_de,
        }
        self.addCleanup(self._restore_globals)
        # Pass the Berlin-hour gate regardless of wall clock.
        R._FSRS_REMINDER_HOUR_BERLIN = 0
        R.list_active_users = lambda db: [U1, U2, U3]
        self._due = {U1: 5, U2: 0, U3: 3}
        R.fsrs_due_forecast = lambda db, uid, mode: {"total": self._due[uid]}
        self._lang = {U1: "ru", U2: "en", U3: "uk"}
        R._user_interface_lang = lambda uid: self._lang[uid]
        R._mode_l1_de = lambda uid: "ru_de"

    def _restore_env(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _restore_globals(self):
        for k, v in self._orig.items():
            setattr(R, k, v)

    def test_broadcast_localized_and_skips_empty_queue(self):
        bot = _FakeBot()
        _run(R._fsrs_afternoon_reminder_job(_Ctx(bot)))
        sent = dict(bot.sent)
        # U2 has an empty queue -> no message.
        self.assertNotIn(U2, sent)
        self.assertIn(U1, sent)
        self.assertIn(U3, sent)
        # Localized + count substituted.
        self.assertIn("5", sent[U1])
        self.assertIn("Очередь", sent[U1])  # ru
        self.assertIn("3", sent[U3])
        self.assertIn("Черга", sent[U3])    # uk

    def test_dedup_no_resend_same_day(self):
        bot = _FakeBot()
        _run(R._fsrs_afternoon_reminder_job(_Ctx(bot)))
        first = list(bot.sent)
        # Dedup marker recorded for the users who got a message.
        today_marked = get_user_setting(self.db, U1, R._FSRS_REMINDER_DATE_KEY, "")
        self.assertTrue(today_marked)
        self.assertEqual(get_user_setting(self.db, U2, R._FSRS_REMINDER_DATE_KEY, ""), "")
        # Second tick the same day -> no new sends.
        _run(R._fsrs_afternoon_reminder_job(_Ctx(bot)))
        self.assertEqual(bot.sent, first)


if __name__ == "__main__":
    unittest.main()
