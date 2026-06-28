"""P6.1 — storage helpers for error reports + feedback.

Users add their own rows; the owner lists all pending, closes (status->reviewed),
or deletes. No telegram needed (storage level).
"""

import os
import sqlite3
import tempfile
import unittest

from bot.storage import (
    add_error_report,
    add_feedback,
    delete_error_report,
    delete_feedback,
    init_db,
    list_error_reports,
    list_feedback,
    set_error_report_status,
    set_feedback_status,
)

U1, U2 = 111, 222


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    init_db(tmp.name)
    # A real word for the error_reports FK.
    conn = sqlite3.connect(tmp.name)
    conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1', 'nomen', 'Haus', 'дом')")
    conn.commit()
    conn.close()
    return tmp.name


class ErrorReportsTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))

    def test_add_list_close_delete(self):
        rid = add_error_report(self.db, U1, "w1", "wrong plural", mode="ru_de", state="QUIZ_RU_DE", last_action="answered")
        pending = list_error_reports(self.db, status="pending")
        self.assertEqual(len(pending), 1)
        item = pending[0]
        self.assertEqual(item["id"], rid)
        self.assertEqual(item["user_id"], U1)
        self.assertEqual(item["comment"], "wrong plural")
        self.assertEqual(item["mode"], "ru_de")
        self.assertEqual(item["german"], "Haus")  # joined from words
        # Close -> drops out of pending.
        set_error_report_status(self.db, rid, "reviewed")
        self.assertEqual(list_error_reports(self.db, status="pending"), [])
        self.assertEqual(len(list_error_reports(self.db, status="reviewed")), 1)
        # Delete removes entirely.
        delete_error_report(self.db, rid)
        self.assertEqual(list_error_reports(self.db, status="reviewed"), [])

    def test_owner_sees_all_users(self):
        add_error_report(self.db, U1, "w1", "from u1")
        add_error_report(self.db, U2, "w1", "from u2")
        pending = list_error_reports(self.db, status="pending")
        self.assertEqual({r["user_id"] for r in pending}, {U1, U2})

    def test_invalid_status_rejected(self):
        rid = add_error_report(self.db, U1, "w1", "x")
        with self.assertRaises(ValueError):
            set_error_report_status(self.db, rid, "bogus")


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))

    def test_add_list_close_delete(self):
        fid = add_feedback(self.db, U1, "great bot, add dark mode")
        pending = list_feedback(self.db, status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], fid)
        self.assertEqual(pending[0]["text"], "great bot, add dark mode")
        set_feedback_status(self.db, fid, "reviewed")
        self.assertEqual(list_feedback(self.db, status="pending"), [])
        delete_feedback(self.db, fid)
        self.assertEqual(list_feedback(self.db, status="reviewed"), [])


if __name__ == "__main__":
    unittest.main()
