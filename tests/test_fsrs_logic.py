import os
import sqlite3
import tempfile
import unittest
import warnings
from datetime import date, timedelta

from bot.storage import (
    init_db,
    add_word,
    select_due,
    update_fsrs,
    list_topics,
    learning_get_or_create_batch,
    learning_add_words,
    get_daily_first_touch_retention,
)

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


class FsrsLogicTests(unittest.TestCase):
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

    def test_update_fsrs_wrong_moves_due_tomorrow_soft_step_back(self):
        today = date(2026, 2, 24)
        srs = {"interval": 3, "repetitions": 2, "ease": 2.5, "due": today.isoformat()}
        updated = update_fsrs(srs, quality=2, today=today)
        self.assertEqual(updated["interval"], 1)
        self.assertEqual(updated["repetitions"], 1)
        self.assertEqual(updated["due"], (today + timedelta(days=1)).isoformat())

    def test_update_fsrs_correct_increases_interval(self):
        today = date(2026, 2, 24)
        srs = {"interval": 1, "repetitions": 0, "ease": 2.5, "due": today.isoformat()}
        updated = update_fsrs(srs, quality=4, today=today)
        self.assertEqual(updated["interval"], 1)
        self.assertEqual(updated["repetitions"], 1)
        self.assertEqual(updated["due"], (today + timedelta(days=1)).isoformat())

    def test_update_fsrs_interval_divisor_halves_interval(self):
        today = date(2026, 2, 24)
        srs = {"interval": 6, "repetitions": 2, "ease": 2.5, "due": today.isoformat()}
        updated = update_fsrs(srs, quality=4, today=today, interval_divisor=2.0)
        self.assertEqual(updated["interval"], 8)
        self.assertEqual(updated["due"], (today + timedelta(days=8)).isoformat())

    def test_update_fsrs_interval_divisor_keeps_min_one(self):
        today = date(2026, 2, 24)
        srs = {"interval": 1, "repetitions": 0, "ease": 2.5, "due": today.isoformat()}
        updated = update_fsrs(srs, quality=4, today=today, interval_divisor=2.0)
        self.assertEqual(updated["interval"], 1)
        self.assertEqual(updated["due"], (today + timedelta(days=1)).isoformat())

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

    def test_select_due_prioritizes_learning_completed_words(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        e_learning = _add_noun(db_path, "Apfel", "яблоко", topic="1")
        e_old = _add_noun(db_path, "Jacke", "куртка", topic="1")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct) VALUES (?, ?, 'ru_de', 1)",
                (1, e_old["id"]),
            )
            conn.execute(
                """
                INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (1, e_old["id"], "ru_de", 2, 1, 2.5, date.today().isoformat()),
            )
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[e_learning], batch_id=batch_id, first_delay_seconds=30)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE learning_items SET status = 'completed' WHERE user_id = ? AND word_id = ?",
                (1, e_learning["id"]),
            )
        due = select_due(db_path, user_id=1, mode="ru_de")
        self.assertGreaterEqual(len(due), 2)
        self.assertEqual(due[0]["id"], e_learning["id"])

    def test_list_topics(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        _add_noun(db_path, "Apfel", "яблоко", topic="1")
        _add_noun(db_path, "Jacke", "куртка", topic="2")
        topics = list_topics(db_path)
        self.assertEqual(topics, ["1", "2"])

    def test_daily_retention_uses_only_first_touch_per_word(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        e1 = _add_noun(db_path, "Apfel", "яблоко", topic="1")
        e2 = _add_noun(db_path, "Jacke", "куртка", topic="1")
        today = date.today().isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct, created_at) VALUES (?, ?, 'ru_de', 0, ?)",
                (1, e1["id"], f"{today} 08:00:00"),
            )
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct, created_at) VALUES (?, ?, 'ru_de', 1, ?)",
                (1, e1["id"], f"{today} 08:05:00"),
            )
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct, created_at) VALUES (?, ?, 'de_ru', 1, ?)",
                (1, e2["id"], f"{today} 09:00:00"),
            )
        stats = get_daily_first_touch_retention(db_path, user_id=1, target_date=today)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["correct"], 1)
        self.assertEqual(stats["rate"], 50.0)


if __name__ == "__main__":
    unittest.main()
