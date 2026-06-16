import os
import sqlite3
import tempfile
import unittest
import warnings
from datetime import date, timedelta

from bot.storage import add_word, init_db, select_due, sm2_due_forecast

warnings.filterwarnings("ignore", category=ResourceWarning)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path, german, russian, topic="1"):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "nomen",
            "article": "der",
            "german": german,
            "russian": russian,
            "ukrainian": "",
            "english": "",
            "topic": topic,
        },
    )


def _event(conn, user_id, word_id, mode, is_correct, created_at):
    conn.execute(
        "INSERT INTO quiz_events (user_id, word_id, mode, is_correct, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, word_id, mode, is_correct, created_at),
    )


def _progress(conn, user_id, word_id, mode, due, interval=1, reps=1, ease=2.5):
    conn.execute(
        "INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, word_id, mode, interval, reps, ease, due),
    )


class SelectDueGatingTests(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.today = date.today().isoformat()

    def test_non_learning_event_introduces_word(self):
        """A word with any non-learning quiz_event becomes due (no learning_items needed)."""
        e = _add_noun(self.db, "Schal", "шарф")
        # not introduced yet
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de")), 0)
        with sqlite3.connect(self.db) as conn:
            _event(conn, 1, e["id"], "ru_de", 0, f"{self.today} 08:00:00")
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de")), 1)

    def test_progress_alone_does_not_introduce(self):
        """Progress row without any learning/event does NOT make a word due."""
        e = _add_noun(self.db, "Schal", "шарф")
        with sqlite3.connect(self.db) as conn:
            _progress(conn, 1, e["id"], "ru_de", self.today)
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de")), 0)

    def test_exclude_correct_today(self):
        """A word answered correctly today is dropped only when exclude_correct_today=True."""
        e = _add_noun(self.db, "Schal", "шарф")
        with sqlite3.connect(self.db) as conn:
            _event(conn, 1, e["id"], "ru_de", 1, f"{self.today} 09:00:00")
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de", exclude_correct_today=False)), 1)
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de", exclude_correct_today=True)), 0)

    def test_user_isolation(self):
        """Another user's progress must not affect this user's queue (progress JOIN binds user_id)."""
        e = _add_noun(self.db, "Schal", "шарф")
        with sqlite3.connect(self.db) as conn:
            _event(conn, 1, e["id"], "ru_de", 0, f"{self.today} 08:00:00")
            # user 2 has it scheduled far in the future — irrelevant to user 1
            future = (date.today() + timedelta(days=999)).isoformat()
            _progress(conn, 2, e["id"], "ru_de", future)
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de")), 1)

    def test_mode_isolation(self):
        """Future due in ru_de must not hide the word in de_ru (progress JOIN binds mode)."""
        e = _add_noun(self.db, "Schal", "шарф")
        future = (date.today() + timedelta(days=999)).isoformat()
        with sqlite3.connect(self.db) as conn:
            _event(conn, 1, e["id"], "de_ru", 0, f"{self.today} 08:00:00")
            _progress(conn, 1, e["id"], "ru_de", future)  # scheduled far out in ru_de only
        # ru_de: future-due -> excluded
        self.assertEqual(len(select_due(self.db, user_id=1, mode="ru_de")), 0)
        # de_ru: no progress -> due today
        self.assertEqual(len(select_due(self.db, user_id=1, mode="de_ru")), 1)

    def test_forecast_total_matches_due_queue(self):
        """sm2_due_forecast total equals select_due count (notification vs actual queue)."""
        for i in range(3):
            e = _add_noun(self.db, f"Wort{i}", f"слово{i}")
            with sqlite3.connect(self.db) as conn:
                _event(conn, 1, e["id"], "ru_de", 0, f"{self.today} 08:0{i}:00")
        due = select_due(self.db, user_id=1, mode="ru_de")
        forecast = sm2_due_forecast(self.db, user_id=1, mode="ru_de")
        self.assertEqual(forecast["total"], len(due))
        self.assertEqual(forecast["total"], 3)


if __name__ == "__main__":
    unittest.main()
