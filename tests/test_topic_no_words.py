"""spec #8 block 7 — 'no words under the selected topics' vs 'no due' distinction."""

import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.quiz as Q  # noqa: E402
from bot.storage import count_words_for_topics, init_db, set_user_setting  # noqa: E402

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
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        _seed_words(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_empty_topic_uses_topic_message(self):
        set_user_setting(self.db, U, "topic_filter", "05")  # topic with no words
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "topic_no_words_quiz")
        self.assertEqual(Q._no_words_key(U, "no_due_nouns"), "topic_no_words_quiz")

    def test_populated_topic_keeps_due_key(self):
        set_user_setting(self.db, U, "topic_filter", "01")  # has words -> just not due
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "no_due_words")
        self.assertEqual(Q._no_words_key(U, "no_due_pl_sg"), "no_due_pl_sg")

    def test_all_filter_keeps_due_key(self):
        set_user_setting(self.db, U, "topic_filter", "all")
        self.assertEqual(Q._no_words_key(U, "no_due_words"), "no_due_words")


if __name__ == "__main__":
    unittest.main()
