"""spec #2 step 3b — verbformen.de fetch primitives (URL building, conj-link
extraction, and the summary+conjugation orchestration with fetch_url mocked)."""

import os
import unittest

from bot.verb_forms_parsing import de_fetch as F

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "verbformen_de")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class UrlTest(unittest.TestCase):
    def test_summary_url_plain(self):
        self.assertEqual(F.summary_url("gehen"), "https://www.verbformen.de/?w=gehen")

    def test_summary_url_umlaut_encoded(self):
        self.assertIn("%C3%B6", F.summary_url("können"))  # ö -> %C3%B6

    def test_summary_url_card_type_hint_uses_verbformen(self):
        # nomen/verb/adjektiv keep the verbformen ?w= summary (richer extractors)
        self.assertEqual(F.summary_url("Haus", "nomen"), "https://www.verbformen.de/?w=Haus")
        self.assertEqual(F.summary_url("gehen", "verb"), "https://www.verbformen.de/?w=gehen")

    def test_summary_url_function_word_hint_uses_verben_type_page(self):
        # prepositions/adverbs/conjunctions route to the verben.de type page so a
        # noun homonym (das Aus) does not hijack the ?w= search
        self.assertEqual(F.summary_url("aus", "praeposition"), "https://www.verben.de/praepositionen/aus.htm")
        self.assertEqual(F.summary_url("wieder", "adverb"), "https://www.verben.de/adverbien/wieder.htm")
        self.assertEqual(F.summary_url("und", "konjunktion"), "https://www.verben.de/konjunktionen/und.htm")
        self.assertEqual(F.summary_url("ja", "partikel"), "https://www.verben.de/partikeln/ja.htm")
        # case-insensitive hint
        self.assertEqual(F.summary_url("aus", "Praeposition"), "https://www.verben.de/praepositionen/aus.htm")

    def test_summary_url_pronomen_uses_verben_query(self):
        # pronouns have no /segment/ page; a ?w=&id=pronomen: query selects the entry
        self.assertEqual(F.summary_url("wer", "pronomen"), "https://www.verben.de/?w=wer&id=pronomen:wer")

    def test_summary_url_unmapped_hint_falls_back(self):
        # a hint without a type page (or none) falls back to the ?w= search
        self.assertEqual(F.summary_url("x", "phrase"), "https://www.verbformen.de/?w=x")
        self.assertEqual(F.summary_url("aus", ""), "https://www.verbformen.de/?w=aus")

    def test_conjugation_url_from_verb_summary(self):
        url = F.conjugation_url(load("gehen_summary.html"))
        self.assertIn("/konjugation/", url)
        self.assertTrue(url.startswith("https://www.verbformen.de"))
        self.assertTrue(url.endswith(".htm"))

    def test_conjugation_url_none_for_noun(self):
        self.assertEqual(F.conjugation_url(load("Haus_summary.html")), "")


class FetchWordPagesTest(unittest.TestCase):
    def setUp(self):
        self._real = F.fetch_url
        self.addCleanup(lambda: setattr(F, "fetch_url", self._real))

    def test_fetches_summary_and_conjugation_with_pause(self):
        calls, paused = [], []

        def fake(url, proxy=None, timeout_sec=40):
            calls.append(url)
            if "konjugation" in url:
                return True, 200, "<conj-html>"
            return True, 200, load("gehen_summary.html")

        F.fetch_url = fake
        summary, conj = F.fetch_word_pages("gehen", proxy="", pause=lambda: paused.append(1))
        self.assertIn("gehen", summary)
        self.assertEqual(conj, "<conj-html>")
        self.assertEqual(len(calls), 2)  # summary + conjugation
        self.assertEqual(paused, [1])  # paused once, between the two requests

    def test_no_conjugation_link_single_fetch(self):
        calls = []

        def fake(url, proxy=None, timeout_sec=40):
            calls.append(url)
            return True, 200, load("Haus_summary.html")  # noun: no konjugation link

        F.fetch_url = fake
        summary, conj = F.fetch_word_pages("Haus", proxy="")
        self.assertEqual(conj, "")
        self.assertEqual(len(calls), 1)

    def test_summary_failure_raises(self):
        F.fetch_url = lambda url, proxy=None, timeout_sec=40: (False, 429, "")
        with self.assertRaises(RuntimeError):
            F.fetch_word_pages("gehen", proxy="")


if __name__ == "__main__":
    unittest.main()
