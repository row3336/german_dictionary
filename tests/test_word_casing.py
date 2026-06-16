import unittest
import warnings

from bot.storage import _normalize_word_entry_for_storage

warnings.filterwarnings("ignore", category=ResourceWarning)


class WordCasingTests(unittest.TestCase):
    """Casing rules: Nomen capitalized; other forms and translations lowercased; phrase untouched."""

    def test_nomen_german_capitalized_article_and_translation_lowercased(self):
        r = _normalize_word_entry_for_storage(
            {"type": "nomen", "article": "DER", "german": "tISCH", "russian": "СТОЛ", "english": "Table"}
        )
        self.assertEqual(r["german"], "Tisch")
        self.assertEqual(r["article"], "der")
        self.assertEqual(r["russian"], "стол")
        self.assertEqual(r["english"], "table")

    def test_type_is_lowercased(self):
        r = _normalize_word_entry_for_storage(
            {"type": "NOMEN", "article": "die", "german": "katze", "russian": "кошка"}
        )
        self.assertEqual(r["type"], "nomen")
        self.assertEqual(r["german"], "Katze")

    def test_verb_forms_lowercased(self):
        r = _normalize_word_entry_for_storage(
            {"type": "verb", "german": "GEHEN", "infinitive": "GEHEN", "prateritum": "Ging", "russian": "Идти"}
        )
        self.assertEqual(r["german"], "gehen")
        self.assertEqual(r["infinitive"], "gehen")
        self.assertEqual(r["prateritum"], "ging")
        self.assertEqual(r["russian"], "идти")

    def test_adjektiv_forms_lowercased(self):
        r = _normalize_word_entry_for_storage(
            {"type": "adjektiv", "german": "SCHÖN", "komparativ": "Schöner", "superlativ": "SCHÖNSTE", "russian": "Красивый"}
        )
        self.assertEqual(r["german"], "schön")
        self.assertEqual(r["komparativ"], "schöner")
        self.assertEqual(r["superlativ"], "schönste")
        self.assertEqual(r["russian"], "красивый")

    def test_phrase_returns_unchanged_casing(self):
        r = _normalize_word_entry_for_storage(
            {"type": "phrase", "german": "Guten TAG", "russian": "Добрый день"}
        )
        # phrase exits early: german/translations keep their casing
        self.assertEqual(r["german"], "Guten TAG")
        self.assertEqual(r["russian"], "Добрый день")
        self.assertEqual(r["type"], "phrase")


if __name__ == "__main__":
    unittest.main()
