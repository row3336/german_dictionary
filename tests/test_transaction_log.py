import os
import sqlite3
import tempfile
import unittest

from bot.storage import (
    add_word,
    add_transaction_log,
    delete_transaction_logs,
    init_db,
    list_transaction_logs,
    log_quiz_event,
    restore_user_word_state,
    snapshot_user_word_state,
    update_progress,
)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "nomen",
            "article": "der",
            "german": "Apfel",
            "plural": "Äpfel",
            "infinitive": "",
            "prateritum": "",
            "partizip2": "",
            "komparativ": "",
            "superlativ": "",
            "russian": "яблоко",
            "ukrainian": "",
            "english": "",
            "topic": "1",
        },
    )


class TransactionLogTests(unittest.TestCase):
    def test_snapshot_restore_progress_and_events(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        snap = snapshot_user_word_state(db_path, user_id=1, word_id=entry["id"], modes=["ru_de"])

        update_progress(db_path, user_id=1, word_id=entry["id"], mode="ru_de", quality=2)
        log_quiz_event(db_path, user_id=1, word_id=entry["id"], mode="ru_de", is_correct=False)

        restored = restore_user_word_state(db_path, snap)
        self.assertTrue(restored)
        with sqlite3.connect(db_path) as conn:
            p_cnt = conn.execute(
                "SELECT COUNT(*) FROM progress WHERE user_id = ? AND word_id = ? AND mode = 'ru_de'",
                (1, entry["id"]),
            ).fetchone()[0]
            e_cnt = conn.execute(
                "SELECT COUNT(*) FROM quiz_events WHERE user_id = ? AND word_id = ? AND mode = 'ru_de'",
                (1, entry["id"]),
            ).fetchone()[0]
        self.assertEqual(p_cnt, 0)
        self.assertEqual(e_cnt, 0)

    def test_add_list_delete_transaction_logs(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        add_transaction_log(db_path, user_id=1, action="quiz:ru_de", payload={"snapshot": {"x": 1}})
        add_transaction_log(db_path, user_id=1, action="learning:answer", payload={"snapshot": {"x": 2}})
        logs = list_transaction_logs(db_path, user_id=1, limit=10)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["action"], "learning:answer")
        ids = [logs[0]["id"]]
        delete_transaction_logs(db_path, user_id=1, ids=ids)
        logs_after = list_transaction_logs(db_path, user_id=1, limit=10)
        self.assertEqual(len(logs_after), 1)


if __name__ == "__main__":
    unittest.main()
