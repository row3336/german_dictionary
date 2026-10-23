"""K14 S03–S06: installed PTB dispatcher with SQLite and a no-network bot."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


def actual_suite():
    # A separate interpreter keeps the real PTB independent of legacy test stubs.
    import asyncio
    import copy
    from contextlib import ExitStack
    from datetime import datetime, timezone
    import sqlite3
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch

    from telegram import CallbackQuery, Chat, Message, Update, User
    from telegram.ext import Application, ConversationHandler, ExtBot
    from telegram.request import BaseRequest

    from bot import main, storage
    from bot.app_config import ADD_EDIT, DB_SEARCH, DB_SEARCH_VIEW, MENU
    from bot.common import search_view as V
    from bot.common.quiz_resume import PUBLISHED_VIEW_KEY, RETURN_POINT_KEY
    from bot.handlers import db_search as D
    from bot.i18n import t

    NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
    UID, OWNER = 7, 999

    class NoNetworkRequest(BaseRequest):
        attempts = 0

        @property
        def read_timeout(self):
            return None

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def do_request(self, *args, **kwargs):
            type(self).attempts += 1
            raise AssertionError("network request attempted")

    class FakeBot(ExtBot):
        __slots__ = ("sent", "answered", "fail_at")

        def __init__(self):
            super().__init__("123456:synthetic", request=NoNetworkRequest(),
                             get_updates_request=NoNetworkRequest())
            object.__setattr__(self, "sent", [])
            object.__setattr__(self, "answered", [])
            object.__setattr__(self, "fail_at", None)

        async def initialize(self):
            self._bot_user = User(99, "Synthetic bot", True, username="synthetic_bot")

        async def shutdown(self):
            pass

        async def send_message(self, chat_id, text, **kwargs):
            if self.fail_at == len(self.sent):
                object.__setattr__(self, "fail_at", None)
                raise TimeoutError("synthetic uncertain send timeout")
            result = Message(100 + len(self.sent), NOW, Chat(chat_id, "private"),
                             from_user=self._bot_user, text=text, reply_markup=kwargs.get("reply_markup"))
            result.set_bot(self)
            self.sent.append((result, kwargs))
            return result

        async def answer_callback_query(self, callback_query_id, **kwargs):
            self.answered.append((callback_query_id, kwargs))
            return True

    class Builder:
        def __init__(self, app):
            self.app = app

        def __getattr__(self, name):
            return lambda *args, **kwargs: self

        def build(self):
            return self.app

    class AdapterTest(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            asyncio.get_running_loop().set_debug(False)
            self.stack = ExitStack()
            self.db = tempfile.mktemp(suffix="-k14-adapter.sqlite")
            self.stack.enter_context(patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": str(OWNER)}))
            storage.init_db(self.db)
            with sqlite3.connect(self.db) as conn:
                for uid in (UID, OWNER, 8):
                    conn.execute("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES(?,'approved','ru','ru')", (uid,))
            self.seed("a", "Tafel")
            self.seed("b", "See", article="der")
            self.seed("c", "See", article="die")
            self.seed("phrase", "die Tafel Schokolade", type="phrase")
            self.bot = FakeBot()
            self.app = Application.builder().bot(self.bot).job_queue(None).build()
            for name in ("load_dotenv", "_configure_logging", "require_supported_ptb", "_prepare_storage",
                         "_configure_learning_reminders", "_configure_fsrs_reports",
                         "_configure_access_reminders", "_configure_owner_digest"):
                self.stack.enter_context(patch.object(main, name))
            self.stack.enter_context(patch.object(main, "Application", SimpleNamespace(builder=lambda: Builder(self.app))))
            self.stack.enter_context(patch.object(main, "AIORateLimiter", return_value=object()))
            self.stack.enter_context(patch.object(main, "run_polling_safely"))
            main.main()
            self.conv = next(handler for handler in self.app.handlers[0] if isinstance(handler, ConversationHandler))
            self.errors = []

            async def capture_error(update, context):
                self.errors.append(context.error)

            self.app.error_handlers.clear()
            self.app.add_error_handler(capture_error)
            await self.app.initialize()
            self.ident = 1000
            self.conv._conversations[(UID, UID)] = DB_SEARCH

        async def asyncTearDown(self):
            self.assertEqual(self.errors, [])
            self.assertEqual(NoNetworkRequest.attempts, 0)
            await self.app.shutdown()
            self.stack.close()

        def seed(self, wid, german, **extra):
            data = {"id": wid, "german": german, "type": "nomen", "created_at": "2026-01-01",
                    "russian": "РУССКИЙ", "ukrainian": "УКРАЇНСЬКИЙ", "english": "ENGLISH", **extra}
            with sqlite3.connect(self.db) as conn:
                conn.execute(f"INSERT INTO words ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))

        def state(self, uid=UID):
            return self.conv._conversations.get((uid, uid))

        def data(self, uid=UID):
            return self.app.user_data[uid]

        def state_without_dispatch_transport(self):
            from bot.study.transport_guard import LAST_TRANSPORT
            # main tracks every fresh Update, including a rejected control.
            self.assertEqual(self.data()[LAST_TRANSPORT], self.ident)
            return copy.deepcopy({key: value for key, value in self.data().items() if key != LAST_TRANSPORT})

        def study_rows(self):
            with sqlite3.connect(self.db) as conn:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                          if any(part in r[0] for part in ("progress", "events", "delivery", "counter"))]
                return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}

        async def text(self, text, uid=UID, ident=None):
            self.ident += 1
            message = Message(self.ident, NOW, Chat(uid, "private"), from_user=User(uid, "Learner", False), text=text)
            message.set_bot(self.bot)
            update = Update(ident or self.ident, message=message)
            update.set_bot(self.bot)
            await self.app.process_update(update)
            self.assertEqual(self.errors, [])
            return update

        async def click(self, sent, uid=UID, data=None, message_id=None):
            self.ident += 1
            message = sent
            if message_id is not None:
                message = Message(message_id, NOW, sent.chat, from_user=self.bot._bot_user, text=sent.text)
                message.set_bot(self.bot)
            callback_data = data or sent.reply_markup.inline_keyboard[0][0].callback_data
            query = CallbackQuery(str(self.ident), User(uid, "Learner", False), "synthetic", message=message, data=callback_data)
            query.set_bot(self.bot)
            update = Update(self.ident, callback_query=query)
            update.set_bot(self.bot)
            self.assertIsNone(update.message)
            await self.app.process_update(update)
            self.assertEqual(self.errors, [])
            return update

        def cards(self):
            return [message for message, _ in self.bot.sent if message.reply_markup
                    and getattr(message.reply_markup, "inline_keyboard", None)
                    and message.reply_markup.inline_keyboard[0][0].text == t("ru", "clarify_fields")]

        def more(self):
            return next(message for message, _ in reversed(self.bot.sent)
                        if message.reply_markup and getattr(message.reply_markup, "inline_keyboard", None)
                        and message.reply_markup.inline_keyboard[0][0].text in (t("ru", "search_more"), t("ru", "search_retry")))

        async def test_s05_real_callback_first_card_opens_exact_b_and_fieldinput_rejects_c(self):
            data = self.data()
            data.update({"quiz_entry": {"id": "a"}, PUBLISHED_VIEW_KEY: {"word_id": "a", "opaque": [1]},
                         RETURN_POINT_KEY: {"visible_answer_id": "a"}, "written_frame": {"current": "a"},
                         "learning_frame": {"last_answer": {"word_id": "a"}}})
            before = copy.deepcopy(data)
            rows = self.study_rows()
            await self.text("See")
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            self.assertNotIn("quiz_edit_entry", data)
            b, c = self.cards()
            self.assertIn("id: b", b.text)
            self.assertIn("id: c", c.text)
            await self.click(b)
            self.assertEqual(self.state(), ADD_EDIT)
            self.assertEqual(data["quiz_edit_entry"]["id"], "b")
            for key, value in before.items():
                self.assertEqual(data[key], value, key)
            await self.text(t("ru", "my_translation"))
            self.assertEqual(data["edit_stage"], "enter_value")
            await self.click(c)
            self.assertEqual(self.state(), ADD_EDIT)
            self.assertEqual(data["edit_stage"], "enter_value")
            self.assertEqual(data["quiz_edit_entry"]["id"], "b")
            await self.text("личный перевод Б")
            self.assertEqual(storage.get_user_word_media(self.db, UID, "b")["russian"], "личный перевод Б")
            await self.text(t("ru", "back"))
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            self.assertEqual(self.study_rows(), rows)
            for key, value in before.items():
                self.assertEqual(data[key], value, key)

        async def test_s03_all_27_exact_pages_snapshot_and_stale_more(self):
            for i in range(27):
                self.seed(f"x{i:02}", "same")
            before = self.study_rows()
            await self.text("same")
            self.assertEqual(len(self.cards()), 5)
            old_more = self.more()
            self.seed("new", "same")
            await self.click(old_more)
            self.assertEqual(len(self.cards()), 10)
            await self.click(old_more)
            self.assertEqual(len(self.cards()), 10)
            while self.data()[V.SEARCH_KEY]["page_cursor"] < 27:
                await self.click(self.more())
            self.assertEqual(len(self.cards()), 27)
            self.assertNotIn("new", self.data()[V.SEARCH_KEY]["ordered_ids"])
            self.assertEqual(self.study_rows(), before)

        async def test_s04_retired_labels_without_legacy_map_preserve_live_query_in_all_languages(self):
            await self.text("See")
            card = self.cards()[0]
            before = self.state_without_dispatch_transport()
            rows = self.study_rows()
            self.assertNotIn("db_search_exact_variants", before)
            controls = ["vor [local, +D/A]", "vor [temporal, +Dativ]", "vor [+Genitiv]", "vor [modal]", "vor []"]
            controls += [t(lang, "db_search_back_to_variants") for lang in ("ru", "uk", "en")]
            for lang in ("ru", "uk", "en"):
                storage.set_user_interface_lang(self.db, UID, lang)
                with patch.object(D, "search_entries", side_effect=AssertionError("stale label performed lookup")), patch.object(
                        V, "new_snapshot", side_effect=AssertionError("stale label replaced query")):
                    for control in controls:
                        await self.text(control)
                        self.assertEqual(self.state(), DB_SEARCH_VIEW)
                        self.assertEqual(self.state_without_dispatch_transport(), before)
                        self.assertEqual(self.bot.sent[-1][0].text, t(lang, "search_stale"))
                    await self.click(card, data="ds:retired-semantic-control")
                    self.assertEqual(self.state(), DB_SEARCH_VIEW)
                    self.assertEqual(self.state_without_dispatch_transport(), before)
            self.assertEqual(self.study_rows(), rows)
            self.seed("bracket-query", "Hinweis [in Klammern]", type="phrase")
            await self.text("Hinweis [in Klammern]")
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            self.assertEqual(self.data()[V.SEARCH_KEY]["ordered_ids"], ["bracket-query"])
            self.assertNotEqual(self.data()[V.SEARCH_KEY]["query_id"], before[V.SEARCH_KEY]["query_id"])

        async def test_s04_retired_back_and_labels_never_mutate_search_edit_or_study(self):
            study = {"quiz_entry": {"id": "c", "marker": "hidden-study"},
                     "quiz_last_answered_entry": {"id": "a", "marker": "visible-study"},
                     PUBLISHED_VIEW_KEY: {"visible": "a", "hidden": "c"},
                     RETURN_POINT_KEY: {"word_id": "a"},
                     "learning_frame": {"last_answer": {"word_id": "a"}, "current": "c"}}
            self.data().update(copy.deepcopy(study))
            await self.text("See")
            b, c = self.cards()
            await self.click(b)
            self.assertEqual(self.state(), ADD_EDIT)
            rows = self.study_rows()
            controls = [t(lang, "db_search_back_to_variants") for lang in ("ru", "uk", "en")]
            controls.append("vor [local, +D/A]")
            for stage in ("pick_field", "enter_value"):
                if stage == "enter_value":
                    storage.set_user_interface_lang(self.db, UID, "ru")
                    await self.text(t("ru", "my_translation"))
                self.assertEqual(self.data()["edit_stage"], stage)
                before = self.state_without_dispatch_transport()
                for lang in ("ru", "uk", "en"):
                    storage.set_user_interface_lang(self.db, UID, lang)
                    with patch.object(D, "search_entries", side_effect=AssertionError("stale edit performed lookup")), patch.object(
                            V, "new_snapshot", side_effect=AssertionError("stale edit replaced query")), patch(
                            "bot.search.read_search_entry", side_effect=AssertionError("stale edit refreshed target")):
                        for control in controls:
                            await self.text(control)
                            self.assertEqual(self.state(), ADD_EDIT)
                            self.assertEqual(self.state_without_dispatch_transport(), before)
                        await self.click(c, data="ds:retired-back-control")
                        self.assertEqual(self.state(), ADD_EDIT)
                        self.assertEqual(self.state_without_dispatch_transport(), before)
            self.assertEqual(self.study_rows(), rows)
            for key, value in study.items():
                self.assertEqual(self.data()[key], value)
            await self.text("personal [translation]")
            self.assertEqual(storage.get_user_word_media(self.db, UID, "b")["russian"], "personal [translation]")
            self.assertEqual(self.data()["quiz_edit_entry"]["id"], "b")
            self.assertEqual(self.state(), ADD_EDIT)
            for key, value in study.items():
                self.assertEqual(self.data()[key], value)

        async def test_s04_page_click_during_field_input_does_not_advance_or_retarget(self):
            for i in range(7):
                self.seed(f"x{i}", "same")
            await self.text("same")
            more = self.more()
            await self.click(self.cards()[0])
            await self.text(t("ru", "my_translation"))
            target = self.data()["quiz_edit_entry"]["id"]
            cursor = self.data()[V.SEARCH_KEY]["page_cursor"]
            await self.click(more)
            self.assertEqual(self.state(), ADD_EDIT)
            self.assertEqual(self.data()["edit_stage"], "enter_value")
            self.assertEqual(self.data()["quiz_edit_entry"]["id"], target)
            self.assertEqual(self.data()[V.SEARCH_KEY]["page_cursor"], cursor)

        async def test_s04_two_queries_users_message_binding_restart_and_other_state(self):
            await self.text("See")
            old = self.cards()[0]
            await self.text("Tafel")
            current = self.cards()[-1]
            await self.click(old)
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            self.assertNotIn("quiz_edit_entry", self.data())
            await self.click(current, message_id=99999)
            self.assertNotIn("quiz_edit_entry", self.data())
            self.conv._conversations[(8, 8)] = DB_SEARCH
            # A copied token cannot authorize a different actor, even if its
            # context is deliberately populated with the first user's map.
            self.data(8)[V.SEARCH_KEY] = copy.deepcopy(self.data()[V.SEARCH_KEY])
            await self.click(current, uid=8)
            self.assertNotIn("quiz_edit_entry", self.data(8))
            self.conv._conversations[(UID, UID)] = MENU
            await self.click(current)
            self.assertEqual(self.state(), MENU)
            self.conv._conversations.pop((UID, UID))
            self.data().clear()
            await self.click(current)
            self.assertIsNone(self.state())

        async def test_s04_deletion_and_permission_changes_before_click_and_during_edit(self):
            await self.text("See")
            b, c = self.cards()
            with sqlite3.connect(self.db) as conn:
                conn.execute("DELETE FROM words WHERE id='b'")
            await self.click(b)
            self.assertNotIn("quiz_edit_entry", self.data())
            self.assertTrue(any("b" in message.text and "недоступна" in message.text for message, _ in self.bot.sent))
            await self.click(c)
            await self.text(t("ru", "my_translation"))
            with sqlite3.connect(self.db) as conn:
                conn.execute("UPDATE users SET status='rejected' WHERE user_id=?", (UID,))
            await self.text("must not save")
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            self.assertFalse(storage.get_user_word_media(self.db, UID, "c").get("russian"))
            await self.click(c)
            self.assertNotIn("quiz_edit_entry", self.data())

        async def test_s05_owner_guest_languages_personal_media_and_current_permissions(self):
            for uid in (UID, OWNER):
                for lang, translation in (("ru", "РУССКИЙ"), ("uk", "УКРАЇНСЬКИЙ"), ("en", "ENGLISH")):
                    with sqlite3.connect(self.db) as conn:
                        conn.execute("UPDATE users SET dict_lang=? WHERE user_id=?", (lang, uid))
                    if uid == OWNER:
                        storage.set_setting(self.db, main.OWNER_DICT_LANG_KEY, lang)
                    self.conv._conversations[(uid, uid)] = DB_SEARCH
                    storage.set_user_word_media(self.db, uid, "b", image=f"personal-{uid}")
                    storage.set_user_word_translation(self.db, uid, "b", lang, f"personal-{uid}-{lang}")
                    await self.text("See", uid)
                    card = self.cards()[-2]
                    self.assertIn(f"personal-{uid}", card.text)
                    self.assertIn(translation, card.text)
                    if uid == UID:
                        self.assertIn(f"my_translation: personal-{uid}-{lang}", card.text)
                        for other in {"РУССКИЙ", "УКРАЇНСЬКИЙ", "ENGLISH"} - {translation}:
                            self.assertNotIn(other, card.text)
                    else:
                        self.assertIn("РУССКИЙ", card.text)
                        self.assertIn("УКРАЇНСЬКИЙ", card.text)
                        self.assertIn("ENGLISH", card.text)
                    await self.click(card, uid)
                    markup = self.bot.sent[-1][1]["reply_markup"]
                    labels = [str(x) for row in markup.keyboard for x in row]
                    self.assertEqual(any("delete" in x for x in labels), uid == OWNER)
                    await self.text(t("ru", "back"), uid)

        async def test_s05_search_send_learning_exact_id_replay_return_preserves_study(self):
            data = self.data()
            data.update({"quiz_entry": {"id": "a"}, RETURN_POINT_KEY: {"caller_kind": "learning"},
                         PUBLISHED_VIEW_KEY: {"flow_id": "left-study"},
                         "learning_frame": {"last_answer": {"word_id": "b"}, "current": "a"}})
            before = copy.deepcopy(data)
            await self.text("See")
            await self.click(self.cards()[0])
            update = await self.text(t("ru", "send_learn"), ident=888888)
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            self.assertEqual(data["learning_send_receipt"]["word_id"], "b")
            for key, value in before.items():
                self.assertEqual(data[key], value)
            with sqlite3.connect(self.db) as conn:
                delivered = conn.execute("SELECT COUNT(*) FROM study_deliveries WHERE user_id=? AND transport_update_id=?", (UID, 888888)).fetchone()[0]
                self.assertEqual(delivered, 1)
            # Reopen another ID, then deliver the old transport to the real FSM.
            await self.click(self.cards()[1])
            self.assertEqual(data["quiz_edit_entry"]["id"], "c")
            await self.app.process_update(update)
            self.assertEqual(self.state(), ADD_EDIT)
            self.assertEqual(data["quiz_edit_entry"]["id"], "c")
            # The outer delivery guard rejects the duplicate before clarify;
            # its existing receipt and the newly selected C remain unchanged.
            self.assertEqual(data["learning_send_receipt"]["word_id"], "b")
            from bot.handlers.learning import _send_entry_to_learning
            isolated = SimpleNamespace(user_data={"quiz_edit_entry": {"id": "c"}})
            from bot.learning_requests import EnqueueResult
            self.assertEqual(_send_entry_to_learning(update, isolated, entry_key="quiz_edit_entry"), EnqueueResult.QUEUED)
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT word_id,status FROM learning_requests WHERE user_id=?", (UID,)).fetchall(), [("b", "pending")])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM learning_items WHERE user_id=?", (UID,)).fetchone()[0], 0)
            self.assertTrue(isolated.user_data["learning_send_receipt"]["replayed"])
            self.assertEqual(isolated.user_data["learning_send_receipt"]["word_id"], "b")
            for key, value in before.items():
                self.assertEqual(data[key], value)

        async def test_s05_synonyms_return_keeps_study_and_stale_origin_cannot_capture_add(self):
            from bot.handlers import clarify_fields as C
            from bot.labels import SYNONYMS_TEXT
            self.conv._conversations[(OWNER, OWNER)] = DB_SEARCH
            data = self.data(OWNER)
            study = {PUBLISHED_VIEW_KEY: {"visible": "a", "hidden": "c"},
                     RETURN_POINT_KEY: {"word_id": "a"}, "quiz_entry": {"id": "a"}}
            data.update(copy.deepcopy(study))
            await self.text("See", OWNER)
            b = self.cards()[0]
            await self.click(b, OWNER)
            await self.text(SYNONYMS_TEXT, OWNER)
            await self.text(t("en", "back"), OWNER)
            self.assertEqual(self.state(OWNER), DB_SEARCH)
            for key, value in study.items():
                self.assertEqual(data[key], value)
            # The legacy synonym exit keeps the query snapshot, so the same
            # message can still open its precise ID without repeating the query.
            await self.click(b, OWNER)
            self.assertEqual(data["quiz_edit_entry"]["id"], "b")
            await self.text(SYNONYMS_TEXT, OWNER)
            await self.text(t("en", "back"), OWNER)
            pending = {"type": "phrase", "german": "new pending phrase"}
            data.update(edit_source="add", pending_entry=pending, edit_stage="pick_field")
            self.conv._conversations[(OWNER, OWNER)] = ADD_EDIT
            with patch.object(C, "add_words_enabled", return_value=True):
                await self.text("image", OWNER)
            self.assertEqual(self.state(OWNER), ADD_EDIT)
            self.assertEqual(data["edit_stage"], "enter_value")
            self.assertEqual(data["pending_entry"], pending)
            self.assertNotIn(V.ORIGIN_KEY, data)
            for key, value in study.items():
                self.assertEqual(data[key], value)
            self.conv._conversations[(OWNER, OWNER)] = DB_SEARCH
            await self.text("See", OWNER)
            await self.click(self.cards()[-2], OWNER)
            self.conv._conversations[(OWNER, OWNER)] = DB_SEARCH
            await self.text("Tafel", OWNER)
            self.assertNotIn(V.ORIGIN_KEY, data)
            self.assertNotIn("quiz_edit_entry", data)

        async def test_s05_guest_media_report_and_add_int_stay_on_first_id(self):
            await self.text("See")
            b, c = self.cards()
            await self.click(b)
            await self.text("image")
            await self.text("only-B-image")
            self.assertEqual(storage.get_user_word_media(self.db, UID, "b")["image"], "only-B-image")
            self.assertFalse(storage.get_user_word_media(self.db, UID, "c").get("image"))
            await self.text("add_int")
            from bot.common.lang import _quiz_dir_labels
            await self.text(_quiz_dir_labels(UID)[0])
            await self.text("3")
            from bot.labels import SYN_CONFIRM_TEXT
            await self.text(SYN_CONFIRM_TEXT)
            with sqlite3.connect(self.db) as conn:
                self.assertEqual([row[0] for row in conn.execute("SELECT word_id FROM progress WHERE user_id=?", (UID,))], ["b"])
            await self.text(t("ru", "report_card"))
            await self.text("report for B")
            self.assertEqual(self.state(), DB_SEARCH_VIEW)
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT word_id FROM error_reports WHERE user_id=?", (UID,)).fetchall(), [("b",)])
            await self.click(c)
            self.assertEqual(self.data()["quiz_edit_entry"]["id"], "c")

        async def test_s05_owner_delete_and_role_change_during_field_input(self):
            self.conv._conversations[(OWNER, OWNER)] = DB_SEARCH
            await self.text("See", OWNER)
            b, c = self.cards()
            await self.click(b, OWNER)
            await self.text("de", OWNER)
            self.assertEqual(self.data(OWNER)["edit_stage"], "enter_value")
            with patch.dict(os.environ, {"OWNER_ID": "1001"}):
                await self.text("must-not-write", OWNER)
                self.assertEqual(self.data(OWNER)["edit_stage"], "pick_field")
                with sqlite3.connect(self.db) as conn:
                    self.assertEqual(conn.execute("SELECT german FROM words WHERE id='b'").fetchone()[0], "See")
            await self.text("delete", OWNER)
            await self.text("yes", OWNER)
            self.assertEqual(self.state(OWNER), DB_SEARCH_VIEW)
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT id FROM words WHERE id IN ('b','c')").fetchall(), [("c",)])

        async def test_s06_ack_failure_does_not_prevent_valid_explicit_target(self):
            from unittest.mock import AsyncMock
            await self.text("See")
            with patch.object(FakeBot, "answer_callback_query", AsyncMock(side_effect=TimeoutError("ACK timeout"))):
                with self.assertLogs(V.logger.name, level="WARNING"):
                    await self.click(self.cards()[0])
            self.assertEqual(self.state(), ADD_EDIT)
            self.assertEqual(self.data()["quiz_edit_entry"]["id"], "b")

        async def test_s06_long_line_unicode_markdown_partial_send_retry(self):
            long = ("😀_*`\\Ä" * 1400) + "TAIL"
            with sqlite3.connect(self.db) as conn:
                conn.execute("UPDATE words SET example=? WHERE id='b'", (long,))
            before = self.study_rows()
            object.__setattr__(self.bot, "fail_at", 2)  # header + first confirmed chunk
            with self.assertLogs(V.logger.name, level="WARNING"):
                await self.text("See")
            state = self.data()[V.SEARCH_KEY]
            self.assertEqual(state["page_cursor"], 0)
            self.assertEqual(state["pending"]["word_id"], "b")
            self.assertEqual(state["pending"]["next_chunk"], 1)
            self.assertEqual(set(state["messages"].values()), {"b"})
            confirmed = self.bot.sent[1][0]
            await self.click(self.more())
            self.assertEqual(state["page_cursor"], 2)
            self.assertIsNone(state["pending"])
            self.assertEqual(sum(message.message_id == confirmed.message_id for message, _ in self.bot.sent), 1)
            chunks = [message for message, kw in self.bot.sent if kw.get("entities")]
            for message in chunks:
                self.assertLessEqual(V.utf16_length(message.text), V.MESSAGE_BUDGET)
            bchunks = [message for message in chunks if message.text.startswith("id: b [")]
            reconstructed = "".join(message.text.split("\n", 1)[1] for message in bchunks)
            self.assertIn("TAIL", reconstructed)
            self.assertEqual(len([m for m in bchunks if m.reply_markup]), 1)
            self.assertTrue(bchunks[-1].reply_markup)
            await self.click(bchunks[-1])
            self.assertEqual(self.data()["quiz_edit_entry"]["id"], "b")
            self.assertEqual(self.study_rows(), before)

        async def test_s04_deleted_next_page_id_is_skipped_without_replacement(self):
            for i in range(7):
                self.seed(f"x{i}", "same")
            await self.text("same")
            with sqlite3.connect(self.db) as conn:
                conn.execute("DELETE FROM words WHERE id='x5'")
            self.seed("replacement", "same")
            await self.click(self.more())
            ids = list(self.data()[V.SEARCH_KEY]["messages"].values())
            self.assertNotIn("x5", ids)
            self.assertNotIn("replacement", ids)
            self.assertIn("x6", ids)
            self.assertEqual(len(self.cards()), 6)

        async def test_s03_similar_limit_localized_and_exact_has_no_phrase_extra(self):
            await self.text("Tafel")
            self.assertEqual(len(self.cards()), 1)
            self.assertFalse(any("Phrase matches" in m.text for m, _ in self.bot.sent))
            for i in range(21):
                self.seed(f"s{i}", "substring matches")
            await self.text("substring")
            self.assertTrue(self.data()[V.SEARCH_KEY]["has_more_similar"])
            self.assertTrue(any(t("ru", "search_similar_limit") in m.text for m, _ in self.bot.sent))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AdapterTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


class ActualSearchDispatchTest(unittest.TestCase):
    def test_s03_s06_real_conversation_no_network(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--actual"],
                                capture_output=True, text=True, env=os.environ.copy(), timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stderr)


if __name__ == "__main__":
    if "--actual" in sys.argv:
        actual_suite()
    else:
        unittest.main()
