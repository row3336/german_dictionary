"""spec #8 block 7 — 'no words under the selected topics' vs 'no due' distinction."""

import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.quiz as Q  # noqa: E402
from bot.common import word_filter as wf  # noqa: E402
from bot.storage import (  # noqa: E402
    count_words_for_topics,
    init_db,
    register_or_update_user,
    set_topic_level,
    set_user_setting,
)

U = 555


def _seed_words(db):
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        INSERT INTO words (id, type, topic) VALUES ('w1', 'nomen', '01');
        INSERT INTO words (id, type, topic) VALUES ('w2', 'verb', '01');
        INSERT INTO words (id, type, topic) VALUES ('w3', 'nomen', '03');
        """
    )
    conn.commit()
    conn.close()


class CountWordsForTopicsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        _seed_words(self.db)

    def test_populated_topic(self):
        self.assertEqual(count_words_for_topics(self.db, ["01"]), 2)
        self.assertEqual(count_words_for_topics(self.db, ["03"]), 1)
        self.assertEqual(count_words_for_topics(self.db, ["01", "03"]), 3)

    def test_empty_topic(self):
        self.assertEqual(count_words_for_topics(self.db, ["05"]), 0)

    def test_none_means_all(self):
        self.assertEqual(count_words_for_topics(self.db, None), 3)
        self.assertEqual(count_words_for_topics(self.db, []), 3)


class QuizNoWordsKeyTest(unittest.TestCase):
    """spec_topics_levels.md §4 — 'no words under this filter' vs 'no due', now driven
    by the mode-aware resolver (count_words_for_filter)."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        _seed_words(self.db)  # topics 01 (2 words), 03 (1 word)
        register_or_update_user(self.db, U, "u", "U")
        # topics 01/03 -> A1 (have words); topic 09 -> B2 (assigned but NO words)
        set_topic_level(self.db, "01", "A1")
        set_topic_level(self.db, "03", "A1")
        set_topic_level(self.db, "09", "B2")
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_book_level_without_words_uses_topic_message(self):
        # topics mode, active level B2 whose only topic (09) holds no words
        wf.set_active_book_level(U, "B2")
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "topic_no_words_quiz")
        self.assertEqual(Q._no_words_key(U, "no_due_nouns"), "topic_no_words_quiz")

    def test_populated_book_level_keeps_due_key(self):
        # default topics/A1/all -> topics 01+03 have words -> just 'no due'
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "no_due_words")
        self.assertEqual(Q._no_words_key(U, "no_due_pl_sg"), "no_due_pl_sg")

    def test_cefr_level_without_words_uses_topic_message(self):
        # cefr mode restricted to C2, which no seeded word has -> topic_no_words
        wf.set_filter_mode(U, "cefr")
        for lvl in ("A1", "A2", "B1", "B2", "C1"):
            from bot.storage import toggle_user_level
            toggle_user_level(self.db, U, lvl)  # leave only C2
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "topic_no_words_quiz")

    def test_cefr_all_keeps_due_key(self):
        # cefr mode, all six levels -> no restriction -> words exist -> 'no due'
        wf.set_filter_mode(U, "cefr")
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "no_due_words")


if __name__ == "__main__":
    unittest.main()
