"""Tests for the verbformen.de full-page parser (spec #2, Step 1).

Runs against real .de HTML fixtures captured through the proxy
(tests/fixtures/verbformen_de/). Sub-step 1a: word-type + headword detection
and the headword-mismatch guard.
"""

import os
import unittest
from unittest.mock import patch

from bot.verb_forms_parsing.de_parser import (
    detect_type_and_headword,
    headword_matches,
    parse_adjektiv_degrees,
    parse_de,
    parse_examples,
    parse_noun_article_plural,
    parse_praesens_conjugation,
    parse_translations,
    parse_verb_forms,
    parse_word_level,
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

    def test_stress_accents_stripped_and_deduped(self):
        # dict translations mark stress with a combining acute (U+0301); stored
        # words must be plain text, and stripping must not create visible dupes
        t = parse_translations(load("Haus_summary.html"))
        self.assertTrue(all("́" not in v and "̀" not in v for v in t["ru"]))
        self.assertIn("здание", t["ru"])          # зда́ние -> здание
        self.assertEqual(t["ru"].count("здание"), 1)  # collapsed with the plain spelling

    def test_stress_strip_leaves_umlauts(self):
        from bot.verb_forms_parsing.de_parser import _strip_stress
        self.assertEqual(_strip_stress("Häuser grün größe"), "Häuser grün größe")
        self.assertEqual(_strip_stress("мгнове́ние"), "мгновение")


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

    def test_multi_gender_article(self):
        # Joghurt: primary "der Joghurt" + JSON-LD nav "das/die Joghurt" -> all
        # articles, primary first, comma-joined (canonical multi-value separator).
        r = parse_noun_article_plural(load("Joghurt_summary.html"))
        self.assertEqual(r["article"], "der, das, die")
        self.assertEqual(r["plural"], "Joghurts")

    def test_multi_plural_forms(self):
        # Pizza: "Nominativ Plural „Pizzas/Pizzen“" -> both forms, slash normalised
        # to the canonical comma separator (split_plural_forms already reads both).
        r = parse_noun_article_plural(load("Pizza_summary.html"))
        self.assertEqual(r["article"], "die")
        self.assertEqual(r["plural"], "Pizzas, Pizzen")

    def test_non_noun_page_is_empty(self):
        r = parse_noun_article_plural(load("gehen_summary.html"))
        self.assertEqual(r, {"article": "", "plural": ""})


class WordLevelTest(unittest.TestCase):
    def test_badge_on_every_card_type(self):
        # The "Wortschatz Zertifikat" badge is present on nouns, verbs AND
        # adjectives/adverbs (unlike the noun/verb-only prose), so it is the source.
        self.assertEqual(parse_word_level(load("Haus_summary.html")), "A1")
        self.assertEqual(parse_word_level(load("gehen_summary.html")), "A1")
        self.assertEqual(parse_word_level(load("schnell_summary.html")), "A1")
        self.assertEqual(parse_word_level(load("Joghurt_summary.html")), "C1")

    def test_first_badge_is_the_headword(self):
        # weil -> weilen (archaic) is C2; the mismatch page still yields its level.
        self.assertEqual(parse_word_level(load("weil_summary.html")), "C2")

    def test_prose_fallback_when_no_badge(self):
        # A page that dropped the badge still yields a level via the "auf X-Niveau"
        # prose (nouns/verbs carry it).
        html = 'Das Wort „Test“ gehört zum Wortschatz auf B2-Niveau.'
        self.assertEqual(parse_word_level(html), "B2")

    def test_missing_level_is_empty(self):
        self.assertEqual(parse_word_level("<html><body>nothing</body></html>"), "")
        self.assertEqual(parse_word_level(""), "")


class AdjektivDegreesTest(unittest.TestCase):
    def test_schnell(self):
        r = parse_adjektiv_degrees(load("schnell_summary.html"))
        self.assertEqual(r["komparativ"], "schneller")
        self.assertEqual(r["superlativ"], "am schnellsten")

    def test_laut(self):
        r = parse_adjektiv_degrees(load("laut_summary.html"))
        self.assertEqual(r["komparativ"], "lauter")
        self.assertEqual(r["superlativ"], "am lautesten")

    def test_non_adjektiv_page_is_empty(self):
        r = parse_adjektiv_degrees(load("gehen_summary.html"))
        self.assertEqual(r, {"komparativ": "", "superlativ": ""})


class ExamplesTest(unittest.TestCase):
    def test_verb_first_three(self):
        ex = parse_examples(load("gehen_summary.html"))
        lines = ex.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Ich gehe jetzt.")
        # German only — no English translation leaked in
        self.assertNotIn("taking off", ex)

    def test_noun_first_example(self):
        ex = parse_examples(load("Haus_summary.html"))
        self.assertEqual(ex.split("\n")[0], "Ich geh nach Hause.")

    def test_limit_is_respected(self):
        ex = parse_examples(load("gehen_summary.html"), limit=1)
        self.assertEqual(ex, "Ich gehe jetzt.")

    def test_no_examples_section(self):
        self.assertEqual(parse_examples("<html><body>no examples</body></html>"), "")


class ParseDeAssemblyTest(unittest.TestCase):
    def test_valid_separable_verb_marker_maps_to_one(self):
        html = load("gehen_summary.html").replace(
            '<h1 class="rClear">Konjugation des Verbs gehen</h1>',
            '<h1 class="rClear">Konjugation des Verbs gehen</h1>'
            '<span title="trennbares Verb">trennbar</span>',
        )
        e = parse_de(html, load("gehen_conj.html"), query="gehen")
        self.assertEqual(e["is_separable"], 1)

    def test_valid_non_separable_verb_without_marker_maps_to_zero(self):
        e = parse_de(load("gehen_summary.html"), load("gehen_conj.html"), query="gehen")
        self.assertEqual(e["is_separable"], 0)

    def test_visible_marker_words_without_structural_attribute_do_not_count(self):
        html = load("gehen_summary.html").replace(
            '<h1 class="rClear">Konjugation des Verbs gehen</h1>',
            '<h1 class="rClear">Konjugation des Verbs gehen</h1>'
            '<p>trennbares Verb</p>',
        )
        e = parse_de(html, load("gehen_conj.html"), query="gehen")
        self.assertEqual(e["is_separable"], 0)

    def test_untrusted_or_non_verb_card_maps_to_unknown(self):
        cases = (
            parse_de(load("weil_summary.html"), load("gehen_conj.html"), query="weil"),
            parse_de(load("mit_summary.html"), query="mit"),
            parse_de(load("Haus_summary.html"), query="Haus"),
        )
        for entry in cases:
            self.assertIsNone(entry["is_separable"])

    def test_conjugation_parse_error_maps_to_unknown(self):
        html = load("gehen_summary.html").replace(
            '<h1 class="rClear">Konjugation des Verbs gehen</h1>',
            '<h1 class="rClear">Konjugation des Verbs gehen</h1>'
            '<span title="trennbares Verb">trennbar</span>',
        )
        with patch(
            "bot.verb_forms_parsing.de_parser.parse_praesens_conjugation",
            side_effect=ValueError("broken table"),
        ):
            e = parse_de(html, load("gehen_conj.html"), query="gehen")
        self.assertIsNone(e["is_separable"])

    def test_verb_full_card(self):
        e = parse_de(load("gehen_summary.html"), load("gehen_conj.html"), query="gehen")
        self.assertEqual(e["type"], "verb")
        self.assertEqual(e["infinitive"], "gehen")
        self.assertEqual(e["prateritum"], "ging")
        self.assertEqual(e["partizip2"], "gegangen")
        self.assertEqual(
            e["conjugation_praesens"],
            "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen",
        )
        self.assertTrue(e["russian"].startswith("идти, "))  # all meanings kept
        self.assertEqual(len(e["example"].split("\n")), 3)
        self.assertEqual(e["_flags"], [])

    def test_verb_without_conjugation_page_is_flagged(self):
        # bug #29 (live run, fliegen): a failed/missing conjugation page used to
        # produce a silently empty conjugation_praesens — must be flagged so the
        # owner sees it in the review file.
        e = parse_de(load("gehen_summary.html"), conj_html=None, query="gehen")
        self.assertEqual(e["conjugation_praesens"], "")
        self.assertIn("conjugation_missing", e["_flags"])

    def test_nomen_full_card(self):
        e = parse_de(load("Haus_summary.html"), query="Haus")
        self.assertEqual(e["type"], "nomen")
        self.assertEqual(e["article"], "das")
        self.assertEqual(e["german"], "Haus")
        self.assertEqual(e["plural"], "Häuser")
        self.assertEqual(e["cefr_level"], "A1")
        self.assertIn("дом", e["russian"])
        self.assertEqual(e["_flags"], [])

    def test_multi_gender_noun_card(self):
        e = parse_de(load("Joghurt_summary.html"), query="Joghurt")
        self.assertEqual(e["type"], "nomen")
        self.assertEqual(e["article"], "der, das, die")
        self.assertEqual(e["cefr_level"], "C1")
        self.assertEqual(e["_flags"], [])

    def test_level_present_on_every_type(self):
        for name, q in (("gehen", "gehen"), ("schnell", "schnell")):
            e = parse_de(load(f"{name}_summary.html"), query=q)
            self.assertEqual(e["cefr_level"], "A1", name)

    def test_adjektiv_full_card(self):
        e = parse_de(load("schnell_summary.html"), query="schnell")
        self.assertEqual(e["type"], "adjektiv")
        self.assertEqual(e["german"], "schnell")
        self.assertEqual(e["komparativ"], "schneller")
        self.assertEqual(e["superlativ"], "am schnellsten")
        self.assertEqual(e["_flags"], [])

    def test_headword_mismatch_flagged(self):
        e = parse_de(load("weil_summary.html"), query="weil")
        self.assertEqual(e["_headword"], "weilen")
        self.assertIn("headword_mismatch", e["_flags"])

    def test_search_page_flagged_no_card(self):
        e = parse_de(load("mit_summary.html"), query="mit")
        self.assertIn("no_card", e["_flags"])
        self.assertEqual(e["type"], "")
        self.assertEqual(e["german"], "mit")


class TypeHintTest(unittest.TestCase):
    """[type] hint routing/gating (spec #2): function words parse from their
    verben.de type page; a homonym card is rejected by the hint."""

    def test_detect_recognises_function_word_pages(self):
        # verben.de type pages use "Definition <gen. type> <word>" h1s
        self.assertEqual(
            detect_type_and_headword(load("aus_praeposition_summary.html")),
            {"type": "praeposition", "headword": "aus", "has_entry": True},
        )

    def test_detect_recognises_generic_definition_page(self):
        # pronoun pages use a generic "Definition von <word>" h1 (no type word) —
        # still a real entry, type left to the hint
        self.assertEqual(
            detect_type_and_headword(load("wer_pronomen_summary.html")),
            {"type": "", "headword": "wer", "has_entry": True},
        )

    def test_pronomen_page_full_card(self):
        e = parse_de(load("wer_pronomen_summary.html"), query="wer", type_hint="pronomen")
        self.assertEqual(e["type"], "pronomen")
        self.assertEqual(e["german"], "wer")
        self.assertEqual(e["cefr_level"], "A1")
        self.assertIn("кто", e["russian"])
        self.assertEqual(e["_flags"], [])  # generic page is a real entry -> no no_card

    def test_detect_recognises_partikel_page(self):
        self.assertEqual(
            detect_type_and_headword(load("ja_partikel_summary.html")),
            {"type": "partikel", "headword": "ja", "has_entry": True},
        )

    def test_partikel_page_full_card(self):
        e = parse_de(load("ja_partikel_summary.html"), query="ja", type_hint="partikel")
        self.assertEqual(e["type"], "partikel")
        self.assertEqual(e["german"], "ja")
        self.assertEqual(e["cefr_level"], "A1")
        self.assertIn("да", e["russian"])
        self.assertEqual(e["_flags"], [])

    def test_preposition_page_full_card(self):
        e = parse_de(load("aus_praeposition_summary.html"), query="aus", type_hint="praeposition")
        self.assertEqual(e["type"], "praeposition")
        self.assertEqual(e["german"], "aus")
        self.assertEqual(e["cefr_level"], "A1")
        self.assertIn("из", e["russian"])
        self.assertIn("out of", e["english"])
        self.assertEqual(e["_flags"], [])  # no mismatch: page matches the hint

    def test_hint_matching_detected_type_no_flag(self):
        # a nomen hint on a real noun page agrees -> parses normally, no flag
        e = parse_de(load("Haus_summary.html"), query="Haus", type_hint="nomen")
        self.assertEqual(e["type"], "nomen")
        self.assertEqual(e["article"], "das")
        self.assertEqual(e["_flags"], [])

    def test_homonym_card_rejected_by_hint(self):
        # ?w=aus would land on the noun "das Aus"; the praeposition hint must not
        # adopt that wrong-sense card — flag it and emit nothing to overwrite with
        noun = '<html><h1>Definition des Substantivs Aus</h1><dd lang="en">end</dd></html>'
        e = parse_de(noun, query="aus", type_hint="praeposition")
        self.assertIn("type_mismatch", e["_flags"])
        self.assertEqual(e["type"], "praeposition")   # hint wins over detected nomen
        self.assertEqual(e["german"], "aus")          # query, not the homonym "Aus"
        self.assertEqual(e["russian"], "")            # wrong-sense content dropped
        self.assertEqual(e["english"], "")
        self.assertEqual(e["cefr_level"], "")


if __name__ == "__main__":
    unittest.main()
