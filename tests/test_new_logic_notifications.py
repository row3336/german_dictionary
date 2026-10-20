"""Personal preferences, Berlin calendar dispatch and notification settings UI."""

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from _telegram_stub import install
install()

from telegram.error import BadRequest, Forbidden, TimedOut
from bot.app_config import MENU, NOTIFICATION_SETTINGS_MENU, NOTIFICATION_SETTINGS_TIME, SETTINGS_MENU
from bot.handlers import settings as settings_handler
from bot.i18n import t
from bot.jobs import reminders as R
from bot.notifications import handlers as H, settings as N
from bot.storage import (
    add_word, fsrs_due_forecast, get_user_setting, init_db, register_or_update_user, select_due,
    set_user_interface_lang, set_user_setting, set_user_status,
)
from bot.ui.keyboards import _settings_menu_keys

OWNER, USER, OTHER = 999, 101, 202


def update(text="", uid=USER):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_message=msg, effective_user=SimpleNamespace(id=uid))


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(os.environ["TMPDIR"]) / f"notifications-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": str(OWNER)})
        env.start(); self.addCleanup(env.stop)
        init_db(self.db)
        for uid in (USER, OTHER):
            register_or_update_user(self.db, uid, "fixture", "Fixture")
            set_user_status(self.db, uid, "approved")
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.ctx = SimpleNamespace(bot=self.bot, user_data={})
        users = patch.object(R, "list_active_users", return_value=[USER])
        users.start(); self.addCleanup(users.stop)
        R._dispatcher_running = False

    def tick(self, token):
        now = datetime.fromisoformat(token).astimezone(N.BERLIN)
        with patch.object(R, "_now_berlin", return_value=now):
            asyncio.run(R._fsrs_afternoon_reminder_job(self.ctx))

    def enable_new(self, uid=USER, time="14:00"):
        N.set_preference(self.db, uid, "new_words", enabled=True, time=time)

    def test_defaults_do_not_create_or_overwrite_settings_and_keep_old_day_key(self):
        self.assertEqual(N.get_preference(self.db, USER, "quiz"), N.ReminderPreference(True, "18:00"))
        self.assertEqual(N.get_preference(self.db, USER, "new_words"), N.ReminderPreference(False, "14:00"))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_settings").fetchone()[0], 0)
        set_user_setting(self.db, USER, "quiz_reminder_time", "21:47")
        set_user_setting(self.db, USER, "quiz_reminder_enabled", "false")
        set_user_setting(self.db, USER, "fsrs_afternoon_reminder_date", "2026-10-20")
        self.assertEqual(N.get_preference(self.db, USER, "quiz"), N.ReminderPreference(False, "21:47"))
        self.assertEqual(get_user_setting(self.db, USER, "fsrs_afternoon_reminder_date"), "2026-10-20")
        self.assertEqual(N.get_preference(self.db, OTHER, "quiz").time, "18:00")

    def test_strict_time_rejects_bad_values_without_changing_personal_settings(self):
        for value in ("9:00", "24:00", "12:60", "-1:00", "18:00:00", " 18:00", "１２:００", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    N.set_preference(self.db, USER, "quiz", time=value)
        for value in ("00:00", "23:59", "09:00"):
            self.assertTrue(N.valid_time(value))
        self.assertEqual(N.get_preference(self.db, USER, "quiz").time, "18:00")

    def test_new_start_fact_is_atomic_and_records_berlin_assignment_day(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("BEGIN")
            N.record_learning_new_start(conn, USER, "2026-10-20T22:30:00Z")
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT value FROM user_settings WHERE key=?", (N.LEARNING_NEW_START_KEY,)).fetchone()[0], "2026-10-21")
            conn.rollback()
        self.assertIsNone(get_user_setting(self.db, USER, N.LEARNING_NEW_START_KEY))

    def test_quiz_count_is_twelve_unfiltered_written_words_not_five_or_phrases_or_oral_due(self):
        for index in range(12):
            add_word(self.db, {"id": f"w{index}", "type": "nomen", "article": "das",
                "german": f"Wort{index}", "russian": f"слово{index}", "topic": "one" if index < 5 else "two"})
        add_word(self.db, {"id": "p", "type": "phrase", "german": "Eine Phrase", "russian": "фраза"})
        add_word(self.db, {"id": "oral", "type": "verb", "german": "gehen", "infinitive": "gehen", "russian": "идти"})
        with closing(sqlite3.connect(self.db)) as conn:
            for index in range(12):
                conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES(?,?,?,0,'2020-01-01')", (USER, f"w{index}", "ru_de"))
                conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES(?,?,?,0,1,2.5,'2020-01-01')", (USER, f"w{index}", "ru_de"))
            for wid, mode, due in (("p", "phrase_ru_de", "2020-01-01"), ("oral", "de_ru", "2020-01-01"), ("oral", "ru_de", "2099-01-01")):
                conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES(?,?,?,0,'2020-01-01')", (USER, wid, mode))
                conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES(?,?,?,0,1,2.5,?)", (USER, wid, mode, due))
            conn.commit()
        day = datetime(2026, 10, 20).date()
        self.assertEqual(len(select_due(self.db, USER, "ru_de", topics=["one"], today=day, exclude_correct_today=True)), 5)
        self.assertEqual(R._count_quiz_due(self.db, USER, day), 12)
        self.tick("2026-10-20T18:00:00+02:00")
        self.assertIn("12", self.bot.send_message.call_args.kwargs["text"])
        self.assertEqual(self.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "notify:quiz")

    def test_zero_due_does_not_mark_day_and_later_readiness_is_sent_once(self):
        with patch.object(R, "_count_quiz_due", return_value=0):
            self.tick("2026-10-20T19:00:00+02:00")
        self.bot.send_message.assert_not_called()
        self.assertIsNone(get_user_setting(self.db, USER, "fsrs_afternoon_reminder_date"))
        with patch.object(R, "_count_quiz_due", return_value=3):
            self.tick("2026-10-20T19:01:00+02:00")
            self.tick("2026-10-20T19:02:00+02:00")
        self.bot.send_message.assert_awaited_once()

    def test_spring_dst_fires_after_missing_local_time_and_autumn_dedups_repeated_hour(self):
        N.set_preference(self.db, USER, "quiz", enabled=False)
        self.enable_new(time="02:30")
        self.tick("2026-03-29T00:59:00+00:00")
        self.bot.send_message.assert_not_called()
        self.tick("2026-03-29T01:00:00+00:00")
        self.bot.send_message.assert_awaited_once()
        self.tick("2026-10-25T00:30:00+00:00")
        self.tick("2026-10-25T01:30:00+00:00")
        self.assertEqual(self.bot.send_message.await_count, 2)

    def test_old_active_learning_does_not_suppress_new_words_but_today_start_does(self):
        N.set_preference(self.db, USER, "quiz", enabled=False)
        self.enable_new()
        add_word(self.db, {"id": "old", "type": "verb", "german": "gehen", "infinitive": "gehen", "russian": "идти"})
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO learning_items(user_id,word_id,batch_id,next_due,status) VALUES(?,?,?,'2099-01-01','active')", (USER, "old", "old-batch"))
            conn.commit()
        self.tick("2026-10-20T14:00:00+02:00")
        self.bot.send_message.assert_awaited_once()
        with closing(sqlite3.connect(self.db)) as conn:
            N.record_learning_new_start(conn, USER, "2026-10-21T10:00:00Z")
            conn.commit()
        self.tick("2026-10-21T18:00:00+02:00")
        self.assertEqual(self.bot.send_message.await_count, 1)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE learning_items SET status='completed' WHERE user_id=?", (USER,))
            conn.commit()
        self.tick("2026-10-21T18:01:00+02:00")
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.tick("2026-10-22T14:00:00+02:00")
        self.assertEqual(self.bot.send_message.await_count, 2)

    def test_failed_send_does_not_mark_day_and_success_marks_after_db_is_unlocked(self):
        async def send(**kwargs):
            # A separate writer can enter while Telegram is awaited.
            with closing(sqlite3.connect(self.db, timeout=0)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            raise TimedOut("synthetic")
        self.bot.send_message.side_effect = send
        with patch.object(R, "_count_quiz_due", return_value=1):
            self.tick("2026-10-20T18:00:00+02:00")
            self.assertIsNone(get_user_setting(self.db, USER, "fsrs_afternoon_reminder_date"))
            self.bot.send_message.side_effect = Forbidden("synthetic blocked user")
            self.tick("2026-10-20T18:00:30+02:00")
            self.assertIsNone(get_user_setting(self.db, USER, "fsrs_afternoon_reminder_date"))
            self.bot.send_message.side_effect = None
            self.tick("2026-10-20T18:01:00+02:00")
        self.assertEqual(get_user_setting(self.db, USER, "fsrs_afternoon_reminder_date"), "2026-10-20")

    def test_recheck_prevents_send_if_disabled_or_new_start_appears_before_delivery(self):
        def disable(db, uid, day):
            N.set_preference(db, uid, "quiz", enabled=False)
            return 12
        with patch.object(R, "_count_quiz_due", side_effect=disable):
            self.tick("2026-10-20T18:00:00+02:00")
        self.bot.send_message.assert_not_called()
        self.enable_new()
        def set_start(uid):
            with closing(sqlite3.connect(self.db)) as conn:
                N.record_learning_new_start(conn, uid, "2026-10-20T10:00:00Z")
                conn.commit()
            return "en"
        with patch.object(R, "_user_interface_lang", side_effect=set_start):
            self.tick("2026-10-20T18:01:00+02:00")
        self.bot.send_message.assert_not_called()

    def test_overlapping_ticks_cannot_send_twice(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def send(**kwargs):
                entered.set(); await release.wait()
            self.bot.send_message.side_effect = send
            task = asyncio.create_task(R._fsrs_afternoon_reminder_job(self.ctx))
            await entered.wait()
            await R._fsrs_afternoon_reminder_job(self.ctx)
            release.set(); await task
        now = datetime.fromisoformat("2026-10-20T18:00:00+02:00")
        with patch.object(R, "_count_quiz_due", return_value=1), patch.object(R, "_now_berlin", return_value=now):
            asyncio.run(scenario())
        self.bot.send_message.assert_awaited_once()

    def test_permanent_and_inline_settings_show_and_update_only_the_effective_user(self):
        upd = update(t("en", "notifications_title"))
        self.assertEqual(asyncio.run(settings_handler.settings_menu_choice(upd, self.ctx)), NOTIFICATION_SETTINGS_MENU)
        self.assertEqual(asyncio.run(H.notification_menu_input(update(t("en", "notification_new_words")), self.ctx)), NOTIFICATION_SETTINGS_MENU)
        asyncio.run(H.notification_menu_input(update(t("en", "notification_enable")), self.ctx))
        self.assertTrue(N.get_preference(self.db, USER, "new_words").enabled)
        self.assertFalse(N.get_preference(self.db, OTHER, "new_words").enabled)
        self.assertEqual(asyncio.run(H.notification_menu_input(update(t("en", "notification_change_time")), self.ctx)), NOTIFICATION_SETTINGS_TIME)
        for invalid in ("24:00", " 16:45", "16:45 ", "9:00"):
            self.assertEqual(asyncio.run(H.notification_time_input(update(invalid), self.ctx)), NOTIFICATION_SETTINGS_TIME)
            self.assertEqual(N.get_preference(self.db, USER, "new_words").time, "14:00")
        asyncio.run(H.notification_time_input(update("16:45"), self.ctx))
        cb = update()
        cb.callback_query = SimpleNamespace(data="notify:new_words", from_user=SimpleNamespace(id=OTHER), answer=AsyncMock())
        asyncio.run(H.notification_settings_callback(cb, self.ctx))
        self.assertIn("16:45", cb.message.reply_text.call_args.args[0])
        self.assertEqual(N.get_preference(self.db, OTHER, "new_words").time, "14:00")

    def test_all_ui_languages_have_permanent_entry_and_berlin_time_and_correct_new_text(self):
        for uid in (OWNER, USER):
            for lang in ("en", "ru", "uk"):
                set_user_interface_lang(self.db, uid, lang)
                self.assertIn(t(lang, "notifications_title"), [button for row in _settings_menu_keys(uid) for button in row])
                upd = update(uid=uid)
                asyncio.run(H.show_notification_settings(upd, self.ctx, "quiz"))
                self.assertIn(t(lang, "notification_berlin"), upd.message.reply_text.call_args.args[0])
        self.assertEqual(t("ru", "reminder_new_words"), "⏰ Сегодня не было изучения новых слов 😔\nОткрой Learning и добавь слова для изучения.")

    def test_unapproved_and_invalid_callback_cannot_modify_settings(self):
        set_user_status(self.db, OTHER, "pending")
        ctx = SimpleNamespace(user_data={"notification_kind": "quiz"})
        self.assertEqual(asyncio.run(H.notification_time_input(update("01:00", OTHER), ctx)), MENU)
        self.assertEqual(N.get_preference(self.db, OTHER, "quiz").time, "18:00")
        upd = update()
        upd.callback_query = SimpleNamespace(data="notify:quiz:202", answer=AsyncMock())
        self.assertEqual(asyncio.run(H.notification_settings_callback(upd, ctx)), MENU)

    def test_back_navigation_and_expired_callback_keep_settings_working(self):
        upd = update()
        upd.callback_query = SimpleNamespace(data="notify:quiz", answer=AsyncMock(side_effect=BadRequest("expired")))
        self.assertEqual(asyncio.run(H.notification_settings_callback(upd, self.ctx)), NOTIFICATION_SETTINGS_MENU)
        self.assertEqual(asyncio.run(H.notification_menu_input(update(t("en", "back")), self.ctx)), NOTIFICATION_SETTINGS_MENU)
        self.assertEqual(asyncio.run(H.notification_menu_input(update(t("en", "back")), self.ctx)), SETTINGS_MENU)

    def test_restart_catches_only_current_day_and_honors_existing_legacy_marker(self):
        set_user_setting(self.db, USER, "fsrs_afternoon_reminder_date", "2026-10-19")
        with patch.object(R, "_count_quiz_due", return_value=4):
            self.tick("2026-10-23T19:00:00+02:00")
            self.assertEqual(get_user_setting(self.db, USER, "fsrs_afternoon_reminder_date"), "2026-10-23")
            R._dispatcher_running = False  # a process restart retains only database dedup
            self.tick("2026-10-23T22:00:00+02:00")
        self.bot.send_message.assert_awaited_once()

    def test_dispatch_uses_personal_times_and_defaults_new_off_before_first_message(self):
        with patch.object(R, "_count_quiz_due", return_value=2):
            self.tick("2026-10-20T14:00:00+02:00")
        self.bot.send_message.assert_not_called()
        N.set_preference(self.db, USER, "quiz", time="21:00")
        N.set_preference(self.db, OTHER, "quiz", enabled=False)
        self.enable_new(OTHER, "14:30")
        with patch.object(R, "list_active_users", return_value=[USER, OTHER]), patch.object(R, "_count_quiz_due", return_value=2):
            self.tick("2026-10-20T18:00:00+02:00")
            self.assertEqual([call.kwargs["chat_id"] for call in self.bot.send_message.call_args_list], [OTHER])
            self.tick("2026-10-20T21:00:00+02:00")
        self.assertEqual([call.kwargs["chat_id"] for call in self.bot.send_message.call_args_list], [OTHER, USER])
        self.assertIsNone(get_user_setting(self.db, USER, "new_words_reminder_date"))

    def test_legacy_future_phrase_forecast_fix_is_preserved_separately_from_new_word_count(self):
        # The f390ab2 scenario is retained, not reimplemented by the new dispatcher.
        add_word(self.db, {"id": "future-phrase", "type": "phrase", "german": "Eine Phrase", "russian": "фраза"})
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES(?,?,?,1,1,2.5,'2099-01-01')",
                         (USER, "future-phrase", "phrase_ru_de"))
            conn.commit()
        self.assertEqual(fsrs_due_forecast(self.db, USER, "ru_de")["total"], 0)
        self.assertEqual(R._count_quiz_due(self.db, USER, datetime(2026, 10, 20).date()), 0)

    def test_scheduler_keeps_continuation_and_installs_one_minute_dispatcher(self):
        removals = []
        queue = SimpleNamespace(get_jobs_by_name=lambda name: [SimpleNamespace(schedule_removal=lambda: removals.append(name))],
                                run_repeating=lambda *args, **kwargs: calls.append((args, kwargs)))
        calls = []
        R._configure_learning_reminders(SimpleNamespace(job_queue=queue))
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][0][0], R._learning_reminder_job)
        self.assertIs(calls[1][0][0], R._fsrs_afternoon_reminder_job)
        self.assertEqual([item[1]["interval"] for item in calls], [60, 60])
        self.assertEqual(len(removals), 2)


if __name__ == "__main__":
    unittest.main()
