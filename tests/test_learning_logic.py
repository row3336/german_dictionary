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
    learning_preview_entry,
    learning_mark_preview_next,
    learning_reset_session,
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
        self.assertEqual(snap["preview_pending"], 3)
        self.assertFalse(snap["ready"])

    def test_preview_cycle_requires_next_before_due(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        w1 = _add_noun(db_path, "Apfel", "яблоко")
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[w1], batch_id=batch_id, first_delay_seconds=30)

        preview = learning_preview_entry(db_path, user_id=1)
        self.assertIsNotNone(preview)
        self.assertEqual(preview["id"], w1["id"])
        self.assertIsNone(learning_due_entry(db_path, user_id=1))

        ok = learning_mark_preview_next(db_path, user_id=1, word_id=w1["id"], first_interval_seconds=30)
        self.assertTrue(ok)
        self.assertIsNone(learning_preview_entry(db_path, user_id=1))

    def test_reset_learning_session_clears_items(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        w1 = _add_noun(db_path, "Apfel", "яблоко")
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[w1], batch_id=batch_id, first_delay_seconds=30)
        learning_reset_session(db_path, user_id=1)
        snap = learning_status_snapshot(db_path, user_id=1, quorum_pct=70)
        self.assertEqual(snap["active_count"], 0)

    def test_reset_learning_keeps_completed_and_resets_only_active(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        w1 = _add_noun(db_path, "Apfel", "яблоко")
        w2 = _add_noun(db_path, "Katze", "кошка")
        batch_id = learning_get_or_create_batch(db_path, user_id=1, daily_qty=20)
        learning_add_words(db_path, user_id=1, words=[w1, w2], batch_id=batch_id, first_delay_seconds=30)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE learning_items SET status = 'completed' WHERE user_id = ? AND word_id = ?",
                (1, w2["id"]),
            )
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct) VALUES (?, ?, 'learning', 1)",
                (1, w1["id"]),
            )
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct) VALUES (?, ?, 'learning', 1)",
                (1, w2["id"]),
            )

        learning_reset_session(db_path, user_id=1)

        with sqlite3.connect(db_path) as conn:
            active_cnt = conn.execute(
                "SELECT COUNT(*) FROM learning_items WHERE user_id = ? AND status = 'active'",
                (1,),
            ).fetchone()[0]
            completed_cnt = conn.execute(
                "SELECT COUNT(*) FROM learning_items WHERE user_id = ? AND status = 'completed'",
                (1,),
            ).fetchone()[0]
            w1_learning_events = conn.execute(
                "SELECT COUNT(*) FROM quiz_events WHERE user_id = ? AND mode = 'learning' AND word_id = ?",
                (1, w1["id"]),
            ).fetchone()[0]
            w2_learning_events = conn.execute(
                "SELECT COUNT(*) FROM quiz_events WHERE user_id = ? AND mode = 'learning' AND word_id = ?",
                (1, w2["id"]),
            ).fetchone()[0]

        self.assertEqual(active_cnt, 0)
        self.assertEqual(completed_cnt, 1)
        self.assertEqual(w1_learning_events, 0)
        self.assertEqual(w2_learning_events, 1)

        picked = learning_pick_new_words(db_path, user_id=1, limit=10)
        picked_ids = {x["id"] for x in picked}
        self.assertIn(w1["id"], picked_ids)
        self.assertNotIn(w2["id"], picked_ids)


if __name__ == "__main__":
    unittest.main()
