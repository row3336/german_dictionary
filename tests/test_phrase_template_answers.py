"""Optional ellipsis placeholders affect only complete phrase answers."""

import copy
import unittest
from unittest.mock import patch

from tests._telegram_stub import install
install()

from bot.common.phrase_answers import phrase_answer_matches
from bot.handlers import quiz


def phrase(german, **fields):
    return {"type": "phrase", "german": german, **fields}


class PhraseTemplateAnswersTest(unittest.TestCase):
    def check(self, entry, answer, synonyms=None):
        before = copy.deepcopy(entry)
        with patch.object(quiz, "_synonym_entries", return_value=synonyms or [entry]):
            result = quiz._check_ru_de_strict(entry, answer)
        self.assertEqual(entry, before)
        return result

    def test_optional_ascii_and_unicode_placeholders(self):
        for words in ("Wem gehört", "Seit wann", "Äußert Grüße"):
            for stored in (f"{words} ...?", f"{words} …?", f"{words}...?", f"{words}… ?"):
                for answer in (f"{words}?", f"{words} ?", f"{words} ...?",
                               f"{words}…?", f"  {words.upper().replace('SS', 'ẞ')}  …  ?\n"):
                    with self.subTest(stored=stored, answer=answer):
                        self.assertEqual(self.check(phrase(stored), answer), (True, {}))

    def test_interior_placeholder_preserves_word_boundary(self):
        for stored in ("Ich ... dir?", "Ich…dir?", "Ich...dir?"):
            for answer, accepted in (("Ich dir?", True), ("Ich…dir?", True),
                                     ("Ich ... dir?", True), ("Ichdir?", False),
                                     ("Ich irgendetwas dir?", False)):
                with self.subTest(stored=stored, answer=answer):
                    self.assertEqual(self.check(phrase(stored), answer)[0], accepted)

    def test_reject_empty_missing_words_and_wrong_content(self):
        for answer in ("", "  ", "...", "…?", "???", "Wem?", "gehört?",
                       "Wem gehoert?", "Wem gehort?", "Wem gehört das?",
                       "Seit wann?", "Wem gehört!", "Wem gehört"):
            with self.subTest(answer=answer):
                self.assertEqual(self.check(phrase("Wem gehört ...?"), answer), (False, {}))
        for stored in ("...", "…?", "", None):
            with self.subTest(stored=stored):
                self.assertFalse(phrase_answer_matches(stored, "…?"))

    def test_parentheses_hints_and_meaningful_words_stay_required(self):
        entry = phrase("Seit wann ... (genau)?")
        self.assertTrue(self.check(entry, "Seit wann (genau)?")[0])
        for answer in ("Seit wann?", "Seit (genau)?", "Seit wann genau?"):
            with self.subTest(answer=answer):
                self.assertFalse(self.check(entry, answer)[0])

    def test_numeric_phrases_and_templates_keep_meaningful_content(self):
        for stored in ("42", "08/15", "12:30", "24.10.2026"):
            for answer in (stored, f" {stored} ", f"{stored}..."):
                with self.subTest(stored=stored, answer=answer):
                    self.assertEqual(self.check(phrase(stored), answer), (True, {}))
        for stored in ("42 ...?", "42 …?"):
            for answer in ("42?", "42 ...?", "42…?"):
                with self.subTest(stored=stored, answer=answer):
                    self.assertTrue(self.check(phrase(stored), answer)[0])
            for answer in ("?", "…?", "43?", "4?"):
                with self.subTest(stored=stored, answer=answer):
                    self.assertFalse(self.check(phrase(stored), answer)[0])
        for stored in ("", " ", "...", "…?", "?!", "()", "---", ", ."):
            with self.subTest(punctuation_only=stored):
                self.assertFalse(self.check(phrase(stored), stored)[0])

    def test_ordinary_phrases_keep_existing_punctuation_contract(self):
        cases = (
            ("Das Licht geht nicht.", "das licht geht nicht", True),
            ("Guten Tag, Anna.", "guten tag anna", True),
            ("Wie geht es?", "Wie geht es?", True),
            ("Wie geht es?", "Wie geht es ?", False),
            ("Wie geht es?", "Wie geht es…?", False),
            ("Wie geht es?", "Wie geht es", False),
            ("Hallo!", "Hallo", False),
            ("Auf Wiedersehen", "Aufsehen", False),
        )
        for stored, answer, accepted in cases:
            with self.subTest(stored=stored, answer=answer):
                self.assertEqual(self.check(phrase(stored), answer)[0], accepted)

    def test_synonym_phrase_templates_and_other_types_cannot_bypass(self):
        entry = phrase("Wem gehört ...?", id="first")
        synonyms = [phrase("Seit wann …?", id="second"),
                    {"type": "verb", "infinitive": "gehen"},
                    {"type": "adverb", "german": "heimlich"}]
        before = copy.deepcopy([entry, synonyms])
        with patch.object(quiz, "get_db_path", return_value="synthetic"), \
                patch.object(quiz, "get_synonym_group", return_value=synonyms):
            for answer, accepted in (("Wem gehört?", True), ("Seit wann?", True),
                                     ("Seit?", False), ("gehen", False), ("heimlich", False)):
                with self.subTest(answer=answer):
                    self.assertEqual(quiz._check_ru_de_strict(entry, answer)[0], accepted)
        self.assertEqual([entry, synonyms], before)

    def test_other_pos_keep_existing_answer_contracts(self):
        cases = (
            ({"type": "adverb", "german": "Wem gehört ...?"}, "Wem gehört?", False),
            ({"type": "adverb", "german": "Wem gehört …?"}, "Wem gehört?", False),
            ({"type": "nomen", "article": "das", "german": "Haus", "plural": "Häuser"},
             "das Haus Häuser", True),
            ({"type": "nomen", "article": "das", "german": "Haus", "plural": "Häuser"},
             "der Haus Häuser", False),
            ({"type": "verb", "infinitive": "gehen", "prateritum": "ging",
              "partizip2": "gegangen"}, "gehen gegangen", True),
            ({"type": "verb", "infinitive": "gehen", "prateritum": "ging",
              "partizip2": "gegangen"}, "gehen", False),
            ({"type": "adjektiv", "german": "gut", "komparativ": "besser",
              "superlativ": "am besten"}, "gut, besser, am besten", True),
            ({"type": "adjektiv", "german": "gut", "komparativ": "besser",
              "superlativ": "am besten"}, "gut am besten", False),
            ({"type": "verb", "infinitive": "sich waschen", "reflexive_case": "akkusativ",
              "prateritum": "wusch sich", "partizip2": "gewaschen"},
             "sich waschen gewaschen", True),
            ({"type": "verb", "infinitive": "sich waschen", "reflexive_case": "akkusativ",
              "prateritum": "wusch sich", "partizip2": "gewaschen"},
             "waschen gewaschen", False),
        )
        for entry, answer, accepted in cases:
            with self.subTest(entry=entry, answer=answer):
                self.assertEqual(self.check(entry, answer)[0], accepted)

    def test_written_handler_accepts_templates_in_all_l1_directions(self):
        # Reuse the real in-memory transaction/transport fixture, not its tests.
        from tests.test_new_logic_written_answers import WrittenTests

        for lang in ("ru", "uk", "en"):
            for answer, accepted in (("Wem gehört?", True), ("Wem gehört…?", True),
                                     ("Wem?", False)):
                with self.subTest(lang=lang, answer=answer):
                    fixture = WrittenTests()
                    fixture.setUp()
                    try:
                        fixture.pair = f"{lang}_de"
                        # Phrase-only modes use oral self-grading. This checks
                        # the actual typed-answer handler shared with Learning.
                        mode = fixture.pair
                        fixture.conn.execute("UPDATE words SET type='phrase', german='Wem gehört ...?' WHERE id='a'")
                        fixture.conn.commit()
                        fixture.start()
                        fixture.answer(answer)
                        events = fixture.rows("quiz_events")
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0]["mode"], mode)
                        self.assertEqual(bool(events[0]["is_correct"]), accepted)
                        self.assertEqual(fixture.word("a")["german"], "Wem gehört ...?")
                    finally:
                        fixture.doCleanups()

    def test_written_numeric_answers_have_correct_event_and_srs(self):
        from tests.test_new_logic_written_answers import BASE, WrittenTests

        cases = (("42", "42"), ("08/15", "08/15"), ("12:30", "12:30"),
                 ("24.10.2026", "24.10.2026"), ("42 ...?", "42?"), ("42 …?", "42…?"))
        for lang in ("ru", "uk", "en"):
            for stored, answer in cases:
                with self.subTest(lang=lang, stored=stored, answer=answer):
                    fixture = WrittenTests()
                    fixture.setUp()
                    try:
                        fixture.pair = f"{lang}_de"
                        fixture.conn.execute("UPDATE words SET type='phrase', german=? WHERE id='a'", (stored,))
                        fixture.conn.commit()
                        fixture.baseline()
                        fixture.start()
                        fixture.answer(answer)
                        events = fixture.rows("quiz_events")
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0]["mode"], fixture.pair)
                        self.assertEqual(events[0]["is_correct"], 1)
                        progress = fixture.rows("progress")
                        self.assertEqual(len(progress), 1)
                        self.assertEqual(progress[0]["repetitions"], BASE["repetitions"] + 1)
                        self.assertGreater(progress[0]["interval"], BASE["interval"])
                        self.assertGreater(progress[0]["ease"], BASE["ease"])
                        self.assertEqual(fixture.word("a")["german"], stored)
                    finally:
                        fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
