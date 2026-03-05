import os
import sqlite3
import tempfile
import unittest
import warnings

from bot.storage import (
    add_word,
    init_db,
    learning_add_words,
    learning_due_entry,
    learning_get_or_create_batch,
    learning_pick_new_words,
    learning_status_snapshot,
    learning_update_result,
    select_due,
)

warnings.filterwarnings("ignore", category=ResourceWarning)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path, german, russian):
    return add_word(
        db_path,
        {
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
            "topic": "1",
        },
    )


class LearningLogicTests(unittest.TestCase):
    def test_select_due_excludes_active_learning_words(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        w1 = _add_noun(db_path, "Apfel", "яблоко")
        w2 = _add_noun(db_path, "Katze", "кошка")

        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        added = learning_add_words(db_path, user_id=1, words=[w1, w2], batch_id=batch_id, first_delay_seconds=30)
        self.assertEqual(added, 2)
        # Only w2 is released to SM2.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE learning_items SET status = 'completed' WHERE user_id = ? AND word_id = ?",
                (1, w2["id"]),
            )

        due = select_due(db_path, user_id=1, mode="ru_de")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["german"], "Katze")

    def test_learning_wrong_resets_to_step_zero(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        w1 = _add_noun(db_path, "Apfel", "яблоко")
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[w1], batch_id=batch_id, first_delay_seconds=1)

        due = learning_due_entry(db_path, user_id=1)
        self.assertIsNone(due)

        # Force immediate due by using short intervals and two updates.
        result = learning_update_result(db_path, user_id=1, word_id=w1["id"], ok=True, intervals_seconds=[1, 2, 3])
        self.assertFalse(result["completed"])
        result2 = learning_update_result(db_path, user_id=1, word_id=w1["id"], ok=False, intervals_seconds=[1, 2, 3])
        self.assertFalse(result2["completed"])
        self.assertEqual(result2["step_index"], 0)

    def test_learning_pick_new_words_only_untouched(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        _add_noun(db_path, "Apfel", "яблоко")
        _add_noun(db_path, "Katze", "кошка")
        picked = learning_pick_new_words(db_path, user_id=1, limit=2)
        self.assertEqual(len(picked), 2)

    def test_learning_status_ready_quorum(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        words = [
            _add_noun(db_path, "Apfel", "яблоко"),
            _add_noun(db_path, "Katze", "кошка"),
            _add_noun(db_path, "Haus", "дом"),
        ]
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=3)
        learning_add_words(db_path, user_id=1, words=words, batch_id=batch_id, first_delay_seconds=1)
        snap = learning_status_snapshot(db_path, user_id=1, quorum_pct=50)
        self.assertEqual(snap["active_count"], 3)
        self.assertFalse(snap["ready"])


if __name__ == "__main__":
    unittest.main()
