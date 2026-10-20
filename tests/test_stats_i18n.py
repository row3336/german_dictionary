"""Statistics screen (_format_progress_text) is localized for ru/uk users.

The screen (shown to everyone via the 'Statistics' button) used to render every
label in English. Labels now go through t(); the FSRS direction tokens follow the
user's dictionary language (Ru>De / Ua>De / En>De), and the today-FSRS counters
read the user's own quiz mode (not a hardcoded ru_de). German word-type names have a separate dictionary view.
"""

import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.handlers.owner_stats import _format_progress_text, _format_word_types_text  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    get_statistics_snapshot,
    init_db,
    log_quiz_event,
    register_or_update_user,
    set_user_dict_lang,
    set_user_interface_lang,
)

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
        for key in ("stat_words_in_dict", "stat_never_touched", "stat_today_heading", "stat_wrong_fsrs"):
            self.assertIn(TEXTS["ru"][key], text)
        # the English originals must be gone
        for raw in ("words in dict", "never touched", "word types:", "wrong fsrs", "5+ correct streak"):
            self.assertNotIn(raw, text)

    def test_directions_follow_dict_language(self):
        # bug (parallel to #24): FSRS direction tokens must reflect the user's
        # dictionary language, not a hardcoded Ru>De.
        set_user_interface_lang(self.db, GUEST, "ru")
        set_user_dict_lang(self.db, GUEST, "uk")
        text = _format_progress_text(GUEST)
        self.assertNotIn("verb = ", text)
        self.assertIn("verb = ", _format_word_types_text("ru"))
        self.assertIn("Ua > De", text)      # direction follows dict language
        self.assertIn("De > Ua", text)
        self.assertNotIn("Ru>De", text)
        self.assertNotIn("Ru > De", text)

    def test_today_fsrs_counts_user_mode_not_hardcoded_ru_de(self):
        # bug: today-FSRS counters were hardcoded to ru_de/de_ru, so a uk/en-dict
        # user always saw 0. They must count the user's own modes (uk_de/de_uk).
        set_user_dict_lang(self.db, GUEST, "uk")
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1','nomen','Haus','дом')")
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w2','nomen','Buch','книга')")
        conn.commit()
        conn.close()
        log_quiz_event(self.db, user_id=GUEST, word_id="w1", mode="uk_de", is_correct=True)
        log_quiz_event(self.db, user_id=GUEST, word_id="w2", mode="de_uk", is_correct=True)
        # a stray ru_de event must NOT leak into this user's uk counters
        snap = get_statistics_snapshot(self.db, user_id=GUEST, mode="uk_de")
        self.assertEqual(snap["today_complete_fsrs_ru_de"], 1)  # uk_de row
        self.assertEqual(snap["today_complete_fsrs_de_ru"], 1)  # de_uk row

    def test_english_user_unchanged(self):
        set_user_interface_lang(self.db, GUEST, "en")
        text = _format_progress_text(GUEST)
        self.assertIn("words in dict = ", text)
        self.assertNotIn("word types:", text)
        self.assertIn("Total = 1\n\nverb = 1", _format_word_types_text("en"))


if __name__ == "__main__":
    unittest.main()
