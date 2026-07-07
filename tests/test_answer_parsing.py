"""Answer parsing must tolerate stray punctuation.

Prod bug (2026-07-07): a user answered a nomen "Die bankverbindung,
bankverbindungen" (space between article+noun, comma before the plural) and it
was marked wrong. `_split_answer_parts` used to split on comma XOR whitespace,
so a comma present anywhere made it split ONLY on the comma — "die bankverbindung"
landed in the article field. Now commas, periods and whitespace are all
interchangeable separators, so however the user punctuates, the fields line up.
"""

import unittest

from _telegram_stub import install

install()

from bot.handlers.quiz import _check_ru_de_strict, _split_answer_parts  # noqa: E402

NOMEN = {"type": "nomen", "article": "die", "german": "Bankverbindung", "plural": "Bankverbindungen"}


class SplitAnswerPartsTest(unittest.TestCase):
    def test_all_separators_yield_same_fields(self):
        expected = ["die", "Bankverbindung", "Bankverbindungen"]
        for raw in (
            "die Bankverbindung Bankverbindungen",       # spaces
            "die, Bankverbindung, Bankverbindungen",     # commas
            "die. Bankverbindung. Bankverbindungen",     # periods
            "die Bankverbindung, Bankverbindungen",      # the reported mixed case
            "die Bankverbindung ,Bankverbindungen",      # comma glued to the plural
            "die Bankverbindung.Bankverbindungen",       # period, no space
            "  die ,, Bankverbindung .. Bankverbindungen ",  # messy noise
        ):
            self.assertEqual(_split_answer_parts(raw), expected, raw)

    def test_empty(self):
        self.assertEqual(_split_answer_parts(""), [])
        self.assertEqual(_split_answer_parts("   "), [])


class NomenValidationTest(unittest.TestCase):
    def _ok(self, answer):
        ok, _ = _check_ru_de_strict(NOMEN, answer)
        return ok

    def test_reported_case_now_valid(self):
        # the exact string from the prod report (any casing)
        self.assertTrue(self._ok("Die bankverbindung, bankverbindungen"))

    def test_every_punctuation_style_valid(self):
        for raw in (
            "die Bankverbindung Bankverbindungen",
            "die, Bankverbindung, Bankverbindungen",
            "die Bankverbindung, Bankverbindungen",
            "die Bankverbindung. Bankverbindungen",
            "die Bankverbindung ,Bankverbindungen",
        ):
            self.assertTrue(self._ok(raw), raw)

    def test_still_rejects_wrong_article_or_noun(self):
        self.assertFalse(self._ok("der Bankverbindung, Bankverbindungen"))  # wrong article
        self.assertFalse(self._ok("die Kontoverbindung, Kontoverbindungen"))  # wrong noun


class GeneralAnswerPunctuationTest(unittest.TestCase):
    def test_trailing_punctuation_tolerated(self):
        entry = {"type": "adjektiv", "german": "schnell"}
        for raw in ("schnell", "schnell.", "schnell,", " schnell . "):
            ok, _ = _check_ru_de_strict(entry, raw)
            self.assertTrue(ok, raw)


if __name__ == "__main__":
    unittest.main()
