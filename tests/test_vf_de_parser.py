"""Tests for the verbformen.de full-page parser (spec #2, Step 1).

Runs against real .de HTML fixtures captured through the proxy
(tests/fixtures/verbformen_de/). Sub-step 1a: word-type + headword detection
and the headword-mismatch guard.
"""

import os
import unittest

from bot.verb_forms_parsing.de_parser import (
    detect_type_and_headword,
    headword_matches,
    parse_noun_article_plural,
    parse_praesens_conjugation,
    parse_translations,
    parse_verb_forms,
)

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


class TranslationsTest(unittest.TestCase):
    def test_verb_ru_uk_en_ordered_lowercased(self):
        t = parse_translations(load("gehen_summary.html"))
        self.assertEqual(t["ru"][0], "идти")
        self.assertIn("уходить", t["ru"])
        self.assertEqual(t["uk"][0], "ходити")
        self.assertEqual(t["en"][0], "go")
        # no flag/&nbsp; leaked, everything lowercased
        for lang in ("ru", "uk", "en"):
            self.assertTrue(t[lang])
            self.assertTrue(all(v == v.lower() and "\xa0" not in v and v for v in t[lang]))

    def test_nomen_translations(self):
        t = parse_translations(load("Haus_summary.html"))
        self.assertEqual(t["ru"][0], "дом")
        self.assertEqual(t["en"][0], "house")
        self.assertEqual(t["uk"][0], "будинок")

    def test_adjektiv_translations(self):
        t = parse_translations(load("schnell_summary.html"))
        self.assertEqual(t["ru"][0], "быстрый")
        self.assertEqual(t["en"][0], "fast")

    def test_missing_language_is_empty_list(self):
        t = parse_translations("<html><h1>Konjugation des Verbs x</h1></html>")
        self.assertEqual(t, {"ru": [], "uk": [], "en": []})


class VerbFormsTest(unittest.TestCase):
    def test_strong_verb(self):
        f = parse_verb_forms(load("gehen_summary.html"))
        self.assertEqual(f["prateritum"], "ging")
        self.assertEqual(f["partizip2"], "gegangen")  # auxiliary 'ist' dropped

    def test_modal_verb(self):
        f = parse_verb_forms(load("können_summary.html"))
        self.assertEqual(f["prateritum"], "konnte")
        self.assertEqual(f["partizip2"], "gekonnt")

    def test_weak_verb(self):
        f = parse_verb_forms(load("weil_summary.html"))  # ?w=weil -> weilen
        self.assertEqual(f["prateritum"], "weilte")
        self.assertEqual(f["partizip2"], "geweilt")

    def test_non_verb_page_is_empty(self):
        f = parse_verb_forms(load("Haus_summary.html"))
        self.assertEqual(f, {"prateritum": "", "partizip2": ""})


class PraesensConjugationTest(unittest.TestCase):
    def test_regular_verb_canonical_block(self):
        block = parse_praesens_conjugation(load("gehen_conj.html"))
        self.assertEqual(
            block,
            "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen",
        )

    def test_modal_verb_canonical_block(self):
        block = parse_praesens_conjugation(load("können_conj.html"))
        self.assertEqual(
            block,
            "ich kann\ndu kannst\ner/sie/es kann\nwir können\nihr könnt\nsie/Sie können",
        )

    def test_no_conjugation_table_returns_empty(self):
        self.assertEqual(parse_praesens_conjugation(load("Haus_summary.html")), "")
        self.assertEqual(parse_praesens_conjugation(""), "")


class NounArticlePluralTest(unittest.TestCase):
    def test_haus(self):
        r = parse_noun_article_plural(load("Haus_summary.html"))
        self.assertEqual(r["article"], "das")
        self.assertEqual(r["plural"], "Häuser")

    def test_non_noun_page_is_empty(self):
        r = parse_noun_article_plural(load("gehen_summary.html"))
        self.assertEqual(r, {"article": "", "plural": ""})


if __name__ == "__main__":
    unittest.main()
