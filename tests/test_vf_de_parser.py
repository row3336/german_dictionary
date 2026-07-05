"""Tests for the verbformen.de full-page parser (spec #2, Step 1).

Runs against real .de HTML fixtures captured through the proxy
(tests/fixtures/verbformen_de/). Sub-step 1a: word-type + headword detection
and the headword-mismatch guard.
"""

import os
import unittest

from bot.verb_forms_parsing.de_parser import detect_type_and_headword, headword_matches

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "verbformen_de")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class TypeHeadwordTest(unittest.TestCase):
    def test_verb(self):
        r = detect_type_and_headword(load("gehen_summary.html"))
        self.assertEqual(r["type"], "verb")
        self.assertEqual(r["headword"], "gehen")
        self.assertTrue(r["has_entry"])
        self.assertTrue(headword_matches(r["headword"], "gehen"))

    def test_modal_verb(self):
        r = detect_type_and_headword(load("können_summary.html"))
        self.assertEqual(r["type"], "verb")
        self.assertEqual(r["headword"], "können")

    def test_nomen(self):
        r = detect_type_and_headword(load("Haus_summary.html"))
        self.assertEqual(r["type"], "nomen")
        self.assertEqual(r["headword"], "Haus")

    def test_adjektiv(self):
        r = detect_type_and_headword(load("schnell_summary.html"))
        self.assertEqual(r["type"], "adjektiv")
        self.assertEqual(r["headword"], "schnell")

    def test_headword_mismatch_flags(self):
        # ?w=weil resolves to the verb 'weilen' -> type is read, but the guard
        # must reject the headword so the caller flags the row for manual review.
        r = detect_type_and_headword(load("weil_summary.html"))
        self.assertEqual(r["type"], "verb")
        self.assertEqual(r["headword"], "weilen")
        self.assertFalse(headword_matches(r["headword"], "weil"))

    def test_search_page_has_no_entry(self):
        # adverbs / function words land on a "Deutsche Wörter mit …" search page.
        for word in ("mit", "gern"):
            r = detect_type_and_headword(load(f"{word}_summary.html"))
            self.assertFalse(r["has_entry"], word)
            self.assertEqual(r["type"], "", word)
            self.assertEqual(r["headword"], word)

    def test_headword_matches_is_case_insensitive(self):
        self.assertTrue(headword_matches("Haus", "haus"))
        self.assertFalse(headword_matches("", "haus"))


if __name__ == "__main__":
    unittest.main()
