"""Clarify returns preserve the caller's position and never score or pick words."""

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

from bot.app_config import ADD_EDIT, LEARNING_QUIZ, QUIZ_RU_DE, QUIZ_DE_RU
from bot.common import quiz_resume as R
from bot.handlers import clarify_fields as C, synonyms as S
from bot.i18n import t
from bot.storage import (
    add_word, get_word_by_id, init_db, register_or_update_user, set_user_dict_lang,
    set_user_interface_lang, set_user_word_translation, update_word_fields,
)
from bot.ui.formatting import _format_de_ru_question, _format_word_card

OWNER, GUEST = 999, 101


def update(text="", uid=OWNER):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(message=message, effective_message=message,
                           effective_user=SimpleNamespace(id=uid))


def flat(markup):
    rows = getattr(markup, "keyboard", None) or getattr(markup, "rows", None) or []
    return [getattr(button, "text", button) for row in rows for button in row]


class NavigationTest(unittest.TestCase):
    def setUp(self):
        # Retain synthetic test databases: the workspace does not authorize auto-cleanup.
        self.db = str(Path(os.environ["TMPDIR"]) / f"navigation-{uuid4().hex}.sqlite")
        self.env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": str(OWNER)})
        self.env.start()
        self.addCleanup(self.env.stop)
        init_db(self.db)
        register_or_update_user(self.db, GUEST, "fixture", "Fixture")
        set_user_dict_lang(self.db, GUEST, "uk")
        self.a = add_word(self.db, {"id": "a", "type": "nomen", "article": "das",
            "german": "Haus", "plural": "Häuser", "russian": "дом",
            "ukrainian": "дім", "english": "house", "image": "shared image"})
        self.b = add_word(self.db, {"id": "b", "type": "verb", "german": "gehen",
            "infinitive": "gehen", "russian": "идти", "ukrainian": "йти"})
        self.ctx = SimpleNamespace(user_data={"quiz_entry": self.b,
            "quiz_last_answered_entry": self.a, "quiz_mode": "ru_de",
            "session_mode": "ru_de", "session_seen_ru_de": {"a"},
            "session_correct_ru_de": 1, "session_wrong_ru_de": 0,
            "retry_queue": {"ru_de": ["a"]}, "fsrs_cycle_queue": {"ru_de": [self.b]}})

    def run_async(self, coro):
        return asyncio.run(coro)

    def publish(self, phase="question", kind="written", current="b", visible="a"):
        R.publish_view(self.ctx, caller_kind=kind, mode="ru_de", phase=phase,
                       current_question_id=current, visible_answer_id=visible,
                       flow_id="fixture-flow", view_revision=4)

    def open_card(self, state=QUIZ_RU_DE, uid=OWNER):
        upd = update(uid=uid)
        self.assertEqual(self.run_async(C._start_clarify_fields(upd, self.ctx, state)), ADD_EDIT)
        return upd

    def test_completed_back_does_not_resurrect_stale_question(self):
        self.publish(phase="completed", current=None)
        self.open_card()
        before = copy.deepcopy(self.ctx.user_data)
        upd = update("back")
        with patch("bot.handlers.quiz._send_ru_de_prompt", side_effect=AssertionError("picker")):
            self.assertEqual(self.run_async(C.clarify_fields_input(upd, self.ctx)), QUIZ_RU_DE)
        for key in ("session_seen_ru_de", "session_correct_ru_de", "retry_queue", "fsrs_cycle_queue"):
            self.assertEqual(self.ctx.user_data[key], before[key])
        self.assertNotIn(">>", upd.message.reply_text.call_args_list[-1].args[0])
        self.assertIn(t("en", "clarify_fields"), flat(upd.message.reply_text.call_args.kwargs["reply_markup"]))

    def test_completed_add_int_confirm_and_back_stays_completed(self):
        self.publish(phase="completed", current=None)
        self.open_card()
        self.run_async(C.clarify_fields_input(update("add_int"), self.ctx))
        self.run_async(C.clarify_fields_input(update("3"), self.ctx))
        self.run_async(C.clarify_fields_input(update("Confirm"), self.ctx))
        upd = update("back")
        self.assertEqual(self.run_async(C.clarify_fields_input(upd, self.ctx)), QUIZ_RU_DE)
        self.assertEqual(self.ctx.user_data["session_correct_ru_de"], 1)
        self.assertNotIn(">>", upd.message.reply_text.call_args_list[-1].args[0])

    def test_preview_back_and_edit_keep_next_skip_and_do_not_mark_shown(self):
        self.ctx.user_data.update(quiz_entry=self.a, learning_preview_mode=True)
        for edit in (False, True):
            with self.subTest(edit=edit):
                self.publish(phase="preview", kind="learning", current="a", visible="a")
                self.open_card(LEARNING_QUIZ)
                upd = update("back")
                if edit:
                    self.ctx.user_data.update(edit_stage="enter_value", edit_field="ru")
                    upd = update("новый дом")
                with patch("bot.handlers.learning._send_learning_prompt", side_effect=AssertionError("picker")), \
                     patch("bot.handlers.learning._note_learning_shown", side_effect=AssertionError("shown")):
                    state = self.run_async(C.clarify_fields_input(upd, self.ctx))
                self.assertEqual(state, LEARNING_QUIZ)
                keys = flat(upd.message.reply_text.call_args.kwargs["reply_markup"])
                self.assertIn(t("en", "learning_next"), keys)
                self.assertIn(t("en", "learning_skip"), keys)
                self.assertTrue(self.ctx.user_data["learning_preview_mode"])

    def test_first_hidden_learning_has_no_card_or_picker(self):
        self.ctx.user_data.pop("quiz_last_answered_entry")
        self.ctx.user_data["learning_preview_mode"] = False
        upd = update()
        self.assertEqual(self.run_async(C._start_clarify_fields(upd, self.ctx, LEARNING_QUIZ, True)), LEARNING_QUIZ)
        self.assertEqual(upd.message.reply_text.call_args.args[0], t("en", "clarify_answer_first"))
        self.assertNotIn("quiz_edit_entry", self.ctx.user_data)

    def test_oral_pending_targets_visible_a_instead_of_hidden_b(self):
        self.ctx.user_data.pop("quiz_last_answered_entry")
        self.publish(phase="awaiting_rating", kind="oral", current="b", visible="a")
        self.open_card(51)
        self.assertEqual(self.ctx.user_data["quiz_edit_entry"]["id"], "a")
        self.assertEqual(self.ctx.user_data["quiz_entry"]["id"], "b")

    def test_registered_renderer_receives_original_point_without_queue_snapshot(self):
        self.publish(phase="awaiting_rating", kind="fixture-oral", current="b", visible="a")
        self.open_card(51)
        original = self.ctx.user_data[R.RETURN_POINT_KEY]
        self.ctx.user_data["live_revision"] = 5
        seen = []
        async def renderer(upd, context, point, prefix):
            self.assertIs(point, original)
            self.assertEqual(point.view_revision, 4)
            self.assertEqual(context.user_data["live_revision"], 5)
            seen.append(point.visible_answer_id)
            return 51
        with patch.dict(R._RENDERERS):
            R.register_renderer("fixture-oral", renderer)
            self.assertEqual(self.run_async(C.clarify_fields_input(update("back"), self.ctx)), 51)
        self.assertEqual(seen, ["a"])
        self.assertNotIn(R.RETURN_POINT_KEY, self.ctx.user_data)

    def test_synonyms_returns_learning_preview_and_original_frame(self):
        self.ctx.user_data.update(quiz_entry=self.a, learning_preview_mode=True)
        self.publish(phase="preview", kind="learning", current="a", visible="a")
        self.open_card(LEARNING_QUIZ)
        point = self.ctx.user_data[R.RETURN_POINT_KEY]
        self.run_async(S._open_synonyms_menu(update(), self.ctx, self.a, LEARNING_QUIZ))
        self.assertIs(self.ctx.user_data[R.RETURN_POINT_KEY], point)
        upd = update("back")
        self.assertEqual(self.run_async(S.synonyms_menu(upd, self.ctx)), LEARNING_QUIZ)
        self.assertIn(t("en", "learning_next"), flat(upd.message.reply_text.call_args.kwargs["reply_markup"]))

    def test_deleted_visible_a_keeps_pending_b_without_picker(self):
        self.publish()
        self.open_card()
        self.ctx.user_data["edit_stage"] = "delete_confirm"
        upd = update("yes")
        with patch("bot.handlers.quiz._send_ru_de_prompt", side_effect=AssertionError("picker")):
            self.assertEqual(self.run_async(C.clarify_fields_input(upd, self.ctx)), QUIZ_RU_DE)
        self.assertEqual(self.ctx.user_data["quiz_entry"]["id"], "b")
        self.assertEqual(self.ctx.user_data["retry_queue"]["ru_de"], [])
        self.assertIsNone(get_word_by_id(self.db, "a"))

    def test_delete_current_preview_calls_transition_after_invalidation(self):
        self.publish(phase="preview", kind="fixture-learning", current="a", visible="a")
        self.open_card(LEARNING_QUIZ)
        calls = []
        async def renderer(*args):
            raise AssertionError("must transition after deleting current")
        async def deleted(upd, context, point, wid, prefix):
            self.assertIsNone(point.current_question_id)
            self.assertIsNone(point.visible_answer_id)
            self.assertNotEqual((context.user_data.get("quiz_entry") or {}).get("id"), wid)
            calls.append(wid)
            return LEARNING_QUIZ
        with patch.dict(R._RENDERERS, {"fixture-learning": renderer}), \
             patch.dict(R._DELETE_TRANSITIONS, {"fixture-learning": deleted}):
            self.ctx.user_data["edit_stage"] = "delete_confirm"
            self.run_async(C.clarify_fields_input(update("yes"), self.ctx))
        self.assertEqual(calls, ["a"])

    def test_nested_value_and_conjugation_have_no_stop_and_stale_stop_is_safe(self):
        self.publish()
        self.open_card(uid=GUEST)
        for field in ("image", "example", "my_translation"):
            self.ctx.user_data["edit_stage"] = "pick_field"
            upd = update(t("en", field) if field == "my_translation" else field, GUEST)
            self.run_async(C.clarify_fields_input(upd, self.ctx))
            self.assertNotIn(t("en", "stop"), flat(upd.message.reply_text.call_args.kwargs["reply_markup"]))
        with patch("bot.handlers.add_words._stop_queue", side_effect=AssertionError("stop")):
            self.assertEqual(self.run_async(C.clarify_fields_input(update("stop", GUEST), self.ctx)), ADD_EDIT)
        self.assertIn(R.RETURN_POINT_KEY, self.ctx.user_data)

    def test_guest_card_uses_dictionary_language_for_every_interface(self):
        for interface in ("en", "ru", "uk"):
            set_user_interface_lang(self.db, GUEST, interface)
            for lang, field in (("ru", "russian"), ("uk", "ukrainian"), ("en", "english")):
                with self.subTest(interface=interface, dictionary=lang):
                    set_user_dict_lang(self.db, GUEST, lang)
                    set_user_word_translation(self.db, GUEST, "a", lang, "personal")
                    text = _format_word_card(self.a, user_id=GUEST)
                    self.assertIn(f"{field}: ", text)
                    for other in {"russian", "ukrainian", "english"} - {field}:
                        self.assertNotIn(f"{other}: ", text)
                    self.assertIn("my_translation: personal", text)

    def test_empty_selected_guest_translation_card_and_echo_never_fall_back_to_ru(self):
        update_word_fields(self.db, "a", {"ukrainian": ""})
        self.a = get_word_by_id(self.db, "a")
        text = _format_word_card(self.a, user_id=GUEST)
        self.assertIn("ukrainian: -", text)
        self.assertNotIn("дом", text)
        upd = update(uid=GUEST)
        self.run_async(C._send_effective_value_echo(upd.message, GUEST, self.a, "my_translation"))
        upd.message.reply_text.assert_not_called()

    def test_owner_keeps_all_languages_and_noun_question_keeps_gender_colors(self):
        text = _format_word_card(self.a, user_id=OWNER)
        for field in ("russian", "ukrainian", "english"):
            self.assertIn(f"{field}: ", text)
        for article, marker in (("der", "🔵"), ("die", "🔴"), ("das", "🟢")):
            self.assertIn(f"{marker} {article}", _format_de_ru_question(dict(self.a, article=article)))


if __name__ == "__main__":
    unittest.main()
