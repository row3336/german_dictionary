import os
import sqlite3
import tempfile
import unittest
import warnings
from datetime import date, timedelta

from bot.storage import init_db, add_word, select_due, update_sm2, list_topics, learning_get_or_create_batch, learning_add_words

warnings.filterwarnings("ignore", category=ResourceWarning)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path, german, russian, topic="1"):
    entry = {
        "id": "unique",
        "type": "nomen",
        "article": "der",
        "german": german,
        "plural": "",
        "infinitive": "",
        "prateritum": "",
        "partizip2": "",
        "russian": russian,
        "ukrainian": "",
        "english": "",
        "topic": topic,
    }
    return add_word(db_path, entry)


class Sm2LogicTests(unittest.TestCase):
    def test_select_due_excludes_new_word_until_learning_completed(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        _add_noun(db_path, "Schal", "шарф")
        due = select_due(db_path, user_id=1, mode="ru_de")
        self.assertEqual(len(due), 0)

    def test_select_due_includes_word_after_learning_completed(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path, "Schal", "шарф")
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[entry], batch_id=batch_id, first_delay_seconds=30)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE learning_items SET status = 'completed' WHERE user_id = ? AND word_id = ?",
                (1, entry["id"]),
            )
        due = select_due(db_path, user_id=1, mode="ru_de")
        self.assertEqual(len(due), 1)

    def test_select_due_excludes_future_due(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path, "Schal", "шарф")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (1, entry["id"], "ru_de", 1, 0, 2.5, tomorrow),
            )
        due = select_due(db_path, user_id=1, mode="ru_de")
        self.assertEqual(due, [])

    def test_update_sm2_wrong_moves_due_tomorrow(self):
        today = date(2026, 2, 24)
        srs = {"interval": 3, "repetitions": 2, "ease": 2.5, "due": today.isoformat()}
        updated = update_sm2(srs, quality=2, today=today)
        self.assertEqual(updated["interval"], 1)
        self.assertEqual(updated["repetitions"], 0)
        self.assertEqual(updated["due"], (today + timedelta(days=1)).isoformat())

    def test_update_sm2_correct_increases_interval(self):
        today = date(2026, 2, 24)
        srs = {"interval": 1, "repetitions": 0, "ease": 2.5, "due": today.isoformat()}
        updated = update_sm2(srs, quality=4, today=today)
        self.assertEqual(updated["interval"], 2)
        self.assertEqual(updated["repetitions"], 1)
        self.assertEqual(updated["due"], (today + timedelta(days=2)).isoformat())

    def test_select_due_filters_topics(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        e1 = _add_noun(db_path, "Apfel", "яблоко", topic="1")
        e2 = _add_noun(db_path, "Jacke", "куртка", topic="2")
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[e1, e2], batch_id=batch_id, first_delay_seconds=30)
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE learning_items SET status = 'completed' WHERE user_id = ?", (1,))
        due = select_due(db_path, user_id=1, mode="ru_de", topics=["2"])
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["german"], "Jacke")

    def test_list_topics(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        _add_noun(db_path, "Apfel", "яблоко", topic="1")
        _add_noun(db_path, "Jacke", "куртка", topic="2")
        topics = list_topics(db_path)
        self.assertEqual(topics, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
