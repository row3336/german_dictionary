"""P07 — Telegram-only effective image/example fallback."""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import tempfile
import unittest

from _telegram_stub import install
install()

from bot.common.bot_media import resolve_bot_word_media
from bot.storage import (
    add_word,
    apply_user_word_media,
    init_db,
    set_user_dict_lang,
    set_user_word_media,
    set_user_word_translation,
)


GUEST, OWNER = 101, 5000


def _db_path():
    handle = tempfile.NamedTemporaryFile(delete=False)
    handle.close()
    return handle.name


class BotMediaFallbackTest(unittest.TestCase):
    def setUp(self):
        self.db = _db_path()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.word_id = add_word(self.db, {
            "type": "nomen", "german": "Haus", "russian": "дом",
            "image": "shared image", "example": "shared example",
        })["id"]
        self.entry = {"id": self.word_id, "image": "shared image", "example": "shared example"}

    def test_fallback_is_per_field_and_keeps_shared_input(self):
        set_user_word_media(self.db, GUEST, self.word_id, image=" personal image ", example="  ")
        effective = resolve_bot_word_media(self.db, GUEST, self.entry)
        self.assertEqual(effective["image"], " personal image ")
        self.assertEqual(effective["example"], "shared example")
        self.assertEqual(self.entry, {"id": self.word_id, "image": "shared image", "example": "shared example"})

    def test_absent_and_whitespace_personal_fall_back_but_storage_overlay_does_not(self):
        self.assertEqual(resolve_bot_word_media(self.db, GUEST, self.entry)["image"], "shared image")
        set_user_word_media(self.db, GUEST, self.word_id, image="\t", example="\n")
        effective = resolve_bot_word_media(self.db, GUEST, self.entry)
        self.assertEqual((effective["image"], effective["example"]), ("shared image", "shared example"))
        overlay = apply_user_word_media(self.db, GUEST, self.entry)
        self.assertEqual((overlay["image"], overlay["example"]), ("\t", "\n"))

    def test_both_nonblank_personal_values_win(self):
        set_user_word_media(
            self.db,
            GUEST,
            self.word_id,
            image="personal image",
            example="personal example",
        )
        effective = resolve_bot_word_media(self.db, GUEST, self.entry)
        self.assertEqual(effective["image"], "personal image")
        self.assertEqual(effective["example"], "personal example")

    def test_personal_translation_contract_is_preserved(self):
        set_user_dict_lang(self.db, GUEST, "uk")
        set_user_word_translation(self.db, GUEST, self.word_id, "uk", "дім")
        effective = resolve_bot_word_media(self.db, GUEST, self.entry)
        self.assertEqual(effective["my_translation"], "дім")

    def test_owner_uses_personal_media_fallback_but_shared_translation(self):
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(lambda: os.environ.pop("OWNER_ID", None))
        set_user_word_media(self.db, OWNER, self.word_id, image="owner image", example="  ")
        set_user_word_translation(self.db, OWNER, self.word_id, "ru", "личный перевод")
        effective = resolve_bot_word_media(self.db, OWNER, self.entry)
        self.assertEqual(effective["image"], "owner image")
        self.assertEqual(effective["example"], "shared example")
        self.assertNotIn("my_translation", effective)
        self.assertIsNot(effective, self.entry)


class TelegramCallerCoverageTest(unittest.TestCase):
    """Exercise rendered Telegram values, independently of helper call counts."""

    def setUp(self):
        from bot.storage import register_or_update_user, set_user_status
        self.db = str(Path(os.environ["TMPDIR"]) / f"telegram-media-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": str(OWNER)})
        env.start()
        self.addCleanup(env.stop)
        init_db(self.db)
        for uid in (GUEST, OWNER):
            register_or_update_user(self.db, uid, "synthetic", "Synthetic")
            set_user_status(self.db, uid, "approved")
            set_user_dict_lang(self.db, uid, "uk")
        self.word_id = add_word(self.db, {"type": "praeposition", "german": "mit",
            "russian": "ru sentinel", "ukrainian": "uk shared", "english": "en sentinel",
            "preposition_type": "modal", "case_type": "dativ", "topic": "1"})["id"]
        set_user_word_translation(self.db, GUEST, self.word_id, "uk", "uk private")
        set_user_word_translation(self.db, OWNER, self.word_id, "uk", "owner private")
        self.sequence = 6000

    def update(self, uid):
        self.sequence += 1
        message = SimpleNamespace(message_id=self.sequence, text="",
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=self.sequence)))
        return SimpleNamespace(update_id=self.sequence, message=message, effective_message=message,
            effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=uid))

    def context(self):
        return SimpleNamespace(user_data={"quiz_mode": "uk_de"})

    def cases(self):
        from bot.storage import get_word_by_id, update_word_fields
        variants = (
            ("personal", "shared image", "shared example", "private image", "private example", "private image", "private example"),
            ("whitespace", "shared image", "shared example", "\t", "\n", "shared image", "shared example"),
            ("empty shared", "", "", "private image", "private example", "private image", "private example"),
            ("per field", "shared image", "shared example", "private image", "  ", "private image", "shared example"),
        )
        for label, image, example, personal_image, personal_example, expected_image, expected_example in variants:
            update_word_fields(self.db, self.word_id, {"image": image, "example": example})
            for uid in (GUEST, OWNER):
                set_user_word_media(self.db, uid, self.word_id, image=personal_image, example=personal_example)
                yield label, uid, get_word_by_id(self.db, self.word_id), expected_image, expected_example

    def assert_translation(self, text, uid, card=False):
        if card:
            self.assertIn("ukrainian: uk shared", text)
            if uid == OWNER:
                self.assertIn("russian: ru sentinel", text)
                self.assertIn("english: en sentinel", text)
                self.assertNotIn("owner private", text)
                self.assertNotIn("my_translation:", text)
            else:
                self.assertNotIn("russian:", text)
                self.assertNotIn("english:", text)
                self.assertIn("my_translation: uk private", text)
        else:
            self.assertIn("uk shared" if uid == OWNER else "uk private", text)
            self.assertNotIn("ru sentinel", text)
            self.assertNotIn("en sentinel", text)
            self.assertNotIn("owner private", text)

    def test_full_card_shows_effective_media_without_changing_role_translation_rules(self):
        from bot.ui.formatting import _format_word_card
        for label, uid, shared, image, example in self.cases():
            with self.subTest(case=label, user=uid):
                before = deepcopy(shared)
                text = _format_word_card(shared, user_id=uid)
                self.assertIn("image: " + image, text)
                self.assertIn("example: " + example, text)
                self.assert_translation(text, uid, card=True)
                self.assertEqual(shared, before)

    def test_written_entry_and_return_deliver_media_in_wrong_answer_feedback(self):
        from bot.app_config import QUIZ_RU_DE
        from bot.handlers import quiz as quiz
        for label, uid, shared, image, example in self.cases():
            with self.subTest(case=label, user=uid):
                context, update = self.context(), self.update(uid)
                # Queue selection is a separate contract; return a real shared row
                # here so this test observes only its Telegram presentation.
                with patch.object(quiz, "_pick_due_entry", return_value=shared):
                    asyncio.run(quiz._send_ru_de_prompt(update, context, None))
                self.assert_translation(update.message.reply_text.await_args.args[0], uid)
                feedback = quiz._format_feedback_ru_de(context.user_data["quiz_entry"], False)
                answer = self.update(uid)
                asyncio.run(quiz._repeat_current_ru_de(answer, context, feedback))
                delivered = answer.message.reply_text.await_args.args[0]
                self.assertIn(image, delivered)
                self.assertIn(example, delivered)
                # A render-only return must refresh newly edited personal media.
                set_user_word_media(self.db, uid, self.word_id, image="returned image", example="returned example")
                frame = context.user_data["quiz_written_frame"]
                point = SimpleNamespace(flow_id=frame["flow_id"], mode=frame["mode"], view_revision=frame["revision"])
                asyncio.run(quiz._render_existing_written(self.update(uid), context, point, None))
                refreshed = quiz._format_feedback_ru_de(context.user_data["quiz_entry"], False)
                returned = self.update(uid)
                asyncio.run(quiz._repeat_current_ru_de(returned, context, refreshed))
                delivered = returned.message.reply_text.await_args.args[0]
                self.assertIn("returned image", delivered)
                self.assertIn("returned example", delivered)
                self.assertEqual(frame["return_state"], QUIZ_RU_DE)

    def test_learning_preview_delivers_effective_example_and_selected_translation(self):
        from bot.handlers import learning
        for label, uid, shared, image, example in self.cases():
            with self.subTest(case=label, user=uid):
                context, update = self.context(), self.update(uid)
                frame = learning._new_frame()
                frame.update(phase="preview", revision=1,
                    current={"entry": shared, "action_key": "synthetic-preview"})
                context.user_data[learning.FRAME] = frame
                asyncio.run(learning._render_learning_frame(update, context))
                delivered = update.message.reply_text.await_args.args[0]
                self.assertIn(example, delivered)
                self.assert_translation(delivered, uid)
                self.assertEqual(context.user_data["quiz_entry"]["image"], image)
                self.assertEqual(frame["current"]["entry"]["image"], shared["image"])

    def test_oral_refresh_delivers_effective_media_and_preserves_translation_ownership(self):
        from bot.oral import domain, handlers as oral
        for label, uid, shared, image, example in self.cases():
            with self.subTest(case=label, user=uid):
                context, update = self.context(), self.update(uid)
                frame = domain.new_frame("de_uk")
                frame["hidden"] = domain.slot(shared)
                context.user_data[oral.FRAME] = frame
                asyncio.run(oral._render(update, context, refresh=True))
                delivered = update.message.reply_text.await_args.args[0]
                self.assertIn(image, delivered)
                self.assertIn(example, delivered)
                frame.update(phase="grading", pending=frame["hidden"], hidden=None, view=None)
                answer = self.update(uid)
                asyncio.run(oral._render(answer, context, refresh=True))
                self.assert_translation(answer.message.reply_text.await_args.args[0], uid)

    def test_clarify_card_and_copyable_echo_use_effective_media_for_both_roles(self):
        from bot.app_config import QUIZ_RU_DE
        from bot.handlers import clarify_fields as clarify
        for label, uid, shared, image, example in self.cases():
            with self.subTest(case=label, user=uid):
                context, update = self.context(), self.update(uid)
                context.user_data["quiz_last_answered_entry"] = shared
                asyncio.run(clarify._start_clarify_fields(update, context, QUIZ_RU_DE))
                card = update.message.reply_text.await_args_list[0].args[0]
                self.assertIn("image: " + image, card)
                self.assertIn("example: " + example, card)
                self.assert_translation(card, uid, card=True)
                for field, value in (("image", image), ("example", example),
                                     ("my_translation", "uk shared" if uid == OWNER else "uk private")):
                    echo = self.update(uid)
                    asyncio.run(clarify._send_effective_value_echo(echo.message, uid, shared, field))
                    text = echo.message.reply_text.await_args.args[0]
                    self.assertIn("\n" + value + "\n", text)
                    self.assertEqual(echo.message.reply_text.await_args.kwargs["entities"][0].type, "code")


if __name__ == "__main__":
    unittest.main()
