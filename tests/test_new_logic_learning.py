"""Learning reducer, real SQLite transactions, and fake Telegram transitions."""

import asyncio
import json
import os
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from _telegram_stub import install
install()

from bot import learning_store as store
from bot.learning_logic import reduce_answer, stamp
from bot.handlers import learning as handler
from bot.storage import (init_db, learning_due_entries, learning_status_snapshot,
                         learning_add_word_by_id, delete_word)
from bot.study import answers, repository

STAND = Path("/Users/wf/py/_agent_work/888_german_dictionary/new_logic_learning_tests")
NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


class Message:
    def __init__(self, text, ident):
        self.text, self.message_id = text, ident
        self.sent, self.fail = [], False

    async def reply_text(self, text, **kwargs):
        if self.fail:
            raise RuntimeError("synthetic send failure")
        self.sent.append((text, kwargs))
        return types.SimpleNamespace(message_id=1000 + len(self.sent))


class LearningTests(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.db = str(Path(tempfile.mkdtemp(prefix="learning-", dir=STAND)) / "synthetic.sqlite")
        init_db(self.db)
        with repository.transaction(self.db) as conn:
            conn.executemany("INSERT INTO users(user_id,status) VALUES (?,'approved')", [(1,), (2,)])
            conn.executemany("INSERT INTO words(id,type,german,russian,english,ukrainian) VALUES (?,'nomen',?,?,?,?)",
                             [("a", "Apfel", "яблоко", "apple", "яблуко"),
                              ("b", "Birne", "груша", "pear", "груша"),
                              ("c", "Buch", "книга", "book", "книга")])
            self.batch = store.get_or_create_batch(conn, 1, 20, NOW)
        self.seq = 0
        self.reactions = []

        async def react(*args, **kwargs):
            self.reactions.append((args, kwargs))

        self.ctx = types.SimpleNamespace(user_data={}, bot=types.SimpleNamespace(set_message_reaction=react))
        self.patches = [patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": "0"}),
                        patch.object(handler, "get_db_path", return_value=self.db),
                        patch.object(handler, "_user_interface_lang", return_value="en"),
                        patch.object(handler, "_user_dict_lang", return_value="ru"),
                        patch.object(handler, "_action_key_from_text", side_effect=lambda text, uid:
                            text if text in {"learning_start", "learning_next", "learning_skip", "input_err",
                                             "back", "clarify_fields", "learning_reset"} else None),
                        patch.object(handler, "_check_ru_de_strict", side_effect=lambda entry, text: (text == "good", None))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def update(self, text, user=1):
        self.seq += 1
        return types.SimpleNamespace(update_id=self.seq, effective_user=types.SimpleNamespace(id=user),
            effective_chat=types.SimpleNamespace(id=user), message=Message(text, self.seq))

    def rows(self, table):
        with repository.transaction(self.db) as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def marker(self):
        return next((row["value"] for row in self.rows("user_settings")
                     if row["user_id"] == 1 and row["key"] == "learning_new_start_date"), None)

    def add(self, ids=("a",), *, ready=False, step=0, recovery=0):
        with repository.transaction(self.db) as conn:
            store.add_items(conn, 1, ids, self.batch, NOW)
            if ready:
                for wid in ids:
                    store.preview_transition(conn, 1, wid, "preview_next", NOW, 0)
                conn.execute("""UPDATE learning_items SET next_due='2000-01-01T00:00:00Z',step_index=?,
                             recovery_remaining=? WHERE user_id=1""", (step, recovery))
            for index, wid in enumerate(ids):
                conn.execute("UPDATE learning_items SET created_at=? WHERE user_id=1 AND word_id=?",
                             (f"2000-01-01 00:00:{index:02}", wid))

    def grade(self, key="answer", kind="wrong", *, now=NOW, intervals=None):
        self.seq += 1
        return answers.apply_original_answer(self.db, 1, "a", "learning", kind, key, self.seq, 1, now,
            flow_id="learning", live_revision=1,
            learning_transition=lambda conn, before, answer, clock:
                store.apply_answer(conn, before, answer, clock, intervals))

    def preview(self, word, action, key):
        self.seq += 1
        return answers.apply_preview_transition(self.db, 1, word, action, key, self.seq, 1, NOW,
            flow_id="learning", live_revision=1,
            affected_learning_ids=lambda conn: store.preview_affected_ids(conn, 1, word),
            transition=lambda conn, before, now: store.preview_transition(conn, 1, word, action, now, 10))

    def test_answer_correction_and_preview_recovery_receipts_precede_late_target(self):
        commands = [
            lambda transport: answers.apply_original_answer(self.db, 1, "missing", "learning", "wrong",
                "late-grade", transport, 1, flow_id="old", live_revision=1),
            lambda transport: answers.correct_last_answer(self.db, 1, "missing", "late-fix",
                transport, 1, flow_id="old", live_revision=1),
            lambda transport: answers.apply_preview_transition(self.db, 1, "missing", "preview_next",
                "late-preview", transport, 1, flow_id="old", live_revision=1,
                transition=lambda *args: self.fail("recovered preview mutated")),
        ]
        for index, command in enumerate(commands):
            with self.subTest(command=index):
                with repository.transaction(self.db) as conn:
                    answers.deliveries.begin_delivery(conn, 1, 800 + index * 2, "undo", "undo",
                        result={"tx_ids": [], "cursor": 0, "outcomes": []})
                transport = 801 + index * 2
                self.assertEqual(command(transport)["state"], "recovered_undo")
                with patch.object(answers, "recover_pending_undo", side_effect=AssertionError("late replay recovered again")):
                    replay = command(transport)
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["state"], "recovered_undo")
        self.assertEqual(self.rows("study_actions"), [])
        self.assertEqual(self.rows("transaction_log"), [])
        self.assertEqual(self.rows("quiz_events"), [])

    def test_send_learning_recovery_replay_preserves_a_newer_frame(self):
        with repository.transaction(self.db) as conn:
            answers.deliveries.begin_delivery(conn, 1, 90, "undo", "undo",
                result={"tx_ids": [], "cursor": 0, "outcomes": []})
        update = self.update("send_learn")
        self.ctx.user_data.update(quiz_entry={"id": "a"}, learning_frame={"flow_id": "old"})
        self.assertEqual(handler._send_entry_to_learning(update, self.ctx), handler.EnqueueResult.STALE)
        self.assertTrue(self.ctx.user_data["learning_send_receipt"]["recovered_undo"])
        self.assertNotIn("learning_frame", self.ctx.user_data)
        self.ctx.user_data.update(quiz_entry={"id": "b"}, learning_frame={"flow_id": "new"})
        self.assertEqual(handler._send_entry_to_learning(update, self.ctx), handler.EnqueueResult.STALE)
        self.assertTrue(self.ctx.user_data["learning_send_receipt"]["replayed"])
        self.assertEqual(self.ctx.user_data["learning_frame"], {"flow_id": "new"})
        self.assertEqual(self.ctx.user_data["quiz_entry"], {"id": "b"})
        self.assertEqual(self.rows("learning_items"), [])

    def test_control_recovery_is_receipted_before_late_replay_and_new_flow(self):
        self.add(ready=True)
        self.grade("before-recovery")
        with repository.transaction(self.db) as conn:
            txids = [r[0] for r in conn.execute("SELECT id FROM transaction_log WHERE user_id=1")]
            answers.deliveries.begin_delivery(conn, 1, 100, "undo", "undo",
                result={"tx_ids": txids, "cursor": 0, "outcomes": []})
        result = answers.record_control_transition(self.db, 1, 101, "flow", "reveal",
            lambda conn: self.fail("new command applied during undo recovery"))
        self.assertEqual(result["state"], "recovered_undo")
        self.grade("new-flow")
        before = self.rows("learning_items")
        replay = answers.record_control_transition(self.db, 1, 101, "new-flow", "reveal",
            lambda conn: self.fail("late control replay resolved newer frame"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.rows("learning_items"), before)
        self.assertEqual(len(self.rows("quiz_events")), 1)

    def test_close_flow_preserves_pass_results_and_late_replay_rights_and_sql_rollback(self):
        with repository.transaction(self.db) as conn:
            conn.execute("INSERT INTO conjugation_admissions(user_id,pair,word_id,admitted_at) VALUES (1,'ru_de','a','now')")
            conn.execute("""INSERT INTO conjugation_blocks(user_id,pair,word_id,tense)
                VALUES (1,'ru_de','a','praesens')""")
            conn.execute("""INSERT INTO conjugation_passes(id,user_id,pair,word_id,tense,cycle,status,
                pronoun_order_json,expected_forms_json,baseline_srs_json,started_at)
                VALUES ('pass',1,'ru_de','a','praesens',1,'active','[]','{}','{}','now')""")
            conn.execute("""INSERT INTO conjugation_answers(user_id,pass_id,pronoun_group,ordinal,
                answer_text,is_correct,original_text,original_correct,answered_at,answer_day,
                correction_session_id,action_key)
                VALUES (1,'pass','ich',1,'wrong',0,'wrong',0,'now','2026-10-20','old','conj')""")
        baseline = self.rows("conjugation_passes")
        fresh = answers.close_study_flow(self.db, 1, 201)
        self.assertTrue(fresh["invalidate_frame"])
        self.assertIsNone(self.rows("conjugation_answers")[0]["correction_session_id"])
        self.assertEqual(self.rows("conjugation_passes"), baseline)
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE conjugation_answers SET correction_session_id='new-flow'")
        self.assertTrue(answers.close_study_flow(self.db, 1, 201)["replayed"])
        self.assertEqual(self.rows("conjugation_answers")[0]["correction_session_id"], "new-flow")
        with repository.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER fail_close BEFORE UPDATE ON conjugation_answers
                BEGIN SELECT RAISE(ABORT,'synthetic close failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic close failure"):
            answers.close_study_flow(self.db, 1, 202)
        self.assertFalse(any(r["transport_update_id"] == 202 for r in self.rows("study_deliveries")))
        self.assertEqual(self.rows("conjugation_answers")[0]["correction_session_id"], "new-flow")

    def test_recovery_three_successes_advance_and_wrong_restarts_three(self):
        row = {"step_index": 2, "recovery_remaining": 0, "status": "active", "next_due": stamp(NOW)}
        row = reduce_answer(row, "wrong", [0, 0, 30, 60], NOW)
        self.assertEqual((row["step_index"], row["recovery_remaining"], row["next_due"]), (2, 3, stamp(NOW)))
        row = reduce_answer(row, "correct", [0, 0, 30, 60], NOW)
        self.assertEqual(row["recovery_remaining"], 2)
        row = reduce_answer(row, "idk", [0, 0, 30, 60], NOW)
        self.assertEqual(row["recovery_remaining"], 3)
        for remaining in (2, 1):
            row = reduce_answer(row, "correct", [0, 0, 30, 60], NOW)
            self.assertEqual((row["step_index"], row["recovery_remaining"]), (2, remaining))
        row = reduce_answer(row, "correct", [0, 0, 30, 60], NOW)
        self.assertEqual((row["step_index"], row["recovery_remaining"], row["next_due"]),
                         (3, 0, stamp(NOW + timedelta(seconds=60))))

    def test_wrong_on_last_step_completes_on_third_correct_without_generic_srs(self):
        self.add(ready=True, step=2)
        self.grade(intervals=[0, 0, 30])
        for index in range(3):
            result = self.grade(f"recovery-{index}", "correct", intervals=[0, 0, 30])
            self.assertEqual(result["after"]["completed"], index == 2)
        self.assertEqual(self.rows("progress"), [])
        self.assertEqual(len(self.rows("quiz_events")), 4)

    def test_input_err_recomputes_from_before_recovery_zero_three_two_and_undos(self):
        self.add(ready=True, step=1)
        for before, expected in ((0, 0), (3, 2), (2, 1)):
            with self.subTest(before=before):
                with repository.transaction(self.db) as conn:
                    conn.execute("UPDATE learning_items SET step_index=1,recovery_remaining=?", (before,))
                original = self.grade("wrong-" + str(before), "idk", intervals=[0, 0, 60])
                self.seq += 1
                fix = answers.correct_last_answer(self.db, 1, original["action_key"], "fix-" + str(before),
                    self.seq, 1, NOW, flow_id="learning", live_revision=1,
                    learning_transition=lambda conn, snap, answer, now: store.apply_answer(conn, snap, answer, now, [0, 0, 60]))
                self.assertEqual(fix["after"]["recovery_remaining"], expected)
                self.assertEqual(len(self.rows("quiz_events")), 1)
                self.assertEqual(self.rows("quiz_events")[0]["is_correct"], 1)
                self.seq += 1
                answers.undo_steps(self.db, 1, 1, self.seq)
                self.assertEqual(self.rows("learning_items")[0]["recovery_remaining"], 3)
                self.assertEqual(self.rows("quiz_events")[0]["is_correct"], 0)
                self.seq += 1
                answers.undo_steps(self.db, 1, 1, self.seq)
                self.assertEqual(self.rows("learning_items")[0]["recovery_remaining"], before)
                self.assertEqual(self.rows("quiz_events"), [])

    def test_new_preview_preserves_old_due_and_old_item_updated_at(self):
        self.add(ready=True)
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE learning_items SET next_due='2026-10-20T16:00:00Z',updated_at='old' WHERE word_id='a'")
            before = dict(conn.execute("SELECT * FROM learning_items WHERE word_id='a'").fetchone())
            store.add_items(conn, 1, ["b"], self.batch, NOW)
            store.preview_transition(conn, 1, "b", "preview_next", NOW, 10)
            self.assertEqual(dict(conn.execute("SELECT * FROM learning_items WHERE word_id='a'").fetchone()), before)
            self.assertEqual(store.item(conn, 1, "b")["next_due"], stamp(NOW + timedelta(seconds=10)))

    def test_composite_finalize_skip_undo_repreview_and_marker_remains_fact(self):
        self.add(("a", "b"))
        self.preview("a", "preview_next", "preview-a")
        self.assertEqual(learning_due_entries(self.db, 1), [])
        self.assertIsNone(self.marker())
        result = self.preview("b", "skip", "skip-b")
        self.assertEqual(result["after"]["newly_started_ids"], ["a"])
        self.assertEqual(len(result["before"]["affected_learning_items"]), 2)
        self.seq += 1
        answers.undo_steps(self.db, 1, 1, self.seq)
        self.assertEqual([(r["preview_done"], r["first_due_pending"]) for r in self.rows("learning_items")], [(1, 1), (0, 1)])
        self.assertEqual(self.marker(), "2026-10-20")
        result = self.preview("b", "preview_next", "preview-b-new")
        self.assertEqual(result["after"]["newly_started_ids"], ["a", "b"])
        self.assertEqual(len(self.rows("quiz_events")), 0)

    def test_all_skip_or_delete_without_due_assignment_has_no_start_marker(self):
        self.add(("a", "b"))
        self.preview("a", "skip", "skip-a")
        delete_word(self.db, "b")
        self.assertIsNone(self.marker())

    def test_delete_last_required_preview_finalizes_survivor_but_undo_does_not_resurrect_parent(self):
        self.add(("a", "b"))
        self.preview("a", "preview_next", "preview-a")
        delete_word(self.db, "b")
        self.assertEqual(self.rows("learning_items")[0]["first_due_pending"], 0)
        self.assertIsNotNone(self.marker())
        self.seq += 1
        answers.undo_steps(self.db, 1, 1, self.seq)
        self.assertEqual([r["word_id"] for r in self.rows("learning_items")], ["a"])
        self.assertEqual(self.rows("learning_items")[0]["preview_done"], 0)

    def test_finalize_failure_rolls_back_trigger_other_rows_marker_and_receipts(self):
        self.add(("a", "b"))
        self.preview("a", "preview_next", "preview-a")
        before = self.rows("learning_items")
        with repository.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER fail_marker BEFORE INSERT ON user_settings
                BEGIN SELECT RAISE(ABORT,'marker fault'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "marker fault"):
            self.preview("b", "preview_next", "preview-b")
        self.assertEqual(self.rows("learning_items"), before)
        self.assertEqual(len(self.rows("transaction_log")), 1)
        self.assertEqual(len(self.rows("study_deliveries")), 1)

    def test_completed_item_undo_reopens_existing_batch_and_never_recreates_deleted_batch(self):
        self.add(ready=True, step=2)
        self.grade(kind="correct", intervals=[0, 0, 30])
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE learning_batches SET status='completed',ready_since='stale'")
        self.seq += 1
        answers.undo_steps(self.db, 1, 1, self.seq)
        self.assertEqual(self.rows("learning_batches")[0]["status"], "active")
        self.assertIsNone(self.rows("learning_batches")[0]["ready_since"])
        self.assertEqual(self.rows("learning_items")[0]["status"], "active")
        self.grade("second-finish", "correct", intervals=[0, 0, 30])
        with repository.transaction(self.db) as conn:
            conn.execute("DELETE FROM learning_batches")
        self.seq += 1
        answers.undo_steps(self.db, 1, 1, self.seq)
        self.assertEqual(self.rows("learning_batches"), [])
        self.assertEqual(self.rows("learning_items"), [])

    def test_current_settings_change_next_transition_without_rewriting_existing_due(self):
        self.add(ready=True, step=2)
        before_due = self.rows("learning_items")[0]["next_due"]
        with repository.transaction(self.db) as conn:
            conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES (1,'learning_intervals_sec','900,1800')")
        self.assertEqual(self.rows("learning_items")[0]["next_due"], before_due)
        result = self.grade(kind="correct")
        self.assertEqual(result["after"]["next_due"], stamp(NOW + timedelta(seconds=900)))

    def test_berlin_fact_uses_assignment_day_not_future_due_and_finalizer_is_idempotent(self):
        self.add()
        midnight = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)
        with repository.transaction(self.db) as conn:
            result = store.preview_transition(conn, 1, "a", "preview_next", midnight, 86400)
            self.assertEqual(result["newly_started_ids"], ["a"])
            self.assertEqual(store.finalize_preview_if_ready(conn, 1, self.batch, midnight), [])
            self.assertTrue(conn.in_transaction)
        self.assertEqual(self.marker(), "2026-10-21")

    def test_send_learn_is_one_connection_deduplicates_before_target_and_retains_fifo(self):
        self.ctx.user_data["quiz_entry"] = {"id": "a"}
        update = self.update("send_learn")
        original_connect = sqlite3.connect
        with patch.object(repository.sqlite3, "connect", wraps=original_connect) as connect:
            self.assertEqual(handler._send_entry_to_learning(update, self.ctx), handler.EnqueueResult.QUEUED)
            self.assertEqual(connect.call_count, 1)
        pending = self.rows("learning_requests")
        self.ctx.user_data["quiz_entry"] = {"id": "b"}
        self.assertEqual(handler._send_entry_to_learning(update, self.ctx), handler.EnqueueResult.QUEUED)
        self.assertEqual(self.rows("learning_requests"), pending)
        self.assertEqual(self.ctx.user_data["learning_send_receipt"]["transport_update_id"], update.update_id)
        self.assertTrue(self.ctx.user_data["learning_send_receipt"]["replayed"])
        self.ctx.user_data["quiz_entry"] = {"id": "a"}
        self.assertEqual(handler._send_entry_to_learning(self.update("send_learn"), self.ctx), handler.EnqueueResult.ALREADY_PENDING)
        self.assertEqual(self.rows("learning_requests"), pending)
        for table in ("learning_items", "progress", "quiz_events", "transaction_log"):
            self.assertEqual(self.rows(table), [])
        self.assertIsNone(self.marker())

    def test_handler_grade_and_correction_replay_keep_b_without_extra_selection(self):
        self.add(("a", "b"), ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        wrong = self.update("wrong")
        asyncio.run(handler.learning_quiz(wrong, self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["current"]["entry"]["id"], "b")
        recent = list(self.ctx.user_data["learning_recent_ids"])
        correction = self.update("input_err")
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("picked")):
            asyncio.run(handler.learning_quiz(correction, self.ctx))
            asyncio.run(handler.learning_quiz(correction, self.ctx))
            asyncio.run(handler.learning_quiz(wrong, self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["current"]["entry"]["id"], "b")
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertEqual(len(self.rows("quiz_events")), 1)
        self.assertEqual(self.rows("quiz_events")[0]["is_correct"], 1)
        self.assertEqual(len(self.rows("transaction_log")), 2)

    def test_singleton_correction_rechecks_due_and_waiting_is_not_completed(self):
        self.add(ready=True, step=2)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        asyncio.run(handler.learning_quiz(self.update("wrong"), self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["current"]["entry"]["id"], "a")
        asyncio.run(handler.learning_quiz(self.update("input_err"), self.ctx))
        frame = self.ctx.user_data[handler.FRAME]
        self.assertIsNone(frame["current"])
        self.assertEqual(frame["phase"], "waiting")
        self.assertEqual(self.rows("learning_items")[0]["status"], "active")

    def test_send_failure_replays_existing_frame_without_grading_next_card(self):
        self.add(("a", "b"), ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        update = self.update("wrong")
        update.message.fail = True
        with self.assertRaisesRegex(RuntimeError, "synthetic send"):
            asyncio.run(handler.learning_quiz(update, self.ctx))
        recent = list(self.ctx.user_data["learning_recent_ids"])
        self.assertTrue(self.ctx.user_data[handler.FRAME]["pending_render"])
        update.message.fail = False
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("picked")):
            asyncio.run(handler.learning_quiz(update, self.ctx))
        self.assertFalse(self.ctx.user_data[handler.FRAME]["pending_render"])
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertEqual(len(self.rows("quiz_events")), 1)


    def test_back_delivery_retry_preserves_both_destinations_and_committed_database(self):
        self.add(ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        for entrypoint, destination in ((handler.learning_quiz, handler.LEARNING_MENU),
                                       (handler.learning_menu_choice, handler.TESTS_MENU)):
            with self.subTest(destination=destination):
                back = self.update("back")
                back.message.fail = True
                with self.assertRaisesRegex(RuntimeError, "synthetic send"):
                    asyncio.run(entrypoint(back, self.ctx))
                baseline = {table: self.rows(table) for table in (
                    "learning_items", "quiz_events", "transaction_log", "study_deliveries")}
                self.assertNotIn(handler.FRAME, self.ctx.user_data)
                with self.assertRaisesRegex(RuntimeError, "synthetic send"):
                    asyncio.run(entrypoint(back, self.ctx))
                back.message.fail = False
                self.assertEqual(asyncio.run(entrypoint(back, self.ctx)), destination)
                self.assertEqual(len(back.message.sent), 1)
                self.assertIsNone(asyncio.run(entrypoint(back, self.ctx)))
                self.assertEqual(len(back.message.sent), 1)
                self.assertNotIn(handler._PENDING_NAVIGATION, self.ctx.user_data)
                self.assertEqual({table: self.rows(table) for table in baseline}, baseline)

    def test_failed_back_late_replay_cannot_present_over_a_new_flow_or_navigation(self):
        from copy import deepcopy
        self.add(ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        back = self.update("back")
        back.message.fail = True
        with self.assertRaisesRegex(RuntimeError, "synthetic send"):
            asyncio.run(handler.learning_quiz(back, self.ctx))
        pending = deepcopy(self.ctx.user_data[handler._PENDING_NAVIGATION])
        back.message.fail = False
        self.ctx.user_data[handler.FRAME] = {"flow_id": "new", "pending_render": False}
        with patch.object(handler, "_present_learning_navigation", side_effect=AssertionError("late navigation rendered")):
            self.assertIsNone(asyncio.run(handler.learning_quiz(back, self.ctx)))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["flow_id"], "new")
        handler.invalidate_study_frames(self.ctx)
        self.assertNotIn(handler._PENDING_NAVIGATION, self.ctx.user_data)
        self.assertIsNone(asyncio.run(handler.learning_quiz(back, self.ctx)))
        self.assertEqual(back.message.sent, [])
        # A fresh Learning entry also closes the failed Back presentation right.
        self.ctx.user_data[handler._PENDING_NAVIGATION] = pending
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        self.assertNotIn(handler._PENDING_NAVIGATION, self.ctx.user_data)
        live = deepcopy(self.ctx.user_data[handler.FRAME])
        self.assertIsNone(asyncio.run(handler.learning_quiz(back, self.ctx)))
        self.assertEqual(self.ctx.user_data[handler.FRAME], live)

    def test_new_delivery_drains_unseen_committed_question_and_late_replay_keeps_new_flow(self):
        from copy import deepcopy
        self.add(("a", "b"), ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        failed = self.update("wrong")
        with patch.object(handler, "resolve_bot_word_media", side_effect=RuntimeError("before reply")):
            with self.assertRaisesRegex(RuntimeError, "before reply"):
                asyncio.run(handler.learning_quiz(failed, self.ctx))
        self.assertEqual(failed.message.sent, [])
        frame = deepcopy(self.ctx.user_data[handler.FRAME])
        recent = list(self.ctx.user_data["learning_recent_ids"])
        before_events, before_tx = self.rows("quiz_events"), self.rows("transaction_log")
        drain = self.update("wrong")
        drain.message.fail = True
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("drain picked")), \
             patch.object(handler, "_note_learning_shown", side_effect=AssertionError("drain noted")):
            with self.assertRaisesRegex(RuntimeError, "synthetic send"):
                asyncio.run(handler.learning_quiz(drain, self.ctx))
            drain.message.fail = False
            asyncio.run(handler.learning_quiz(drain, self.ctx))
        live = self.ctx.user_data[handler.FRAME]
        self.assertEqual(live["current"], frame["current"])
        self.assertEqual(live["revision"], frame["revision"])
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertFalse(live["pending_render"])
        self.assertEqual(self.rows("quiz_events"), before_events)
        self.assertEqual(self.rows("transaction_log"), before_tx)
        receipt = next(row for row in self.rows("study_deliveries") if row["transport_update_id"] == drain.update_id)
        self.assertEqual(receipt["action_kind"], "learning_drain_view")
        self.assertIsNone(receipt["action_key"])
        self.ctx.user_data[handler.FRAME] = {"flow_id": "new", "pending_render": True, "accepted_delivery": 987}
        with patch.object(handler, "_render_learning_frame", side_effect=AssertionError("late replay rendered")):
            self.assertIsNone(asyncio.run(handler.learning_quiz(drain, self.ctx)))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["flow_id"], "new")
        self.assertEqual(self.rows("quiz_events"), before_events)

    def test_menu_delivery_drains_failed_initial_preview_without_consuming_next(self):
        from copy import deepcopy
        self.add()
        with patch.object(handler, "resolve_bot_word_media", side_effect=RuntimeError("initial media")):
            with self.assertRaisesRegex(RuntimeError, "initial media"):
                asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        frame = deepcopy(self.ctx.user_data[handler.FRAME])
        asyncio.run(handler.learning_menu_choice(self.update("learning_next"), self.ctx))
        live = self.ctx.user_data[handler.FRAME]
        self.assertEqual(live["current"], frame["current"])
        self.assertEqual(live["revision"], frame["revision"])
        self.assertEqual(live["phase"], "preview")
        self.assertEqual(self.rows("learning_items")[0]["preview_done"], 0)
        self.assertEqual(self.rows("quiz_events"), [])

    def test_fresh_render_updates_grading_dictionary_without_changing_question_identity(self):
        from copy import deepcopy
        self.add(ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        asyncio.run(handler.learning_quiz(self.update("wrong"), self.ctx))
        frame = deepcopy(self.ctx.user_data[handler.FRAME])
        recent = list(self.ctx.user_data["learning_recent_ids"])
        baseline = self.rows("transaction_log")
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET german='omega' WHERE id='a'")
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("render picked")):
            asyncio.run(handler._render_learning_frame(self.update("return"), self.ctx))
        live = self.ctx.user_data[handler.FRAME]
        self.assertEqual(live["current"]["entry"]["german"], "omega")
        self.assertEqual(live["current"]["action_key"], frame["current"]["action_key"])
        self.assertEqual(live["revision"], frame["revision"])
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertEqual(self.rows("transaction_log"), baseline)
        with patch.object(handler, "_check_ru_de_strict", side_effect=lambda entry,text: (entry["german"] == text, None)):
            asyncio.run(handler.learning_quiz(self.update("omega"), self.ctx))
        self.assertEqual(self.rows("quiz_events")[-1]["is_correct"], 1)
        self.assertEqual(self.rows("learning_items")[0]["recovery_remaining"], 2)

    def test_sql_picker_failure_rolls_back_grade_without_changing_frame_or_recent(self):
        from copy import deepcopy
        self.add(("a", "b"), ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        before = deepcopy(self.ctx.user_data)
        with patch.object(handler, "_pick_learning_due_entry", side_effect=sqlite3.OperationalError("picker fault")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "picker fault"):
                asyncio.run(handler.learning_quiz(self.update("wrong"), self.ctx))
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(self.rows("quiz_events"), [])
        self.assertEqual(self.rows("transaction_log"), [])
        self.assertEqual(self.rows("learning_items")[0]["recovery_remaining"], 0)

    def test_deleted_composite_trigger_undo_then_start_recovers_surviving_pending_tail(self):
        self.add(("a", "b"))
        self.preview("a", "preview_next", "preview-a")
        self.preview("b", "preview_next", "preview-b")
        delete_word(self.db, "b")
        self.seq += 1
        answers.undo_steps(self.db, 1, 1, self.seq)
        self.assertEqual([(r["word_id"],r["preview_done"],r["first_due_pending"])
                          for r in self.rows("learning_items")], [("a",1,1)])
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["phase"], "question")
        self.assertEqual(self.rows("learning_items")[0]["first_due_pending"], 0)
        self.assertEqual(len(self.rows("learning_batches")), 1)

    def test_preview_clarify_and_synonyms_return_render_existing_without_picker(self):
        from bot.common.quiz_resume import capture_return_point, render_existing_frame
        from bot.handlers import synonyms
        from bot.app_config import LEARNING_QUIZ
        self.add()
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        point = capture_return_point(self.ctx, LEARNING_QUIZ)
        before = self.ctx.user_data[handler.FRAME]["revision"]
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("picked")):
            asyncio.run(render_existing_frame(self.update("return"), self.ctx, "Saved"))
            capture_return_point(self.ctx, LEARNING_QUIZ)
            self.ctx.user_data["syn_return_state"] = LEARNING_QUIZ
            asyncio.run(synonyms.synonyms_menu(self.update("back"), self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["revision"], before)
        self.assertEqual(self.ctx.user_data[handler.FRAME]["phase"], "preview")
        self.assertEqual(self.rows("learning_items")[0]["preview_done"], 0)
        self.assertEqual(self.rows("quiz_events"), [])
        self.assertEqual(self.rows("transaction_log"), [])
        self.assertEqual(point.current_question_id, "a")

    def test_completed_clarify_return_has_no_question_and_keeps_visible_answer(self):
        from bot.common.quiz_resume import capture_return_point, render_existing_frame
        from bot.app_config import LEARNING_QUIZ
        self.add(ready=True, step=3)
        with repository.transaction(self.db) as conn:
            conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES (1,'learning_intervals_sec','60')")
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        asyncio.run(handler.learning_quiz(self.update("good"), self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["phase"], "completed")
        point = capture_return_point(self.ctx, LEARNING_QUIZ)
        self.assertEqual(point.visible_answer_id, "a")
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("picked")):
            asyncio.run(render_existing_frame(self.update("return"), self.ctx, "Saved"))
        self.assertIsNone(self.ctx.user_data[handler.FRAME]["current"])
        self.assertEqual(len(self.rows("quiz_events")), 1)

    def test_clarify_send_learn_active_preserves_current_phase_without_picker(self):
        from bot.common.quiz_resume import capture_return_point, render_existing_frame
        from bot.app_config import LEARNING_QUIZ
        self.add(ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        capture_return_point(self.ctx, LEARNING_QUIZ)
        self.ctx.user_data["quiz_edit_entry"] = {"id": "a"}
        from copy import deepcopy
        frame = deepcopy(self.ctx.user_data[handler.FRAME])
        items = self.rows("learning_items")
        self.assertEqual(handler._send_entry_to_learning(self.update("send_learn"), self.ctx, "quiz_edit_entry"), handler.EnqueueResult.ALREADY_ACTIVE)
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("picked")):
            asyncio.run(render_existing_frame(self.update("return"), self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME], frame)
        self.assertEqual(self.rows("learning_items"), items)
        self.assertEqual(self.rows("learning_requests"), [])
        self.assertEqual(self.rows("quiz_events"), [])


if __name__ == "__main__":
    unittest.main()
