"""P1 storage helpers for per-topic book-level (v14, spec_topics_levels.md).

Covers get/set_topic_level, the dynamic level lists (only levels with words),
and the per-level word counters used by the S1/S2 filter screens.
"""

import os
import tempfile
import unittest

from bot import storage


def _add(db, german, wtype, russian, topic, cefr=""):
    entry = {"german": german, "type": wtype, "russian": russian, "topic": topic, "cefr_level": cefr}
    if wtype == "verb":
        entry["reflexive_case"] = "none"
        entry["infinitive"] = german
    if wtype == "nomen":
        entry["article"] = "das"
    storage.add_word(db, entry)


class TopicLevelHelpersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "words.db")
        storage.init_db(self.db)
        # topic 1 -> A1 (2 words), topic 2 -> B1 (1 word), topic 3 -> unassigned (1 word)
        _add(self.db, "Haus", "nomen", "дом", "1", "A1")
        _add(self.db, "Buch", "nomen", "книга", "1", "A2")
        _add(self.db, "reisen", "verb", "ехать", "2", "B1")
        _add(self.db, "Baum", "nomen", "дерево", "3", "")
        storage.set_topic_level(self.db, "1", "A1")
        storage.set_topic_level(self.db, "2", "B1")
        # topic 3 intentionally left unassigned ('')

    def test_get_set_level(self):
        self.assertEqual(storage.get_topic_level(self.db, "1"), "A1")
        self.assertEqual(storage.get_topic_level(self.db, "3"), "")  # unassigned
        self.assertEqual(storage.get_topic_level(self.db, "nope"), "")  # unknown
        storage.set_topic_level(self.db, "1", "b2")  # case-insensitive
        self.assertEqual(storage.get_topic_level(self.db, "1"), "B2")

    def test_set_level_validates(self):
        with self.assertRaises(ValueError):
            storage.set_topic_level(self.db, "1", "Z9")
        with self.assertRaises(ValueError):
            storage.set_topic_level(self.db, "", "A1")

    def test_set_level_creates_missing_topic(self):
        storage.set_topic_level(self.db, "99", "C1")
        self.assertEqual(storage.get_topic_level(self.db, "99"), "C1")

    def test_list_book_levels_dynamic(self):
        # only levels whose topics hold >=1 word, in CEFR order; unassigned topic 3 excluded
        self.assertEqual(storage.list_book_levels(self.db), ["A1", "B1"])

    def test_list_book_levels_excludes_empty_level(self):
        # assigning a level to a topic that has NO words must not surface that level
        storage.set_topic_level(self.db, "empty", "C2")  # topic 'empty' has no words
        self.assertNotIn("C2", storage.list_book_levels(self.db))

    def test_count_words_for_book_level(self):
        self.assertEqual(storage.count_words_for_book_level(self.db, "A1"), 2)
        self.assertEqual(storage.count_words_for_book_level(self.db, "B1"), 1)
        self.assertEqual(storage.count_words_for_book_level(self.db, "C1"), 0)

    def test_list_topics_for_level(self):
        self.assertEqual(storage.list_topics_for_level(self.db, "A1"), ["1"])
        self.assertEqual(storage.list_topics_for_level(self.db, "B1"), ["2"])
        self.assertEqual(storage.list_topics_for_level(self.db, "C1"), [])

    def test_cefr_levels_with_words_is_independent_of_topic_level(self):
        # a B1-topic word can carry any cefr level; the CEFR list reflects words.cefr_level,
        # NOT topics.level (the two axes are independent)
        self.assertEqual(storage.list_cefr_levels_with_words(self.db), ["A1", "A2", "B1"])

    def test_count_words_for_cefr(self):
        self.assertEqual(storage.count_words_for_cefr(self.db, "A1"), 1)
        self.assertEqual(storage.count_words_for_cefr(self.db, "A2"), 1)
        self.assertEqual(storage.count_words_for_cefr(self.db, "C1"), 0)

    def test_list_topics_with_level(self):
        got = {name: level for name, _desc, level in storage.list_topics_with_level(self.db)}
        self.assertEqual(got["1"], "A1")
        self.assertEqual(got["2"], "B1")
        self.assertEqual(got["3"], "")


if __name__ == "__main__":
    unittest.main()
