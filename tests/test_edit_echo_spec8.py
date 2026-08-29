"""Spec #8 — effective value echo before clarify field input."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import ADD_EDIT  # noqa: E402
from bot.handlers import clarify_fields as C  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    register_or_update_user,
    set_user_dict_lang,
    set_user_interface_lang,
    set_user_word_media,
    set_user_word_translation,
)


OWNER, GUEST = 999, 111


class _Message:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))


class _Update:
    def __init__(self, user_id, text):
        self.message = _Message(text)
        self.effective_user = type("User", (), {"id": user_id})()


class _Context:
    def __init__(self, entry):
        self.user_data = {
            "edit_source": "quiz",
            "quiz_edit_entry": entry,
            "edit_stage": "pick_field",
        }


def _utf16_len(value):
    return len(value.encode("utf-16-le")) // 2


class EditEchoSpec8Test(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        self.db = handle.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.previous = {name: os.environ.get(name) for name in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore_env)
        register_or_update_user(self.db, GUEST, "guest", "Guest")
        set_user_dict_lang(self.db, GUEST, "uk")
        self.word_id = add_word(self.db, {
            "type": "nomen", "german": "Haus", "russian": "дом",
            "ukrainian": "дім", "english": "house",
            "image": "shared image", "example": "shared example",
        })["id"]

    def _restore_env(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _entry(self):
        return {
            "id": self.word_id, "type": "nomen", "german": "Haus",
            "russian": "дом", "ukrainian": "дім", "english": "house",
            "image": "shared image", "example": "shared example",
        }

    def _select(self, user_id, lang, field):
        set_user_interface_lang(self.db, user_id, lang)
        text = t(lang, "my_translation") if field == "my_translation" else field
        update = _Update(user_id, text)
        context = _Context(self._entry())
        self.assertEqual(asyncio.run(C.clarify_fields_input(update, context)), ADD_EDIT)
        return update.message.calls, context

    def _assert_echo(self, calls, lang, value, prompt):
        self.assertEqual(len(calls), 2)
        echo, echo_kwargs = calls[0]
        heading = t(lang, "clarify_current_value")
        hint = t(lang, "clarify_tap_copy")
        self.assertEqual(echo, f"{heading}\n{value}\n{hint}")
        entity, = echo_kwargs["entities"]
        self.assertEqual(entity.type, "code")
        self.assertEqual(entity.offset, _utf16_len(f"{heading}\n"))
        self.assertEqual(entity.length, _utf16_len(value))
        self.assertNotIn("parse_mode", echo_kwargs)
        self.assertEqual(calls[1][0], prompt)

    def test_guest_personal_image_echoes_before_ru_prompt_without_markdown_changes(self):
        raw = "`/a_b*\n🙂 line"
        set_user_word_media(self.db, GUEST, self.word_id, image=raw)
        calls, context = self._select(GUEST, "ru", "image")
        self._assert_echo(calls, "ru", raw, t("ru", "clarify_enter_value").format(field="image"))
        self.assertEqual(context.user_data["edit_stage"], "enter_value")

    def test_guest_shared_example_fallback_echoes_before_uk_prompt(self):
        calls, _ = self._select(GUEST, "uk", "example")
        self._assert_echo(calls, "uk", "shared example", t("uk", "clarify_enter_value").format(field="example"))

    def test_guest_personal_example_wins_over_shared_value(self):
        set_user_word_media(self.db, GUEST, self.word_id, example="personal example")
        calls, _ = self._select(GUEST, "en", "example")
        self._assert_echo(calls, "en", "personal example", t("en", "clarify_enter_value").format(field="example"))

    def test_guest_translation_uses_personal_then_shared_dict_language(self):
        set_user_word_translation(self.db, GUEST, self.word_id, "uk", "мій\nпереклад")
        calls, _ = self._select(GUEST, "en", "my_translation")
        self._assert_echo(calls, "en", "мій\nпереклад", t("en", "my_translation_prompt"))

        set_user_word_translation(self.db, GUEST, self.word_id, "uk", "  ")
        calls, _ = self._select(GUEST, "en", "my_translation")
        self._assert_echo(calls, "en", "дім", t("en", "my_translation_prompt"))

    def test_empty_effective_values_send_only_their_existing_prompts(self):
        for field, prompt in (
            ("image", t("en", "clarify_enter_value").format(field="image")),
            ("example", t("en", "clarify_enter_value").format(field="example")),
            ("my_translation", t("en", "my_translation_prompt")),
        ):
            with self.subTest(field=field):
                entry = self._entry()
                entry.update({"image": "", "example": "", "russian": "", "ukrainian": "", "english": ""})
                text = t("en", "my_translation") if field == "my_translation" else field
                update = _Update(GUEST, text)
                context = _Context(entry)
                self.assertEqual(asyncio.run(C.clarify_fields_input(update, context)), ADD_EDIT)
                self.assertEqual([text for text, _ in update.message.calls], [prompt])

    def test_owner_uses_shared_value_and_preset_field_has_no_echo(self):
        calls, _ = self._select(OWNER, "en", "example")
        self._assert_echo(calls, "en", "shared example", t("en", "clarify_enter_value").format(field="example"))

        update = _Update(OWNER, "type")
        context = _Context(self._entry())
        asyncio.run(C.clarify_fields_input(update, context))
        self.assertEqual(len(update.message.calls), 1)
        self.assertEqual(update.message.calls[0][0], "Choose new type:")


if __name__ == "__main__":
    unittest.main()
