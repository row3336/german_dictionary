"""Spec #12 — owner service broadcast: AI-translate + preview/confirm + send-all.

Covers: bot.ai.translate_broadcast (JSON parsing/validation), the owner
conversation flow (prompt -> AI preview -> yes/no), the actual send loop
(per-user language, Forbidden tolerance, report), and the inline "menu" button
callback that resends a user's main menu.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from telegram.error import Forbidden  # noqa: E402

import bot.ai as ai  # noqa: E402
import bot.handlers.broadcast as B  # noqa: E402
from bot.app_config import BROADCAST_CONFIRM, BROADCAST_INPUT, OWNER_REVIEW_MENU  # noqa: E402
from bot.handlers.review import _owner_review_keys, owner_review_menu_choice  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    register_or_update_user,
    set_user_interface_lang,
    set_user_status,
)

OWNER, RU_GUEST, UK_GUEST = 999, 111, 222


def _run(coro):
    return asyncio.run(coro)


def _flat(rows):
    return [c for row in rows for c in row]


# --- bot.ai.translate_broadcast -------------------------------------------------

class TranslateBroadcastTest(unittest.TestCase):
    def setUp(self):
        self._old_gen = ai.generate_fields
        self.addCleanup(lambda: setattr(ai, "generate_fields", self._old_gen))

    def test_parses_clean_json(self):
        ai.generate_fields = lambda prompt: '{"ukrainian":"Привіт","english":"Hello"}'
        result = ai.translate_broadcast("Привет")
        self.assertEqual(result, {"ru": "Привет", "uk": "Привіт", "en": "Hello"})

    def test_extracts_json_wrapped_in_prose(self):
        ai.generate_fields = lambda prompt: 'Sure, here you go:\n{"ukrainian":"Привіт","english":"Hello"}\nDone.'
        result = ai.translate_broadcast("Привет")
        self.assertEqual(result["uk"], "Привіт")
        self.assertEqual(result["en"], "Hello")

    def test_empty_field_raises(self):
        ai.generate_fields = lambda prompt: '{"ukrainian":"","english":"Hello"}'
        with self.assertRaises(ValueError):
            ai.translate_broadcast("Привет")

    def test_garbage_response_raises(self):
        ai.generate_fields = lambda prompt: "not json at all"
        with self.assertRaises(Exception):
            ai.translate_broadcast("Привет")


# --- Conversation flow -----------------------------------------------------------

class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))


class _User:
    def __init__(self, uid):
        self.id = uid


class _Update:
    def __init__(self, text, uid):
        self.message = _Msg(text)
        self.effective_user = _User(uid)


class _Bot:
    def __init__(self, raise_for=None):
        self.sent = []  # (chat_id, text, reply_markup)
        self.raise_for = raise_for or {}

    async def send_message(self, chat_id, text, **kw):
        exc = self.raise_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text, kw.get("reply_markup")))


class _Ctx:
    def __init__(self, bot=None):
        self.user_data = {}
        self.bot = bot or _Bot()


class BroadcastFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        register_or_update_user(self.db, RU_GUEST, "ru_guest", "Ru")
        set_user_status(self.db, RU_GUEST, "approved")
        set_user_interface_lang(self.db, RU_GUEST, "ru")
        register_or_update_user(self.db, UK_GUEST, "uk_guest", "Uk")
        set_user_status(self.db, UK_GUEST, "approved")
        set_user_interface_lang(self.db, UK_GUEST, "uk")
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self._old_translate = B.translate_broadcast
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        B.translate_broadcast = self._old_translate

    def test_review_menu_has_broadcast_button(self):
        self.assertIn(t("en", "review_broadcast"), _flat(_owner_review_keys(OWNER)))

    def test_review_menu_routes_to_broadcast_input(self):
        update = _Update(t("en", "review_broadcast"), OWNER)
        state = _run(owner_review_menu_choice(update, _Ctx()))
        self.assertEqual(state, BROADCAST_INPUT)
        self.assertEqual(update.message.replies[-1], t("en", "broadcast_prompt"))

    def test_back_cancels_to_review_menu(self):
        update = _Update("back", OWNER)
        state = _run(B.broadcast_input(update, _Ctx()))
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertEqual(update.message.replies[-1], t("en", "review_menu"))

    def test_ai_success_shows_preview_and_stores_texts(self):
        B.translate_broadcast = lambda text: {"ru": text, "uk": "укр текст", "en": "en text"}
        update = _Update("Всем привет!", OWNER)
        ctx = _Ctx()
        state = _run(B.broadcast_input(update, ctx))
        self.assertEqual(state, BROADCAST_CONFIRM)
        self.assertEqual(
            ctx.user_data["broadcast_texts"],
            {"ru": "Всем привет!", "uk": "укр текст", "en": "en text"},
        )
        preview = update.message.replies[-1]
        self.assertIn("Всем привет!", preview)
        self.assertIn("укр текст", preview)
        self.assertIn("en text", preview)

    def test_ai_failure_returns_to_review_menu_without_storing(self):
        def _boom(text):
            raise RuntimeError("openai down")

        B.translate_broadcast = _boom
        update = _Update("Всем привет!", OWNER)
        ctx = _Ctx()
        state = _run(B.broadcast_input(update, ctx))
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertEqual(update.message.replies[-1], t("en", "broadcast_ai_error"))
        self.assertNotIn("broadcast_texts", ctx.user_data)

    def test_non_owner_guarded_out_of_input(self):
        update = _Update("hello", RU_GUEST)
        state = _run(B.broadcast_input(update, _Ctx()))
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertEqual(update.message.replies, [])  # no prompt leaked to a non-owner

    def test_non_owner_guarded_out_of_confirm(self):
        update = _Update(t("en", "yes_btn"), RU_GUEST)
        ctx = _Ctx()
        ctx.user_data["broadcast_texts"] = {"ru": "x", "uk": "y", "en": "z"}
        state = _run(B.broadcast_confirm(update, ctx))
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertEqual(ctx.bot.sent, [])

    def test_confirm_not_yes_cancels_without_sending(self):
        update = _Update(t("en", "no_btn"), OWNER)
        ctx = _Ctx()
        ctx.user_data["broadcast_texts"] = {"ru": "рус", "uk": "укр", "en": "eng"}
        state = _run(B.broadcast_confirm(update, ctx))
        self.assertEqual(state, OWNER_REVIEW_MENU)
        self.assertEqual(update.message.replies[-1], t("en", "broadcast_canceled"))
        self.assertEqual(ctx.bot.sent, [])

    def test_confirm_yes_sends_each_user_their_own_language(self):
        bot = _Bot()
        ctx = _Ctx(bot)
        ctx.user_data["broadcast_texts"] = {"ru": "рус текст", "uk": "укр текст", "en": "eng text"}
        update = _Update(t("en", "yes_btn"), OWNER)
        state = _run(B.broadcast_confirm(update, ctx))
        self.assertEqual(state, OWNER_REVIEW_MENU)
        sent_by_id = {chat_id: text for chat_id, text, _ in bot.sent}
        # OWNER is included in list_active_users and defaults to interface_lang "en".
        self.assertEqual(sent_by_id[RU_GUEST], "рус текст")
        self.assertEqual(sent_by_id[UK_GUEST], "укр текст")
        self.assertEqual(sent_by_id[OWNER], "eng text")
        # Every send carries the inline "menu" button.
        for _, _, markup in bot.sent:
            self.assertIsNotNone(markup)
        report = update.message.replies[-1]
        self.assertEqual(report, t("en", "broadcast_report").format(delivered=3, failed=0))
        self.assertNotIn("broadcast_texts", ctx.user_data)

    def test_confirm_yes_forbidden_user_counts_as_undelivered_and_does_not_stop_loop(self):
        bot = _Bot(raise_for={RU_GUEST: Forbidden("blocked")})
        ctx = _Ctx(bot)
        ctx.user_data["broadcast_texts"] = {"ru": "рус текст", "uk": "укр текст", "en": "eng text"}
        update = _Update(t("en", "yes_btn"), OWNER)
        _run(B.broadcast_confirm(update, ctx))
        sent_ids = {chat_id for chat_id, _, _ in bot.sent}
        self.assertNotIn(RU_GUEST, sent_ids)  # blocked -> not delivered
        self.assertIn(UK_GUEST, sent_ids)     # loop continued past the blocked user
        self.assertIn(OWNER, sent_ids)
        report = update.message.replies[-1]
        self.assertEqual(report, t("en", "broadcast_report").format(delivered=2, failed=1))


# --- Inline "menu" button callback ------------------------------------------------

class _FakeQuery:
    def __init__(self, uid):
        self.from_user = _User(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeCbUpdate:
    def __init__(self, query):
        self.callback_query = query


class BroadcastMenuCallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        register_or_update_user(self.db, UK_GUEST, "uk_guest", "Uk")
        set_user_status(self.db, UK_GUEST, "approved")
        set_user_interface_lang(self.db, UK_GUEST, "uk")
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_resends_main_menu_in_users_language(self):
        bot = _Bot()
        ctx = _Ctx(bot)
        query = _FakeQuery(UK_GUEST)
        _run(B.broadcast_menu_callback(_FakeCbUpdate(query), ctx))
        self.assertEqual(len(bot.sent), 1)
        chat_id, text, markup = bot.sent[0]
        self.assertEqual(chat_id, UK_GUEST)
        self.assertEqual(text, t("uk", "main_menu"))
        self.assertIsNotNone(markup)
        self.assertEqual(query.answers, [(None, False)])

    def test_send_failure_is_swallowed(self):
        bot = _Bot(raise_for={UK_GUEST: RuntimeError("network down")})
        ctx = _Ctx(bot)
        query = _FakeQuery(UK_GUEST)
        # Must not raise out of the callback.
        _run(B.broadcast_menu_callback(_FakeCbUpdate(query), ctx))
        self.assertEqual(query.answers, [(None, False)])


if __name__ == "__main__":
    unittest.main()
