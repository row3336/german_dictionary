"""Retained storage aggregates and role-aware statistics navigation.

The legacy storage activity fields stay compatible. The owner's displayed FSRS
word counter and exact-mode breakdown have dedicated new_logic coverage.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:  # menu.py -> quiz.py imports bot.main locally
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup  # noqa: E402
from telegram.error import TelegramError  # noqa: E402

import bot.handlers.owner_stats as OS  # noqa: E402
from bot.handlers.menu import menu_choice  # noqa: E402
from bot.i18n import TEXTS, t  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    list_user_activity,
    log_quiz_event,
    register_or_update_user,
    set_user_status,
)

OWNER, U1, U2, U3 = 999, 111, 222, 333


def _run(coro):
    return asyncio.run(coro)


class ListUserActivityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1', 'nomen', 'Haus', 'дом')")
        conn.commit()
        conn.close()

    def test_counts_and_isolation(self):
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "approved")
        register_or_update_user(self.db, U2, "u2", "U2")
        set_user_status(self.db, U2, "approved")
        for _ in range(3):
            log_quiz_event(self.db, user_id=U1, word_id="w1", mode="ru_de", is_correct=True)
        log_quiz_event(self.db, user_id=U2, word_id="w1", mode="ru_de", is_correct=False)

        items = {row["user_id"]: row for row in list_user_activity(self.db)}
        self.assertEqual(items[U1]["quiz_events_count"], 3)
        self.assertEqual(items[U2]["quiz_events_count"], 1)
        # No cross-user leakage of the counters.
        self.assertEqual(items[U1]["learning_done_count"], 0)

    def test_newcomer_with_no_activity_shows_zeros(self):
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "approved")
        items = list_user_activity(self.db)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quiz_events_count"], 0)
        self.assertEqual(items[0]["learning_done_count"], 0)

    def test_pending_and_blocked_users_excluded(self):
        register_or_update_user(self.db, U1, "u1", "U1")  # left pending
        register_or_update_user(self.db, U2, "u2", "U2")
        set_user_status(self.db, U2, "rejected")
        register_or_update_user(self.db, U3, "u3", "U3")
        set_user_status(self.db, U3, "approved")
        ids = [row["user_id"] for row in list_user_activity(self.db)]
        self.assertEqual(ids, [U3])

    def test_sorted_by_activity_descending(self):
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "approved")
        register_or_update_user(self.db, U2, "u2", "U2")
        set_user_status(self.db, U2, "approved")
        register_or_update_user(self.db, U3, "u3", "U3")
        set_user_status(self.db, U3, "approved")
        for _ in range(5):
            log_quiz_event(self.db, user_id=U2, word_id="w1", mode="ru_de", is_correct=True)
        for _ in range(1):
            log_quiz_event(self.db, user_id=U1, word_id="w1", mode="ru_de", is_correct=True)
        ids = [row["user_id"] for row in list_user_activity(self.db)]
        self.assertEqual(ids, [U2, U1, U3])  # 5, 1, 0

    def test_completed_and_skipped_learning_both_count(self):
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "approved")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO learning_items (user_id, word_id, batch_id, next_due, status) "
            "VALUES (?, 'w1', 'b1', '2020-01-01', 'completed')",
            (U1,),
        )
        conn.execute(
            "INSERT INTO learning_items (user_id, word_id, batch_id, next_due, status) "
            "VALUES (?, 'w1b', 'b1', '2020-01-01', 'skipped')",
            (U1,),
        )
        conn.execute(
            "INSERT INTO words (id, type, german, russian) VALUES ('w1b', 'nomen', 'Auto', 'машина')"
        )
        conn.commit()
        conn.close()
        items = list_user_activity(self.db)
        self.assertEqual(items[0]["learning_done_count"], 2)


class FormatUsersTextTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_empty_state_is_localized(self):
        for lang in ("en", "ru", "uk"):
            self.assertEqual(OS._format_users_text(lang), t(lang, "stat_users_empty"))

    def test_renders_clickable_line_with_counts(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        set_user_status(self.db, U1, "approved")
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1', 'nomen', 'Haus', 'дом')")
        conn.commit()
        conn.close()
        log_quiz_event(self.db, user_id=U1, word_id="w1", mode="ru_de", is_correct=True)
        text = OS._format_users_text("en")
        # a user WITH a @username renders as a plain @handle (Telegram auto-links
        # it) — no wrapped tg:// link, so no backslash leaks under legacy Markdown
        self.assertIn("@alice", text)
        self.assertNotIn(f"tg://user?id={U1}", text)
        self.assertIn("— 1 (0)", text)

    def test_username_with_underscore_is_escaped_for_legacy_markdown(self):
        register_or_update_user(self.db, U1, "Row_xyz2", "Aleksey")
        set_user_status(self.db, U1, "approved")
        text = OS._format_users_text("en")
        self.assertIn("@Row\\_xyz2", text)

    def test_username_less_user_falls_back_to_tg_link(self):
        register_or_update_user(self.db, U1, "", "Тамта")
        set_user_status(self.db, U1, "approved")
        text = OS._format_users_text("en")
        self.assertIn(f"[Тамта](tg://user?id={U1})", text)


class OwnerStatKeyboardTest(unittest.TestCase):
    def test_owner_has_progress_users_and_word_types(self):
        markup = OS._owner_stat_keyboard()
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual([button.text for row in markup.inline_keyboard for button in row],
                         ["progress", "users", "word types"])


# --- menu.py "stat" action: inline toggle for owner only ------------------------

class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return types.SimpleNamespace(message_id=1)


class _Update:
    def __init__(self, uid, text=""):
        self.message = _Msg(text)
        self.effective_user = types.SimpleNamespace(id=uid, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=uid)


class _Ctx:
    def __init__(self):
        self.user_data = {}


class StatScreenOwnerVsGuestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "approved")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_owner_gets_inline_toggle(self):
        update = _Update(OWNER, t("en", "stat"))
        _run(menu_choice(update, _Ctx()))
        _, kwargs = update.message.calls[0]
        self.assertIsInstance(kwargs.get("reply_markup"), InlineKeyboardMarkup)

    def test_guest_gets_progress_and_dictionary_inline_views(self):
        update = _Update(U1, t("en", "stat"))
        _run(menu_choice(update, _Ctx()))
        _, kwargs = update.message.calls[0]
        markup = kwargs.get("reply_markup")
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual([button.text for row in markup.inline_keyboard for button in row],
                         ["progress", "word types in db"])


# --- stat_toggle_callback: owner-gated edit-in-place ----------------------------

class _FakeQuery:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = types.SimpleNamespace(id=uid)
        self.answers = []
        self.edits = []  # (text, kwargs)

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


class _FakeCbUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = query.from_user


class _FailingQuery(_FakeQuery):
    async def edit_message_text(self, text, **kw):
        raise TelegramError("malformed entity")


class StatToggleCallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        register_or_update_user(self.db, U1, "u1", "U1")
        set_user_status(self.db, U1, "approved")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_owner_switches_to_users_view(self):
        query = _FakeQuery("stat:users", OWNER)
        _run(OS.stat_toggle_callback(_FakeCbUpdate(query), None))
        self.assertEqual(len(query.edits), 1)
        text, kw = query.edits[0]
        self.assertNotIn("```", text)  # plain Markdown, not a code block (spec #11)
        self.assertIsInstance(kw.get("reply_markup"), InlineKeyboardMarkup)

    def test_owner_switches_back_to_progress_view(self):
        query = _FakeQuery("stat:progress", OWNER)
        _run(OS.stat_toggle_callback(_FakeCbUpdate(query), None))
        text, kw = query.edits[0]
        self.assertIn("```text", text)
        self.assertIsInstance(kw.get("reply_markup"), InlineKeyboardMarkup)

    def test_non_owner_callback_is_ignored(self):
        query = _FakeQuery("stat:users", U1)
        _run(OS.stat_toggle_callback(_FakeCbUpdate(query), None))
        self.assertEqual(query.edits, [])
        self.assertEqual(len(query.answers), 1)

    def test_telegram_edit_error_is_logged(self):
        query = _FailingQuery("stat:users", OWNER)
        with self.assertLogs(OS.__name__, level="WARNING") as captured:
            _run(OS.stat_toggle_callback(_FakeCbUpdate(query), None))
        self.assertIn("Failed to edit owner statistics message", captured.output[0])


if __name__ == "__main__":
    unittest.main()
