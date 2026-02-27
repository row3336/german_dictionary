import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
import warnings

from bot.storage import (
    add_word,
    get_form_review_stage,
    get_form_state,
    init_db,
    list_weak_words,
    update_form_stats,
    update_weak_review_state,
)

warnings.filterwarnings("ignore", category=ResourceWarning)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path, german="Jacke", russian="куртка"):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "nomen",
            "article": "die",
            "german": german,
            "plural": "Jacken",
            "infinitive": "",
            "prateritum": "",
            "partizip2": "",
            "russian": russian,
            "ukrainian": "",
            "english": "",
            "topic": "1",
        },
    )


class WeakModesTests(unittest.TestCase):
    def test_word_enters_active_weak_after_two_errors(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        state = get_form_state(db_path, 1, entry["id"], "article")
        self.assertTrue(state["active"])
        due = list_weak_words(db_path, 1, "article", word_type="nomen")
        self.assertEqual(len(due), 1)

    def test_active_weak_moves_to_review_stage_1_after_three_correct(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        stage = get_form_review_stage(db_path, 1, entry["id"], "article")
        self.assertEqual(stage, 1)
        due_today = list_weak_words(db_path, 1, "article", word_type="nomen")
        self.assertEqual(due_today, [])

    def test_review_stage_progression(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE form_stats SET article_next_review = ? WHERE user_id = ? AND word_id = ?",
                (date.today().isoformat(), 1, entry["id"]),
            )

        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 2)
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", False), 1)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT article_next_review FROM form_stats WHERE user_id = ? AND word_id = ?",
                (1, entry["id"]),
            ).fetchone()
        self.assertEqual(row[0], (date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 2)
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 3)
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 0)


if __name__ == "__main__":
    unittest.main()
