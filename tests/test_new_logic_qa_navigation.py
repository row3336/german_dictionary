"""Independent acceptance of the frozen navigation and disabled Add package."""

import asyncio
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from _telegram_stub import install
install()

from telegram.error import BadRequest, TimedOut
from bot.app_config import ADD_EDIT, LEARNING_QUIZ, MENU, QUIZ_RU_DE
from bot.common import add_words_gate as gate, quiz_resume as resume
from bot.handlers import clarify_fields as clarify, learning
from bot.i18n import t
from bot.storage import (
    add_word, init_db, register_or_update_user, set_user_dict_lang,
    set_user_interface_lang, set_user_status, set_user_word_translation,
)
from bot.ui.formatting import _format_word_card
from bot.verb_forms_parsing import add_flow


def update(user_id=999, text=""):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(message=message, effective_message=message,
                           effective_user=SimpleNamespace(id=user_id))


class IndependentNavigationQA(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(os.environ["TMPDIR"]) / f"independent-nav-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": "999"})
        env.start()
        self.addCleanup(env.stop)
        init_db(self.db)
        register_or_update_user(self.db, 101, "fixture", "Fixture")
        set_user_status(self.db, 101, "approved")
        self.a = add_word(self.db, {"id": "qa-a", "type": "nomen", "article": "das",
            "german": "Haus", "russian": "shared-ru", "ukrainian": "shared-uk",
            "english": "shared-en"})
        self.b = add_word(self.db, {"id": "qa-b", "type": "verb", "german": "gehen",
            "infinitive": "gehen", "russian": "идти", "ukrainian": "йти"})

    def test_expired_old_vf_callback_still_returns_working_menu(self):
        upd = update()
        upd.callback_query = SimpleNamespace(message=upd.message,
            answer=AsyncMock(side_effect=BadRequest("Query is too old and response timeout expired")))
        ctx = SimpleNamespace(user_data={"add_queue": ["Haus"], "quiz_entry": self.b})
        with patch.object(gate, "ADD_WORDS_ENABLED", False), \
             patch.object(add_flow, "run_job", side_effect=AssertionError("launch")):
            result = asyncio.run(add_flow.vf_add_callback(upd, ctx))
        self.assertEqual(result, MENU)
        self.assertEqual(upd.message.reply_text.call_args.args[0], t("en", "add_words_disabled"))
        self.assertEqual(ctx.user_data["quiz_entry"], self.b)

    def test_disabled_old_callback_uses_its_message_without_effective_message(self):
        upd = update(101)
        message = upd.message
        upd.message = upd.effective_message = None
        upd.callback_query = SimpleNamespace(message=message, answer=AsyncMock())
        ctx = SimpleNamespace(user_data={"vf_apply_entries": [self.a]})
        with patch.object(gate, "ADD_WORDS_ENABLED", False):
            self.assertEqual(asyncio.run(add_flow.vf_add_callback(upd, ctx)), MENU)
        self.assertNotIn("vf_apply_entries", ctx.user_data)
        message.reply_text.assert_awaited_once()

    def test_disabled_reply_failure_still_returns_menu_without_add_effects(self):
        upd = update(101)
        upd.message.reply_text.side_effect = TimedOut("disabled-menu send failed")
        ctx = SimpleNamespace(user_data={"vf_apply_entries": [self.a], "quiz_entry": self.b})
        with patch.object(gate, "ADD_WORDS_ENABLED", False), \
             patch.object(add_flow, "apply_batch", side_effect=AssertionError("apply")), \
             patch.object(add_flow, "run_job", side_effect=AssertionError("launch")):
            self.assertEqual(asyncio.run(add_flow.vf_apply_confirm(upd, ctx)), MENU)
        self.assertNotIn("vf_apply_entries", ctx.user_data)
        self.assertEqual(ctx.user_data["quiz_entry"], self.b)

    def test_personal_translation_and_echo_are_scoped_to_selected_language(self):
        for lang in ("ru", "uk", "en"):
            set_user_word_translation(self.db, 101, self.a["id"], lang, f"personal-{lang}")
        for interface in ("ru", "uk", "en"):
            set_user_interface_lang(self.db, 101, interface)
            for dictionary, column in (("ru", "russian"), ("uk", "ukrainian"), ("en", "english")):
                with self.subTest(interface=interface, dictionary=dictionary):
                    set_user_dict_lang(self.db, 101, dictionary)
                    card = _format_word_card(self.a, user_id=101)
                    self.assertIn(f"{column}: shared-{dictionary}", card)
                    self.assertIn(f"my_translation: personal-{dictionary}", card)
                    for other in {"ru", "uk", "en"} - {dictionary}:
                        self.assertNotIn(f"personal-{other}", card)
                        self.assertNotIn(f"shared-{other}", card)
                    upd = update(101)
                    asyncio.run(clarify._send_effective_value_echo(upd.message, 101, self.a, "my_translation"))
                    self.assertIn(f"personal-{dictionary}", upd.message.reply_text.call_args.args[0])

    def test_learning_normal_phase_opens_previous_answer_for_both_roles(self):
        for user_id in (999, 101):
            with self.subTest(user_id=user_id):
                ctx = SimpleNamespace(user_data={"quiz_entry": self.b,
                    "quiz_last_answered_entry": self.a, "learning_preview_mode": False})
                upd = update(user_id, "clarify_fields")
                self.assertEqual(asyncio.run(learning.learning_quiz(upd, ctx)), ADD_EDIT)
                self.assertEqual(ctx.user_data["quiz_edit_entry"]["id"], self.a["id"])
                self.assertEqual(ctx.user_data["quiz_entry"]["id"], self.b["id"])

    def test_renderer_failure_retains_return_point_and_live_state(self):
        ctx = SimpleNamespace(user_data={"quiz_entry": self.b, "counter": 3})
        resume.publish_view(ctx, caller_kind="qa-renderer", mode="ru_de", phase="question",
            current_question_id=self.b["id"], visible_answer_id=self.a["id"], flow_id="qa-flow")
        point = resume.capture_return_point(ctx, QUIZ_RU_DE)
        before = copy.deepcopy(ctx.user_data)
        renderer = AsyncMock(side_effect=RuntimeError("render failed"))
        with patch.dict(resume._RENDERERS, {"qa-renderer": renderer}):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                asyncio.run(resume.render_existing_frame(update(), ctx))
        self.assertIs(ctx.user_data[resume.RETURN_POINT_KEY], point)
        self.assertEqual(ctx.user_data, before)

    def test_nested_stop_in_any_ui_language_does_not_exit_quiz_clarify(self):
        for language in ("ru", "uk", "en"):
            with self.subTest(language=language):
                set_user_interface_lang(self.db, 999, language)
                ctx = SimpleNamespace(user_data={"quiz_entry": self.b,
                    "quiz_last_answered_entry": self.a, "learning_preview_mode": False})
                asyncio.run(clarify._start_clarify_fields(update(), ctx, LEARNING_QUIZ))
                point = ctx.user_data[resume.RETURN_POINT_KEY]
                ctx.user_data.update(edit_stage="enter_value", edit_field="example")
                with patch("bot.handlers.add_words._stop_queue", side_effect=AssertionError("exit")):
                    self.assertEqual(asyncio.run(clarify.clarify_fields_input(
                        update(text=t(language, "stop")), ctx)), ADD_EDIT)
                self.assertIs(ctx.user_data[resume.RETURN_POINT_KEY], point)
                self.assertEqual(ctx.user_data["quiz_entry"]["id"], self.b["id"])


if __name__ == "__main__":
    unittest.main()
