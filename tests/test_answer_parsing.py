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


VERB = {"type": "verb", "infinitive": "kreuzen", "prateritum": "kreuzte", "partizip2": "gekreuzt"}


class VerbValidationTest(unittest.TestCase):
    """Verb answers must tolerate comma/period-without-space just like nomen
    (checked live on the alpha bot). Short answers contain infinitive+Partizip II;
    full answers contain infinitive+Präteritum+Partizip II. Forms may themselves
    contain spaces, so validation compares complete normalized candidates."""

    def _ok(self, answer):
        ok, _ = _check_ru_de_strict(VERB, answer)
        return ok

    def test_no_space_punctuation_valid(self):
        for raw in (
            "kreuzen kreuzte gekreuzt",     # spaces
            "kreuzen,kreuzte,gekreuzt",     # commas, no space
            "kreuzen.kreuzte.gekreuzt",     # periods, no space
            "kreuzen,gekreuzt",             # short (inf + partizip2), no space
            "kreuzen ,, kreuzte .. gekreuzt",  # messy noise
        ):
            self.assertTrue(self._ok(raw), raw)

    def test_missing_infinitive_rejected(self):
        # "kreuzte,gekreuzt" (the two past forms, no infinitive) is read as
        # [infinitive=kreuzte, partizip2=gekreuzt] -> wrong. NOT a no-space issue:
        # the infinitive is simply absent.
        self.assertFalse(self._ok("kreuzte,gekreuzt"))

    def test_multiword_prateritum_with_any_separator(self):
        entry = {
            "type": "verb",
            "infinitive": "anfangen",
            "prateritum": "fing an",
            "partizip2": "angefangen",
        }
        for raw in (
            "anfangen fing an angefangen",
            "Anfangen, fing an, angefangen",
            "anfangen,fing an,angefangen",
            "anfangen. fing an. angefangen",
            "anfangen angefangen",
        ):
            ok, _ = _check_ru_de_strict(entry, raw)
            self.assertTrue(ok, raw)

    def test_incomplete_multiword_prateritum_is_rejected(self):
        entry = {
            "type": "verb",
            "infinitive": "anfangen",
            "prateritum": "fing an",
            "partizip2": "angefangen",
        }
        ok, checks = _check_ru_de_strict(entry, "anfangen fing angefangen")
        self.assertFalse(ok)
        self.assertEqual(checks, {"partizip2": True, "prateritum": False})

    def test_stored_partizip2_alternatives_are_individually_accepted(self):
        entry = {
            "type": "verb",
            "infinitive": "backen",
            "prateritum": "backte",
            "partizip2": "gebacken, gebackt",
        }
        for raw in ("backen gebacken", "backen,backte,gebackt"):
            ok, _ = _check_ru_de_strict(entry, raw)
            self.assertTrue(ok, raw)


class GeneralAnswerPunctuationTest(unittest.TestCase):
    def test_trailing_punctuation_tolerated(self):
        entry = {"type": "adjektiv", "german": "schnell"}
        for raw in ("schnell", "schnell.", "schnell,", " schnell . "):
            ok, _ = _check_ru_de_strict(entry, raw)
            self.assertTrue(ok, raw)


# bug #26: an adjective may be answered with 1, 2 or 3 forms (base / +komparativ /
# +superlativ), in that order, with any separator style; typing extra degrees must
# not flip a right answer to wrong.
ADJ = {"type": "adjektiv", "german": "leise", "komparativ": "leiser", "superlativ": "am leisesten"}


class AdjektivDegreeValidationTest(unittest.TestCase):
    def _ok(self, answer, entry=ADJ):
        ok, _ = _check_ru_de_strict(entry, answer)
        return ok

    def test_base_only_valid(self):
        self.assertTrue(self._ok("leise"))

    def test_base_plus_komparativ_any_separator(self):
        for raw in ("leise, leiser", "leise leiser", "leise. leiser", "leise,leiser", "leise ,, leiser"):
            self.assertTrue(self._ok(raw), raw)

    def test_all_three_forms_any_separator(self):
        # superlativ itself carries a space ("am leisesten")
        for raw in (
            "leise, leiser, am leisesten",
            "leise leiser am leisesten",
            "leise. leiser. am leisesten",
            "leise,leiser,am leisesten",
            "leise, leiser, am leisesten,",   # trailing comma
            "  leise ,, leiser .. am leisesten ",
        ):
            self.assertTrue(self._ok(raw), raw)

    def test_komparativ_alone_rejected(self):
        # the base form is still required; a lone higher degree is not a pass
        self.assertFalse(self._ok("leiser"))
        self.assertFalse(self._ok("am leisesten"))

    def test_superlativ_without_komparativ_rejected(self):
        # forms must be supplied in order (base, komparativ, superlativ)
        self.assertFalse(self._ok("leise, am leisesten"))

    def test_wrong_extra_form_rejected(self):
        self.assertFalse(self._ok("leise, falsch"))
        self.assertFalse(self._ok("leise, leiser, am lautesten"))

    def test_wrong_base_rejected(self):
        self.assertFalse(self._ok("laut"))

    def test_adjektiv_without_degrees_base_only(self):
        entry = {"type": "adjektiv", "german": "toll", "komparativ": "", "superlativ": ""}
        self.assertTrue(self._ok("toll", entry))
        self.assertFalse(self._ok("toll besser", entry))

    def test_adverb_still_base_only(self):
        entry = {"type": "adverb", "german": "oft"}
        self.assertTrue(self._ok("oft", entry))
        self.assertFalse(self._ok("oft mehr", entry))


if __name__ == "__main__":
    unittest.main()
