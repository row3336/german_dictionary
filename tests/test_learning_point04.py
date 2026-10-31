"""Learning point 4: recovery, durable actions, correction, and old deadlines."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bot.learning_logic import reduce_answer
from bot.common.db_safety import _apply_migration_dispatcher, get_user_version, set_user_version
from bot.learning_store import (
    apply_action,
    correct_last,
    get_display,
    mark_sent,
    recover_display,
    start_display,
    undo_learning_log,
)
from bot.storage import _connect, init_db


SCHEDULE = [0, 0, 0, 60, 120]
NOW = datetime(2026, 10, 31, 14, tzinfo=timezone.utc)


class ReducerTests(unittest.TestCase):
    def test_warmup_and_periodic_errors_have_different_rules(self):
        for step in range(3):
            before = {"step_index": step, "recovery_remaining": 0,
                      "status": "active", "next_due": "old"}
            after = reduce_answer(before, "wrong", SCHEDULE, NOW)
            self.assertEqual((after["step_index"], after["recovery_remaining"]), (0, 0))
        before = {"step_index": 3, "recovery_remaining": 0,
                  "status": "active", "next_due": "old"}
        for kind in ("wrong", "idk"):
            after = reduce_answer(before, kind, SCHEDULE, NOW)
            self.assertEqual((after["step_index"], after["recovery_remaining"]), (3, 3))
            for remaining in (2, 1, 0):
                after = reduce_answer(after, "correct", SCHEDULE, NOW)
                self.assertEqual(after["recovery_remaining"], remaining)
            self.assertEqual(after["step_index"], 4)
            self.assertEqual(after["next_due"], "2026-10-31T14:02:00Z")

    def test_error_during_recovery_restarts_at_three(self):
        before = {"step_index": 4, "recovery_remaining": 1,
                  "status": "active", "next_due": "old"}
        after = reduce_answer(before, "wrong", SCHEDULE, NOW)
        self.assertEqual((after["step_index"], after["recovery_remaining"]), (4, 3))
        for _ in range(3):
            after = reduce_answer(after, "correct", SCHEDULE, NOW)
        self.assertEqual(after["status"], "completed")


class StoreTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = handle.name
        handle.close()
        self.addCleanup(lambda: os.unlink(self.db_path))
        init_db(self.db_path)
        with _connect(self.db_path) as conn:
            conn.execute("INSERT INTO users(user_id,status) VALUES(900001,'approved')")
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES('b1',900001)")
            for word in ("A", "B"):
                conn.execute("INSERT INTO words(id,type,german) VALUES(?,'nomen',?)", (word, word))

    def item(self, word):
        with _connect(self.db_path) as conn:
            return dict(conn.execute("SELECT * FROM learning_items WHERE word_id=?", (word,)).fetchone())

    def add_item(self, word, step, due, preview=1, pending=0):
        with _connect(self.db_path) as conn:
            conn.execute("""INSERT INTO learning_items
                (user_id,word_id,batch_id,step_index,preview_done,next_due,status,
                 cycle_token,first_due_pending)
                VALUES (900001,?,'b1',?,?,?,'active',?,?)""",
                (word, step, preview, due, "token-" + word, pending))

    def confirm(self, display, message_id):
        self.assertTrue(mark_sent(self.db_path, 900001, display, "confirmed", message_id))

    def test_three_correct_answers_after_periodic_error_and_replay(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        result = apply_action(self.db_path, 900001, 1, frame["action_key"],
                              "wrong", SCHEDULE, "wrong", now=NOW)
        self.assertEqual(result["receipt"]["after_rows"]["A"]["recovery_remaining"], 3)
        self.assertEqual(apply_action(self.db_path, 900001, 1, frame["action_key"],
                                      "wrong", SCHEDULE, "wrong", now=NOW)["status"], "replayed")
        for count, remaining in enumerate((2, 1, 0), 2):
            frame = result["display"]
            self.confirm(frame, count)
            result = apply_action(self.db_path, 900001, count, frame["action_key"],
                                  "correct", SCHEDULE, "correct", now=NOW)
            self.assertEqual(result["receipt"]["after_rows"]["A"]["recovery_remaining"], remaining)
        self.assertEqual(self.item("A")["step_index"], 4)
        with _connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 4)

    def test_correction_and_two_undos_restore_before(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        wrong = apply_action(self.db_path, 900001, 1, frame["action_key"],
                             "wrong", SCHEDULE, "wrong", now=NOW)
        self.confirm(wrong["display"], 2)
        correction = correct_last(self.db_path, 900001, 2, frame["action_key"],
                                  SCHEDULE, "fixed", now=NOW)
        self.assertEqual(self.item("A")["step_index"], 4)
        with _connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 1)
        self.assertEqual(undo_learning_log(self.db_path, 900001,
                                           correction["receipt"]["tx_id"])["status"], "restored")
        self.assertEqual((self.item("A")["step_index"], self.item("A")["recovery_remaining"]), (3, 3))
        self.assertEqual(undo_learning_log(self.db_path, 900001,
                                           wrong["receipt"]["tx_id"])["status"], "restored")
        self.assertEqual((self.item("A")["step_index"], self.item("A")["recovery_remaining"]), (3, 0))

    def test_preview_assigns_only_new_first_deadline(self):
        self.add_item("A", 3, "2026-10-31T16:00:00Z")
        self.add_item("B", 0, "2026-10-31T14:00:00Z", preview=0, pending=1)
        frame = start_display(self.db_path, 900001, now=NOW)
        self.assertEqual((frame["phase"], frame["word_id"]), ("preview", "B"))
        self.confirm(frame, 1)
        result = apply_action(self.db_path, 900001, 1, frame["action_key"],
                              "preview_next", SCHEDULE, "next", now=NOW)
        self.assertEqual(self.item("A")["next_due"], "2026-10-31T16:00:00Z")
        self.assertEqual(self.item("B")["first_due_pending"], 0)
        undo_learning_log(self.db_path, 900001, result["receipt"]["tx_id"])
        self.assertEqual(self.item("A")["next_due"], "2026-10-31T16:00:00Z")
        self.assertEqual(self.item("B")["preview_done"], 0)

    def test_uncertain_send_consumes_first_follow_up_without_grading(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        mark_sent(self.db_path, 900001, frame, "uncertain")
        recovered = recover_display(self.db_path, 900001, 1, now=NOW)
        self.assertEqual(recovered["display"]["action_key"], frame["action_key"])
        self.assertEqual(get_display(self.db_path, 900001)["send_state"], "planned")
        self.assertEqual(self.item("A")["step_index"], 3)
        self.confirm(recovered["display"], 2)
        result = apply_action(self.db_path, 900001, 2, frame["action_key"],
                              "wrong", SCHEDULE, "wrong", now=NOW)
        self.assertEqual(result["receipt"]["after_rows"]["A"]["recovery_remaining"], 3)


    def test_second_update_for_consumed_question_does_not_grade_next_word(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        self.add_item("B", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        first = apply_action(self.db_path, 900001, 1, frame["action_key"],
                             "wrong", SCHEDULE, "wrong", recent_ids=[frame["word_id"]], now=NOW)
        self.assertNotEqual(first["display"]["word_id"], frame["word_id"])
        again = apply_action(self.db_path, 900001, 2, frame["action_key"],
                             "wrong", SCHEDULE, "wrong", now=NOW)
        self.assertEqual(again["status"], "replayed")
        with _connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_actions").fetchone()[0], 1)

    def test_sql_failure_rolls_back_word_event_receipt_and_display(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        with _connect(self.db_path) as conn:
            conn.execute("""CREATE TRIGGER fail_learning_event BEFORE INSERT ON quiz_events
                WHEN NEW.mode='learning' BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            apply_action(self.db_path, 900001, 1, frame["action_key"],
                         "wrong", SCHEDULE, "wrong", now=NOW)
        self.assertEqual((self.item("A")["step_index"], self.item("A")["recovery_remaining"]), (3, 0))
        self.assertEqual(get_display(self.db_path, 900001)["action_key"], frame["action_key"])
        with _connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_actions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_deliveries").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 0)


    def test_two_words_and_two_users_keep_separate_recovery(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        self.add_item("B", 3, "2020-01-01T00:00:00Z")
        with _connect(self.db_path) as conn:
            conn.execute("INSERT INTO users(user_id,status) VALUES(900002,'approved')")
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES('b2',900002)")
            conn.execute("""INSERT INTO learning_items
                (user_id,word_id,batch_id,step_index,preview_done,next_due,status,
                 cycle_token,first_due_pending)
                VALUES (900002,'A','b2',3,1,'2020-01-01T00:00:00Z','active','other-cycle',0)""")
        chosen = {}
        for user_id in (900001, 900002):
            display = start_display(self.db_path, user_id, now=NOW)
            chosen[user_id] = display["word_id"]
            mark_sent(self.db_path, user_id, display, "confirmed", user_id)
            apply_action(self.db_path, user_id, user_id, display["action_key"],
                         "wrong", SCHEDULE, "wrong", now=NOW)
        with _connect(self.db_path) as conn:
            rows = conn.execute("""SELECT user_id,word_id,recovery_remaining
                FROM learning_items ORDER BY user_id,word_id""").fetchall()
        self.assertEqual({(r[0], r[1]): r[2] for r in rows}, {
            (900001, "A"): 3 if chosen[900001] == "A" else 0,
            (900001, "B"): 3 if chosen[900001] == "B" else 0,
            (900002, "A"): 3,
        })

    def test_preview_sql_failure_rolls_back_all_first_deadlines(self):
        self.add_item("A", 0, "2000-01-01T00:00:00Z", pending=1)
        self.add_item("B", 0, "2000-01-01T00:00:00Z", preview=0, pending=1)
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        with _connect(self.db_path) as conn:
            conn.execute("""CREATE TRIGGER fail_first_due BEFORE UPDATE OF first_due_pending
                ON learning_items WHEN OLD.word_id='A' AND NEW.first_due_pending=0
                BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            apply_action(self.db_path, 900001, 1, frame["action_key"],
                         "preview_next", SCHEDULE, "next", now=NOW)
        self.assertEqual(self.item("B")["preview_done"], 0)
        self.assertEqual(self.item("A")["first_due_pending"], 1)
        self.assertEqual(get_display(self.db_path, 900001)["action_key"], frame["action_key"])

    def test_correction_sql_failure_keeps_wrong_and_one_event(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        wrong = apply_action(self.db_path, 900001, 1, frame["action_key"],
                             "wrong", SCHEDULE, "wrong", now=NOW)
        self.confirm(wrong["display"], 2)
        with _connect(self.db_path) as conn:
            conn.execute("""CREATE TRIGGER fail_correction BEFORE UPDATE OF is_correct
                ON quiz_events WHEN NEW.mode='learning'
                BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            correct_last(self.db_path, 900001, 2, frame["action_key"],
                         SCHEDULE, "fixed", now=NOW)
        self.assertEqual(self.item("A")["recovery_remaining"], 3)
        with _connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT is_correct FROM quiz_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_actions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 1)

    def test_undo_sql_failure_leaves_action_and_event_intact(self):
        self.add_item("A", 3, "2020-01-01T00:00:00Z")
        frame = start_display(self.db_path, 900001, now=NOW)
        self.confirm(frame, 1)
        wrong = apply_action(self.db_path, 900001, 1, frame["action_key"],
                             "wrong", SCHEDULE, "wrong", now=NOW)
        with _connect(self.db_path) as conn:
            conn.execute("""CREATE TRIGGER fail_undo BEFORE UPDATE OF recovery_remaining
                ON learning_items WHEN NEW.recovery_remaining=0
                BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            undo_learning_log(self.db_path, 900001, wrong["receipt"]["tx_id"])
        self.assertEqual(self.item("A")["recovery_remaining"], 3)
        with _connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT state FROM study_actions").fetchone()[0], "applied")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 1)


    def test_dispatcher_accepts_prior_schema_versions_without_due_drift(self):
        self.add_item("A", 3, "2026-11-01T16:00:00Z")
        for source_version in (15, 16, 17):
            set_user_version(self.db_path, source_version)
            _apply_migration_dispatcher(self.db_path)
            self.assertEqual(get_user_version(self.db_path), 18)
            self.assertEqual(self.item("A")["next_due"], "2026-11-01T16:00:00Z")
            with _connect(self.db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])



if __name__ == "__main__":
    unittest.main()
