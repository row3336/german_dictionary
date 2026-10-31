"""P07 — Telegram-only effective image/example fallback."""

import os
import tempfile
import unittest

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
    def test_every_render_path_calls_bot_only_resolver(self):
        root = os.path.join(os.path.dirname(__file__), "..", "bot")
        paths = {
            "full-card": (os.path.join(root, "ui", "formatting.py"), 1),
            "quiz": (os.path.join(root, "handlers", "quiz.py"), 4),
            "learning": (os.path.join(root, "handlers", "learning.py"), 3),
        }
        for name, (path, expected_calls) in paths.items():
            with self.subTest(path=name), open(path, encoding="utf-8") as source:
                text = source.read()
                self.assertEqual(text.count("resolve_bot_word_media("), expected_calls)
                self.assertNotIn("apply_user_word_media(", text)
        with open(os.path.join(root, "handlers", "clarify_fields.py"), encoding="utf-8") as source:
            text = source.read()
            self.assertEqual(text.count("_format_word_card("), 3)
            self.assertIn("_format_word_card(entry, title, user_id=update.effective_user.id)", text)


if __name__ == "__main__":
    unittest.main()
