"""Statistics screen (_format_progress_text) is localized for ru/uk users.

The screen (shown to everyone via the 'Statistics' button) used to render every
label in English. Labels now go through t(); the German word-type names at the
end and the direction tokens (Ru>De / De>Ru / pl>sg) stay as-is (owner decision
2026-07-07).
"""

import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.handlers.owner_stats import _format_progress_text  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import add_word, init_db, register_or_update_user, set_user_interface_lang  # noqa: E402

GUEST = 111

_STAT_KEYS = [k for k in TEXTS["en"] if k.startswith("stat_")]


class StatsI18nTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = "999"
        self.addCleanup(self._restore)
        register_or_update_user(self.db, GUEST, "g", "G")
        # one verb so 'word types:' has a German type line
        add_word(self.db, {"type": "verb", "german": "gehen", "infinitive": "gehen", "russian": "идти", "topic": "1"})

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_new_keys_present_and_localized(self):
        for key in _STAT_KEYS:
            for lang in ("en", "ru", "uk"):
                self.assertIn(key, TEXTS[lang], f"{key} missing in {lang}")
                self.assertTrue(TEXTS[lang][key].strip(), f"{key} empty in {lang}")
            self.assertNotEqual(TEXTS["ru"][key], TEXTS["en"][key], key)
            self.assertNotEqual(TEXTS["uk"][key], TEXTS["en"][key], key)

    def test_ru_labels_localized_no_raw_english(self):
        set_user_interface_lang(self.db, GUEST, "ru")
        text = _format_progress_text(GUEST)
        # sample of localized labels present
        for key in ("stat_words_in_dict", "stat_never_touched", "stat_word_types", "stat_wrong_fsrs"):
            self.assertIn(TEXTS["ru"][key], text)
        # the English originals must be gone
        for raw in ("words in dict", "never touched", "word types:", "wrong fsrs", "5+ correct streak"):
            self.assertNotIn(raw, text)

    def test_german_types_and_directions_preserved(self):
        set_user_interface_lang(self.db, GUEST, "ru")
        text = _format_progress_text(GUEST)
        self.assertIn("verb = ", text)   # German type name kept as-is
        self.assertIn("Ru>De", text)     # direction token kept as-is

    def test_english_user_unchanged(self):
        set_user_interface_lang(self.db, GUEST, "en")
        text = _format_progress_text(GUEST)
        self.assertIn("words in dict = ", text)
        self.assertIn("word types:", text)


if __name__ == "__main__":
    unittest.main()
