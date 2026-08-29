"""todo P12 — in topic/type lists show semantic preposition subtype/case.

The issue was that prepositions in compact topic/type lists showed only
`[praeposition]`, so different semantic meanings for the same lexical form
(eg `in` local vs temporal) were visually indistinguishable.
"""

import unittest

from bot.handlers.db_search import _format_topic_learning_line, _format_topic_words_line


class DbSearchTopicWordsLineTest(unittest.TestCase):
    def test_praeposition_shows_subtype_and_case(self):
        line = _format_topic_words_line({
            "type": "praeposition",
            "german": "in",
            "preposition_type": "local",
            "case_type": "dativ",
        })
        self.assertEqual(line, "in [praeposition, local, +Dativ]")

    def test_praeposition_keeps_only_existing_fields(self):
        line = _format_topic_words_line({
            "type": "praeposition",
            "german": "in",
            "preposition_type": "local",
        })
        self.assertEqual(line, "in [praeposition, local]")

        line = _format_topic_words_line({
            "type": "praeposition",
            "german": "in",
            "case_type": "akkusativ",
        })
        self.assertEqual(line, "in [praeposition, +Akkusativ]")

        line = _format_topic_words_line({"type": "praeposition", "german": "in"})
        self.assertEqual(line, "in [praeposition]")

    def test_non_preposition_is_unchanged(self):
        line = _format_topic_words_line({
            "type": "verb",
            "infinitive": "gehen",
            "preposition_type": "local",
            "case_type": "dativ",
        })
        self.assertEqual(line, "gehen [verb]")

    def test_distinguishes_same_lexeme_with_different_semantics(self):
        temporal = _format_topic_words_line({
            "type": "praeposition",
            "german": "in",
            "preposition_type": "temporal",
            "case_type": "dativ",
        })
        local = _format_topic_words_line({
            "type": "praeposition",
            "german": "in",
            "preposition_type": "local",
            "case_type": "akkusativ",
        })
        self.assertNotEqual(temporal, local)

    def test_learning_line_ignores_preposition_semantics(self):
        line = _format_topic_learning_line({
            "type": "praeposition",
            "german": "in",
            "topic": "Ort",
            "preposition_type": "local",
            "case_type": "wechsel",
        })
        self.assertEqual(line, "[Ort] - [in]")


if __name__ == "__main__":
    unittest.main()
