"""Sprint 290826 §1: scope multiline examples to Telegram quiz surfaces."""

import unittest

from bot.ui.formatting import (
    _example_lines,
    _format_de_ru_question,
    _format_feedback_ru_de,
    _format_learning_preview_prompt,
)


def _entry(word_type, example="", image=""):
    return {
        "type": word_type,
        "german": "bei",
        "infinitive": "gehen",
        "russian": "у",
        "example": example,
        "image": image,
    }


class ExampleScopeTest(unittest.TestCase):
    EXAMPLE = "Er bleibt bei mir. Noch heute!\n\nBei Regen bleiben wir drinnen."

    def test_helper_keeps_nonempty_lines_without_splitting_punctuation(self):
        self.assertEqual(
            _example_lines("  Ein Satz. Noch einer!  \n\n  Zweite Zeile?  "),
            ["Ein Satz. Noch einer!", "Zweite Zeile?"],
        )
        self.assertEqual(_example_lines(" \n\t "), [])

    def test_learning_shows_all_preposition_lines_and_first_other_line(self):
        preposition = _format_learning_preview_prompt(_entry("praeposition", self.EXAMPLE), "ru")
        noun = _format_learning_preview_prompt(_entry("nomen", self.EXAMPLE), "ru")

        self.assertIn("Er bleibt bei mir. Noch heute!", preposition)
        self.assertIn("Bei Regen bleiben wir drinnen.", preposition)
        self.assertIn("Er bleibt bei mir. Noch heute!", noun)
        self.assertNotIn("Bei Regen bleiben wir drinnen.", noun)

    def test_ru_de_shows_preposition_example_for_correct_and_incorrect_only(self):
        preposition = _entry("praeposition", self.EXAMPLE, image="image hint")
        noun = _entry("nomen", self.EXAMPLE, image="image hint")

        correct = _format_feedback_ru_de(preposition, True)
        wrong = _format_feedback_ru_de(preposition, False)
        noun_correct = _format_feedback_ru_de(noun, True)
        noun_wrong = _format_feedback_ru_de(noun, False)

        for line in ("Er bleibt bei mir. Noch heute!", "Bei Regen bleiben wir drinnen."):
            self.assertIn(line, correct)
            self.assertIn(line, wrong)
            self.assertNotIn(line, noun_correct)
            self.assertNotIn(line, noun_wrong)
        self.assertNotIn("image hint", correct)
        self.assertIn("image hint", wrong)
        self.assertIn("image hint", noun_wrong)

    def test_de_ru_shows_only_first_example_line_for_every_type(self):
        for word_type in ("praeposition", "nomen", "verb", "phrase"):
            question = _format_de_ru_question(_entry(word_type, self.EXAMPLE))
            self.assertIn("Er bleibt bei mir. Noch heute!", question)
            self.assertNotIn("Bei Regen bleiben wir drinnen.", question)

    def test_empty_example_does_not_create_a_hint_block(self):
        entry = _entry("praeposition", " \n\t ")
        self.assertNotIn("\n\n", _format_learning_preview_prompt(entry, "ru"))
        self.assertNotIn("\n\n", _format_feedback_ru_de(entry, True))
        self.assertNotIn("\n\n", _format_de_ru_question(entry))


if __name__ == "__main__":
    unittest.main()
