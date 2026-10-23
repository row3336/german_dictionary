"""Independent Learning acceptance: real SQLite, original handlers and fault probes."""

import asyncio
from contextlib import asynccontextmanager, closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from _telegram_stub import install
install()

from telegram.error import TimedOut
from bot import learning_store as store
from bot.app_config import ADD_EDIT, LEARNING_QUIZ
from bot.common import quiz_resume as resume
from bot.handlers import clarify_fields as clarify, learning as L, synonyms
from bot.i18n import t
from bot.learning_logic import stamp
from bot.storage import delete_word, init_db, learning_due_entries, learning_preview_entry, learning_status_snapshot
from bot.study import answers, deliveries, repository as repo

NOW = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)


class LearningAcceptance(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(os.environ["TMPDIR"]) / f"independent-learning-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": "1"})
        env.start(); self.addCleanup(env.stop)
        init_db(self.db)
        with repo.transaction(self.db) as conn:
            conn.executemany("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES(?,'approved','en','ru')", [(1,), (2,)])
            conn.executemany("INSERT INTO words(id,type,german,russian,ukrainian,english) VALUES(?,'adverb',?,?,?,?)",
                             [(name, word, f"ru-{word}", f"uk-{word}", f"en-{word}")
                              for name, word in (("a", "alpha"), ("b", "beta"), ("c", "gamma"), ("d", "delta"))])
            self.batch = store.get_or_create_batch(conn, 1, 20, NOW)
        self.sequence = 5000
        self.ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(set_message_reaction=AsyncMock()))

    def update(self, text, uid=1):
        self.sequence += 1
        msg = SimpleNamespace(text=text, message_id=self.sequence,
                              reply_text=AsyncMock(return_value=SimpleNamespace(message_id=self.sequence + 10000)))
        return SimpleNamespace(update_id=self.sequence, message=msg, effective_message=msg,
                               effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=uid))

    def read(self, table):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def row(self, wid="a"):
        return next((row for row in self.read("learning_items") if row["user_id"] == 1 and row["word_id"] == wid), None)

    def state(self):
        return {table: self.read(table) for table in ("learning_items", "learning_batches", "progress", "quiz_events", "transaction_log", "study_actions", "study_deliveries", "user_settings")}

    def add(self, ids=("a",), ready=False, step=0, recovery=0):
        with repo.transaction(self.db) as conn:
            store.add_items(conn, 1, ids, self.batch, NOW)
            for index, wid in enumerate(ids):
                conn.execute("UPDATE learning_items SET created_at=?,preview_done=?,first_due_pending=?,step_index=?,recovery_remaining=?,next_due=? WHERE user_id=1 AND word_id=?",
                    (f"2000-01-01T00:00:{index:02}Z", int(ready), int(not ready), step, recovery,
                     "2000-01-01T00:00:00Z", wid))

    def grade(self, kind, intervals=None, wid="a"):
        self.sequence += 1
        return answers.apply_original_answer(self.db, 1, wid, "learning", kind, uuid4().hex,
            self.sequence, 17, NOW, flow_id="qa-model", live_revision=17,
            learning_transition=lambda conn, before, answer, now: store.apply_answer(conn, before, answer, now, intervals))

    def preview(self, wid, action="preview_next", delay=30):
        self.sequence += 1
        return answers.apply_preview_transition(self.db, 1, wid, action, uuid4().hex, self.sequence, 1,
            NOW, flow_id="qa-preview", live_revision=1,
            affected_learning_ids=lambda conn: store.preview_affected_ids(conn, 1, wid),
            transition=lambda conn, before, now: store.preview_transition(conn, 1, wid, action, now, delay))

    def undo(self):
        self.sequence += 1
        return answers.undo_steps(self.db, 1, 1, self.sequence)

    def start(self):
        return asyncio.run(L.learning_menu_choice(self.update(t("en", "learning_start")), self.ctx))

    def answer(self, text):
        return asyncio.run(L.learning_quiz(self.update(text), self.ctx))

    def marker(self):
        return next((row["value"] for row in self.read("user_settings") if row["user_id"] == 1 and row["key"] == "learning_new_start_date"), None)

    def test_recovery_decision_matrix_in_database_and_undo(self):
        self.add(ready=True)
        schedule = [0, 0, 0, 30, 120, 600, 3600]
        for step in (0, 3, 6):
            for recovery in range(4):
                expected_correct = {0: (step + 1, 0), 1: (step + 1, 0), 2: (step, 1), 3: (step, 2)}[recovery]
                for kind in ("wrong", "idk", "correct"):
                    with self.subTest(step=step, recovery=recovery, kind=kind):
                        with repo.transaction(self.db) as conn:
                            conn.execute("UPDATE learning_items SET step_index=?,recovery_remaining=?,status='active' WHERE user_id=1", (step, recovery))
                        baseline = self.row()
                        receipt = self.grade(kind, schedule)
                        after = self.row()
                        expected_step, expected_recovery = expected_correct if kind == "correct" else (step, 3)
                        self.assertEqual((after["step_index"], after["recovery_remaining"]), (expected_step, expected_recovery))
                        self.assertEqual(after["status"], "completed" if expected_step == 7 else "active")
                        if kind != "correct" or recovery > 1:
                            self.assertEqual(after["next_due"], stamp(NOW))
                        elif expected_step < 7:
                            self.assertEqual(after["next_due"], stamp(NOW + timedelta(seconds=schedule[expected_step])))
                        self.assertEqual(len(self.read("quiz_events")), 1)
                        self.assertEqual(self.read("progress"), [])
                        self.assertEqual(receipt["before"]["learning_item"], baseline)
                        self.undo()
                        self.assertEqual(self.row(), baseline)
                        self.assertEqual(self.read("quiz_events"), [])

    def test_error_correct_error_resets_three_and_third_success_finishes_last_step(self):
        self.add(ready=True, step=3)
        self.grade("wrong", [0, 0, 0, 30])
        self.grade("correct", [0, 0, 0, 30])
        self.assertEqual(self.row()["recovery_remaining"], 2)
        self.grade("wrong", [0, 0, 0, 30])
        for remaining in (2, 1, 0):
            self.grade("correct", [0, 0, 0, 30])
            self.assertEqual(self.row()["recovery_remaining"], remaining)
            self.assertEqual(self.row()["status"], "completed" if remaining == 0 else "active")
        self.assertEqual(self.row()["step_index"], 4)
        self.assertEqual(len(self.read("quiz_events")), 6)

    def test_input_error_uses_full_before_and_two_separate_undos(self):
        self.add(ready=True, step=2)
        for recovery, expected_remaining in ((0, 0), (3, 2), (2, 1)):
            with self.subTest(before_recovery=recovery):
                with repo.transaction(self.db) as conn:
                    conn.execute("UPDATE learning_items SET recovery_remaining=?", (recovery,))
                baseline = self.row()
                wrong = self.grade("idk", [0, 0, 0, 90])
                wrong_row = self.row()
                self.sequence += 1
                fixed = answers.correct_last_answer(self.db, 1, wrong["action_key"], uuid4().hex, self.sequence,
                    17, NOW, flow_id="qa-model", live_revision=17,
                    learning_transition=lambda conn, before, kind, now: store.apply_answer(conn, before, kind, now, [0, 0, 0, 90]))
                self.assertEqual(self.row()["recovery_remaining"], expected_remaining)
                self.assertEqual(self.row()["step_index"], 3 if recovery == 0 else 2)
                self.assertEqual([row["id"] for row in self.read("quiz_events")], [wrong["event_id"]])
                self.assertEqual(self.read("quiz_events")[0]["is_correct"], 1)
                self.assertEqual(fixed["parent_action_key"], wrong["action_key"])
                self.undo()
                self.assertEqual(self.row(), wrong_row)
                self.assertEqual(self.read("quiz_events")[0]["is_correct"], 0)
                self.undo()
                self.assertEqual(self.row(), baseline)
                self.assertEqual(self.read("quiz_events"), [])

    def test_new_settings_apply_on_next_transition_without_rewriting_old_due(self):
        self.add(("a", "b"), ready=True, step=2)
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE learning_items SET next_due='2099-04-05T16:00:00Z',updated_at='old' WHERE word_id='b'")
            conn.execute("INSERT INTO user_settings VALUES(1,'learning_intervals_sec','900,1800')")
        old_b = self.row("b")
        self.grade("correct")
        self.assertEqual(self.row()["next_due"], stamp(NOW + timedelta(seconds=900)))
        self.assertEqual(self.row("b"), old_b)
        with repo.transaction(self.db) as conn:
            store.add_items(conn, 1, ["c"], self.batch, NOW)
        self.preview("c")
        self.assertEqual(self.row("b"), old_b)

    def test_composite_skip_next_undo_deleted_trigger_getters_and_start_repair(self):
        self.add(("a", "b", "c"))
        self.preview("a")
        self.preview("b", "skip")
        last = self.preview("c")
        self.assertEqual(set(last["after"]["newly_started_ids"]), {"a", "c"})
        self.assertEqual({row["word_id"] for row in last["before"]["affected_learning_items"]}, {"a", "c"})
        self.assertEqual(self.marker(), "2026-10-21")
        delete_word(self.db, "c")
        self.undo()
        self.assertIsNone(self.row("c"))
        self.assertEqual((self.row()["preview_done"], self.row()["first_due_pending"]), (1, 1))
        self.assertEqual(self.marker(), "2026-10-21")  # a historical start remains a fact
        before = self.state()
        self.assertIsNone(learning_preview_entry(self.db, 1))
        self.assertEqual(learning_due_entries(self.db, 1), [])
        learning_status_snapshot(self.db, 1)
        self.assertEqual(self.state(), before)
        self.start()
        self.assertEqual(self.row()["first_due_pending"], 0)
        self.assertEqual(self.ctx.user_data[L.FRAME]["phase"], "question")
        self.assertEqual(len(self.read("learning_batches")), 1)
        self.assertEqual(self.read("quiz_events"), [])

    def test_deleted_batch_is_not_recreated_but_existing_batch_is_reopened_by_undo(self):
        self.add(ready=True, step=3)
        self.grade("correct", [0, 0, 0, 30])
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE learning_batches SET status='completed',ready_since='old'")
        self.undo()
        self.assertEqual(self.read("learning_batches")[0]["status"], "active")
        self.assertIsNone(self.read("learning_batches")[0]["ready_since"])
        self.grade("correct", [0, 0, 0, 30])
        with repo.transaction(self.db) as conn:
            conn.execute("DELETE FROM learning_batches")
        self.undo()
        self.assertEqual(self.read("learning_batches"), [])
        self.assertEqual(self.read("learning_items"), [])

    def test_all_skipped_or_deleted_before_assignment_has_no_start_fact(self):
        self.add(("a", "b"))
        self.preview("a", "skip")
        delete_word(self.db, "b")
        self.assertIsNone(self.marker())
        self.assertEqual(self.read("quiz_events"), [])

    def test_actual_sql_fault_after_next_selection_rolls_back_grade_and_frame(self):
        self.add(("a", "b"), ready=True)
        self.start()
        with repo.transaction(self.db) as conn:
            conn.execute("CREATE TRIGGER qa_abort_receipt BEFORE INSERT ON study_actions BEGIN SELECT RAISE(ABORT,'qa late SQL'); END")
        before_db, before_ui = self.state(), deepcopy(self.ctx.user_data)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "qa late SQL"):
            self.answer("definitely-wrong")
        self.assertEqual(self.state(), before_db)
        self.assertEqual(self.ctx.user_data, before_ui)

    def test_marker_fault_rolls_back_every_composite_affected_row(self):
        self.add(("a", "b", "c"))
        self.preview("a")
        self.preview("b")
        before = self.state()
        with repo.transaction(self.db) as conn:
            conn.execute("CREATE TRIGGER qa_abort_marker BEFORE INSERT ON user_settings WHEN NEW.key='learning_new_start_date' BEGIN SELECT RAISE(ABORT,'qa marker SQL'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "qa marker SQL"):
            self.preview("c")
        self.assertEqual(self.state(), before)

    def test_concurrent_same_transport_and_correction_keep_b_slot_exactly(self):
        self.add(("a", "b"), ready=True)
        self.start()
        wrong = self.update("definitely-wrong")
        async def race():
            await asyncio.gather(L.learning_quiz(wrong, self.ctx), L.learning_quiz(wrong, self.ctx))
        asyncio.run(race())
        slot = deepcopy(self.ctx.user_data[L.FRAME]["current"])
        recent = list(self.ctx.user_data["learning_recent_ids"])
        self.assertEqual(slot["entry"]["id"], "b")
        correction = self.update(t("en", "input_err"))
        with patch.object(L, "_pick_learning_due_entry", side_effect=AssertionError("unexpected picker")):
            asyncio.run(L.learning_quiz(correction, self.ctx))
            asyncio.run(L.learning_quiz(correction, self.ctx))
            asyncio.run(L.learning_quiz(wrong, self.ctx))
        self.assertEqual(self.ctx.user_data[L.FRAME]["current"], slot)
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertEqual(len(self.read("quiz_events")), 1)
        self.assertEqual(self.read("quiz_events")[0]["is_correct"], 1)
        self.assertEqual(len(self.read("transaction_log")), 2)

    def test_media_and_raised_send_failure_replay_only_existing_revision(self):
        self.add(("a", "b"), ready=True)
        self.start()
        wrong = self.update("definitely-wrong")
        with patch.object(L, "resolve_bot_word_media", side_effect=RuntimeError("qa media")):
            with self.assertRaisesRegex(RuntimeError, "qa media"):
                asyncio.run(L.learning_quiz(wrong, self.ctx))
        frame = deepcopy(self.ctx.user_data[L.FRAME])
        recent = list(self.ctx.user_data["learning_recent_ids"])
        wrong.message.reply_text.side_effect = RuntimeError("qa delivery")
        with patch.object(L, "_pick_learning_due_entry", side_effect=AssertionError("replay picked")):
            with self.assertRaisesRegex(RuntimeError, "qa delivery"):
                asyncio.run(L.learning_quiz(wrong, self.ctx))
            wrong.message.reply_text.side_effect = None
            asyncio.run(L.learning_quiz(wrong, self.ctx))
        self.assertEqual(self.ctx.user_data[L.FRAME]["revision"], frame["revision"])
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertEqual(len(self.read("quiz_events")), 1)
        self.assertFalse(self.ctx.user_data[L.FRAME]["pending_render"])

    def test_new_transport_after_definite_media_failure_only_presents_unseen_pending_b(self):
        self.add(("a", "b"), ready=True)
        self.start()
        failed = self.update("definitely-wrong")
        with patch.object(L, "resolve_bot_word_media", side_effect=RuntimeError("qa before first reply")):
            with self.assertRaisesRegex(RuntimeError, "qa before first reply"):
                asyncio.run(L.learning_quiz(failed, self.ctx))
        failed.message.reply_text.assert_not_called()
        frame = deepcopy(self.ctx.user_data[L.FRAME])
        self.assertEqual(frame["current"]["entry"]["id"], "b")
        self.assertTrue(frame["pending_render"])
        resend = self.update("definitely-wrong")  # a genuinely new transport id
        with patch.object(L, "_pick_learning_due_entry", wraps=L._pick_learning_due_entry) as picker:
            asyncio.run(L.learning_quiz(resend, self.ctx))
        self.assertEqual(len(self.read("quiz_events")), 1, "unseen B was graded by a resend of A")
        self.assertEqual(self.ctx.user_data[L.FRAME]["current"], frame["current"])
        self.assertEqual(self.ctx.user_data[L.FRAME]["revision"], frame["revision"])
        self.assertFalse(self.ctx.user_data[L.FRAME]["pending_render"])
        picker.assert_not_called()
        resend.message.reply_text.assert_awaited_once()

    def test_unknown_timeout_does_not_retry_or_duplicate_grade_and_preserves_current_frame(self):
        self.add(("a", "b"), ready=True)
        self.start()
        wrong = self.update("definitely-wrong")
        wrong.message.reply_text.side_effect = TimedOut("qa network timeout")
        # The established owner policy deliberately avoids automatic resend after
        # an ambiguous read timeout. A receipt must still never grade B again.
        with self.assertLogs("bot.common.navigation", level="WARNING"):
            asyncio.run(L.learning_quiz(wrong, self.ctx))
        self.assertEqual(wrong.message.reply_text.await_count, 1)
        frame, database = deepcopy(self.ctx.user_data[L.FRAME]), self.state()
        self.assertEqual(frame["current"]["entry"]["id"], "b")
        wrong.message.reply_text.side_effect = None
        with patch.object(L, "_pick_learning_due_entry", side_effect=AssertionError("replay picked")):
            asyncio.run(L.learning_quiz(wrong, self.ctx))
        self.assertEqual(wrong.message.reply_text.await_count, 1)
        self.assertEqual(self.ctx.user_data[L.FRAME], frame)
        self.assertEqual(self.state(), database)
        self.assertEqual(len(self.read("quiz_events")), 1)

    def test_singleton_correction_becomes_waiting_or_completed_without_resurrection(self):
        self.add(ready=True, step=2)
        self.start()
        self.answer("definitely-wrong")
        self.assertEqual(self.ctx.user_data[L.FRAME]["current"]["entry"]["id"], "a")
        self.answer(t("en", "input_err"))
        self.assertEqual(self.ctx.user_data[L.FRAME]["phase"], "waiting")
        self.assertIsNone(self.ctx.user_data[L.FRAME]["current"])
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'learning_intervals_sec','30')")
            conn.execute("UPDATE learning_items SET step_index=3,next_due='2000-01-01T00:00:00Z',recovery_remaining=0")
        self.start()
        self.answer("definitely-wrong")
        self.answer(t("en", "input_err"))
        self.assertEqual(self.ctx.user_data[L.FRAME]["phase"], "completed")
        self.assertIsNone(self.ctx.user_data[L.FRAME]["current"])

    def test_clarify_synonyms_return_and_delete_use_one_outer_lock(self):
        self.add(("a", "b"))
        self.start()
        original = L.study_lock
        depth = 0
        entered = []
        @asynccontextmanager
        async def monitored(uid):
            nonlocal depth
            self.assertEqual(depth, 0, "nested study lock acquisition")
            async with original(uid):
                depth += 1; entered.append(uid)
                try:
                    yield
                finally:
                    depth -= 1
        async def navigate():
            self.assertEqual(await L.learning_quiz(self.update(t("en", "clarify_fields")), self.ctx), ADD_EDIT)
            await clarify.clarify_fields_input(self.update(t("en", "back")), self.ctx)
            await L.learning_quiz(self.update(t("en", "clarify_fields")), self.ctx)
            self.ctx.user_data["syn_return_state"] = LEARNING_QUIZ
            await synonyms.synonyms_menu(self.update(t("en", "back")), self.ctx)
        before = deepcopy(self.ctx.user_data[L.FRAME])
        with patch.object(L, "study_lock", monitored), patch.object(clarify, "study_lock", monitored), \
             patch.object(synonyms, "study_lock", monitored), patch.object(L, "_pick_learning_due_entry", side_effect=AssertionError("menu selected")):
            asyncio.run(asyncio.wait_for(navigate(), timeout=3))
        self.assertEqual(len(entered), 4)
        self.assertEqual(self.ctx.user_data[L.FRAME]["revision"], before["revision"])
        self.assertEqual(self.row()["preview_done"], 0)
        self.assertEqual(self.read("quiz_events"), [])
        asyncio.run(L.learning_quiz(self.update(t("en", "clarify_fields")), self.ctx))
        asyncio.run(clarify.clarify_fields_input(self.update("delete"), self.ctx))
        asyncio.run(clarify.clarify_fields_input(self.update("yes"), self.ctx))
        self.assertIsNone(self.row())
        self.assertEqual(self.ctx.user_data[L.FRAME]["phase"], "preview")
        self.assertEqual(self.ctx.user_data[L.FRAME]["current"]["entry"]["id"], "b")

    def test_saved_dictionary_change_is_used_for_current_singleton_grading(self):
        self.add(ready=True)
        self.start()
        self.answer("definitely-wrong")
        self.answer(t("en", "clarify_fields"))
        self.ctx.user_data.update(edit_stage="enter_value", edit_field="de")
        asyncio.run(clarify.clarify_fields_input(self.update("omega"), self.ctx))
        self.assertEqual(self.ctx.user_data["quiz_entry"]["german"], "omega")
        self.assertEqual(next(row for row in self.read("words") if row["id"] == "a")["german"], "omega")
        self.answer("omega")
        self.assertEqual(self.read("quiz_events")[-1]["is_correct"], 1)
        self.assertEqual(self.row()["recovery_remaining"], 2)

    def test_send_learn_single_connection_replay_before_target_and_late_recovery(self):
        self.ctx.user_data["quiz_entry"] = {"id": "a"}
        upd = self.update(t("en", "send_learn"))
        real_connect = sqlite3.connect
        with patch.object(sqlite3, "connect", wraps=real_connect) as connections:
            self.assertEqual(L._send_entry_to_learning(upd, self.ctx), L.EnqueueResult.QUEUED)
        self.assertEqual(connections.call_count, 1)
        class Poison:
            def get(self, key, default=None):
                raise AssertionError("replay resolved the current target")
        self.ctx.user_data["quiz_entry"] = Poison()
        self.assertEqual(L._send_entry_to_learning(upd, self.ctx), L.EnqueueResult.QUEUED)
        self.assertEqual(self.read("learning_items"), [])
        self.assertEqual([r["word_id"] for r in self.read("learning_requests")], ["a"])
        self.assertTrue(self.ctx.user_data["learning_send_receipt"]["replayed"])
        recovery = self.update(t("en", "send_learn"))
        with repo.transaction(self.db) as conn:
            deliveries.begin_delivery(conn, 1, 99999, "qa-undo", "undo", result={"tx_ids": [], "cursor": 0, "outcomes": []})
        self.ctx.user_data.update(quiz_entry={"id": "b"}, learning_frame={"flow_id": "old"})
        self.assertEqual(L._send_entry_to_learning(recovery, self.ctx), L.EnqueueResult.STALE)
        self.assertNotIn(L.FRAME, self.ctx.user_data)
        self.ctx.user_data.update(quiz_entry={"id": "b"}, learning_frame={"flow_id": "new"})
        self.assertEqual(L._send_entry_to_learning(recovery, self.ctx), L.EnqueueResult.STALE)
        self.assertEqual(self.ctx.user_data[L.FRAME], {"flow_id": "new"})
        self.assertIsNone(self.row("b"))


    def test_definite_fault_matrix_drains_before_new_input_and_late_replay(self):
        for initial_preview in (False, True):
            for stage in ("fresh_sql", "media", "first_reply"):
                with self.subTest(initial_preview=initial_preview, stage=stage):
                    probe = LearningAcceptance(methodName="runTest")
                    probe.setUp()
                    self.addCleanup(probe.doCleanups)
                    probe.add(("a", "b"), ready=not initial_preview)
                    if not initial_preview:
                        probe.start()
                    failed = probe.update(t("en", "learning_start") if initial_preview else "wrong")
                    target = L.learning_menu_choice if initial_preview else L.learning_quiz
                    if stage == "first_reply":
                        failed.message.reply_text.side_effect = RuntimeError("qa definite first reply")
                        with self.assertRaisesRegex(RuntimeError, "qa definite"):
                            asyncio.run(target(failed, probe.ctx))
                    else:
                        owner, attr = (L, "resolve_bot_word_media") if stage == "media" else (__import__("bot.storage", fromlist=["get_word_by_id"]), "get_word_by_id")
                        with patch.object(owner, attr, side_effect=sqlite3.OperationalError("qa definite fresh SQL") if stage == "fresh_sql" else RuntimeError("qa definite media")):
                            with self.assertRaisesRegex((RuntimeError, sqlite3.OperationalError), "qa definite"):
                                asyncio.run(target(failed, probe.ctx))
                        failed.message.reply_text.assert_not_called()
                    frame = deepcopy(probe.ctx.user_data[L.FRAME])
                    database = probe.state()
                    recent = deepcopy(probe.ctx.user_data.get("learning_recent_ids"))
                    drain = probe.update(t("en", "learning_next") if initial_preview else "wrong")
                    drain.message.reply_text.side_effect = RuntimeError("qa drain reply")
                    with patch.object(L, "_pick_learning_due_entry", side_effect=AssertionError("drain selected a new question")):
                        with self.assertRaisesRegex(RuntimeError, "qa drain reply"):
                            asyncio.run(target(drain, probe.ctx))
                        retry = probe.update(drain.message.text)
                        asyncio.run(target(retry, probe.ctx))
                    self.assertEqual(probe.ctx.user_data[L.FRAME]["current"], frame["current"])
                    self.assertEqual(probe.ctx.user_data[L.FRAME]["revision"], frame["revision"])
                    self.assertEqual(probe.ctx.user_data.get("learning_recent_ids"), recent)
                    self.assertFalse(probe.ctx.user_data[L.FRAME]["pending_render"])
                    for table in ("learning_items", "quiz_events", "transaction_log", "study_actions"):
                        self.assertEqual(probe.read(table), database[table])
                    self.assertEqual(len(probe.read("quiz_events")), 0 if initial_preview else 1)
                    late_state = probe.state()
                    probe.ctx.user_data[L.FRAME] = {"flow_id": "newer-learning", "revision": 98, "pending_render": True, "accepted_delivery": 999999}
                    with patch.object(L, "_render_learning_frame", side_effect=AssertionError("old delivery reopened Learning")):
                        self.assertIsNone(asyncio.run(target(failed, probe.ctx)))
                        self.assertIsNone(asyncio.run(target(drain, probe.ctx)))
                    self.assertEqual(probe.ctx.user_data[L.FRAME]["flow_id"], "newer-learning")
                    self.assertEqual(probe.state(), late_state)

    def test_fresh_dictionary_render_keeps_slot_action_before_and_personal_overlay_separate(self):
        self.add(ready=True)
        self.start()
        self.answer("wrong")
        before = deepcopy(self.ctx.user_data[L.FRAME])
        database = self.state()
        recent = deepcopy(self.ctx.user_data["learning_recent_ids"])
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE words SET german='omega',russian='new translation' WHERE id='a'")
        def personal_overlay(db, uid, entry):
            return {**entry, "image_file": "synthetic-personal-image", "example_de": "personal example"}
        with patch.object(L, "resolve_bot_word_media", side_effect=personal_overlay), patch.object(L, "_pick_learning_due_entry", side_effect=AssertionError("render picked")):
            asyncio.run(L._render_learning_frame(self.update("return"), self.ctx))
        live = self.ctx.user_data[L.FRAME]
        self.assertEqual(live["current"]["entry"]["german"], "omega")
        self.assertEqual(live["current"]["entry"]["russian"], "new translation")
        self.assertEqual({k:v for k,v in live["current"].items() if k != "entry"}, {k:v for k,v in before["current"].items() if k != "entry"})
        for key in ("flow_id", "revision", "last_answer", "last_visible_id", "prefix_text", "phase"):
            self.assertEqual(live.get(key), before.get(key))
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], recent)
        self.assertEqual(self.state(), database)
        self.assertNotEqual(live["current"]["entry"].get("image_file"), "synthetic-personal-image")
        self.assertNotEqual(live["current"]["entry"].get("example_de"), "personal example")
        self.assertEqual(self.ctx.user_data["quiz_entry"]["image_file"], "synthetic-personal-image")

    def test_real_dispatch_back_send_failure_replay_recovers_learning_menu(self):
        import ast
        import subprocess
        import sys
        import textwrap
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES(7,'approved','en','ru')")
            batch = store.get_or_create_batch(conn, 7, 20, NOW)
            store.add_items(conn, 7, ["a"], batch, NOW)
            conn.execute("UPDATE learning_items SET preview_done=1,first_due_pending=0,next_due='2000-01-01T00:00:00Z' WHERE user_id=7")
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "tests/test_new_logic_core_conversation.py").read_text())
        source = next(node.value.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "script" for target in node.targets)
                      and isinstance(node.value, ast.Call))
        script = textwrap.dedent(source)
        extra = textwrap.dedent('''
            from bot.handlers import learning as learning
            conv._conversations[key] = main.LEARNING_MENU
            app.user_data[UID].clear()
            opening = Update(9100, message=message(bot, t('en', 'learning_start'), 9100))
            opening.set_bot(bot)
            await app.process_update(opening)
            assert not errors, errors
            assert conv._conversations[key] == main.LEARNING_QUIZ
            assert learning.FRAME in app.user_data[UID]
            back = Update(9101, message=message(bot, t('en', 'back'), 9101))
            back.set_bot(bot)
            async def definite_failure(self, *args, **kwargs):
                raise RuntimeError('qa definite back delivery')
            with patch.object(FakeBot, 'send_message', definite_failure):
                await app.process_update(back)
            assert len(errors) == 1 and str(errors[0]) == 'qa definite back delivery', errors
            assert conv._conversations[key] == main.LEARNING_QUIZ
            assert learning.FRAME not in app.user_data[UID]
            errors.clear()
            import sqlite3
            with sqlite3.connect(__import__('os').environ['DB_PATH']) as sql:
                before = [list(sql.execute('SELECT * FROM '+table)) for table in ('learning_items','quiz_events','transaction_log','study_deliveries')]
            sent = len(bot.sent)
            await app.process_update(back)
            assert not errors, errors
            assert conv._conversations[key] == main.LEARNING_MENU, ('back replay stranded FSM', conv._conversations[key], main.LEARNING_MENU)
            assert learning.FRAME not in app.user_data[UID]
            assert len(bot.sent) == sent + 1
            with sqlite3.connect(__import__('os').environ['DB_PATH']) as sql:
                after = [list(sql.execute('SELECT * FROM '+table)) for table in ('learning_items','quiz_events','transaction_log','study_deliveries')]
            assert after == before
            assert NoNetworkRequest.attempts == 0
            print('Learning back delivery recovery PASS; network=0')
        ''')
        script = script.replace("        await app.shutdown()", textwrap.indent(extra, "        ") + "        await app.shutdown()")
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=root, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Learning back delivery recovery PASS; network=0", result.stdout)


    def test_real_dispatch_old_learning_back_cannot_move_later_tests_menu(self):
        import subprocess
        real_run = subprocess.run
        def extended_run(command, *args, **kwargs):
            if len(command) > 3 and command[2] == "-c" and "back replay stranded FSM" in command[3]:
                command = list(command)
                repeated_failure = """        with patch.object(FakeBot, 'send_message', definite_failure):
            await app.process_update(back)
        assert len(errors) == 1 and str(errors[0]) == 'qa definite back delivery', errors
        assert conv._conversations[key] == main.LEARNING_QUIZ
        assert learning.FRAME not in app.user_data[UID]
        errors.clear()
"""
                command[3] = command[3].replace("        errors.clear()\n        import sqlite3", "        errors.clear()\n" + repeated_failure + "        import sqlite3")
                following = """        next_back = Update(9102, message=message(bot, t('en', 'back'), 9102))
        next_back.set_bot(bot)
        await app.process_update(next_back)
        assert not errors, errors
        assert conv._conversations[key] == main.TESTS_MENU
        assert learning.FRAME not in app.user_data[UID]
        sent = len(bot.sent)
        await app.process_update(back)
        assert not errors, errors
        assert conv._conversations[key] == main.TESTS_MENU, ('old Learning Back changed newer Tests menu', conv._conversations[key], main.TESTS_MENU)
        assert len(bot.sent) == sent
        assert NoNetworkRequest.attempts == 0
"""
                command[3] = command[3].replace("        print('Learning back delivery recovery PASS; network=0')", "        print('Learning back delivery recovery PASS; network=0')\n" + following)
            return real_run(command, *args, **kwargs)
        # Reuse the unchanged real-dispatch setup; extend only the post-recovery
        # scenario so a replay is checked through its actual new-state handler.
        with patch.object(subprocess, "run", extended_run):
            self.test_real_dispatch_back_send_failure_replay_recovers_learning_menu()


    def test_guard_real_lookup_isolates_users_and_authorization_without_writes(self):
        from bot.study import transport_guard as guard
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO users(user_id,status) VALUES(3,'pending')")
            for uid in (1, 2, 3):
                deliveries.begin_delivery(conn, uid, 7777, f"user-{uid}", "back")
                deliveries.commit_delivery(conn, uid, 7777, {"flow_id": f"user-{uid}"})
        before = self.state()
        trace = []
        real_connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            self.assertTrue(kwargs.get("uri"))
            self.assertTrue(str(args[0]).endswith("?mode=ro"))
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(trace.append)
            return conn
        with patch.object(guard.sqlite3, "connect", side_effect=traced_connect), patch("bot.app_config.is_owner", return_value=False):
            for uid in (1, 2):
                approved, receipt = guard.read_delivery(self.db, uid, 7777)
                self.assertTrue(approved)
                self.assertEqual(receipt["flow_id"], f"user-{uid}")
            self.assertEqual(guard.read_delivery(self.db, 3, 7777), (False, None))
            self.assertEqual(guard.read_delivery(self.db, 444, 7777), (False, None))
            self.assertEqual(guard.read_delivery(self.db, 2, 8888), (True, None))
        self.assertTrue(trace)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in trace), trace)
        data = {}
        self.assertTrue(guard.allow_dispatch(data, 8888, None))
        self.assertEqual(data, {guard.LAST_TRANSPORT: 8888})
        self.assertEqual(self.state(), before)
        missing = Path(self.db).with_name(f"missing-guard-{uuid4().hex}.sqlite")
        self.assertFalse(missing.exists())
        with self.assertRaises(sqlite3.OperationalError):
            guard.read_delivery(str(missing), 1, 9)
        self.assertFalse(missing.exists(), "read preflight created a database")

    def test_guard_readonly_connection_is_closed_when_inspection_raises(self):
        from bot.study import transport_guard as guard
        opened = []
        real_connect = sqlite3.connect
        def tracked_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn
        before = self.state()
        with patch.object(guard.sqlite3, "connect", side_effect=tracked_connect), patch.object(guard, "inspect_delivery", side_effect=sqlite3.DatabaseError("qa guard inspection fault")):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "qa guard inspection fault"):
                guard.read_delivery(self.db, 1, 123)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        self.assertEqual(self.state(), before)

    def test_guard_actual_undo_receipts_and_fresh_watermark_preserve_recovery_rights(self):
        from bot.common.study_lock import invalidate_study_frames
        from bot.study import transport_guard as guard
        self.add(ready=True)
        receipt = self.grade("wrong")
        transport = self.sequence
        self.undo()
        with repo.transaction(self.db) as conn:
            changes = conn.total_changes
            approved, old = guard.inspect_delivery(conn, 1, transport)
            self.assertTrue(approved)
            self.assertEqual(old["action_state"], "undone")
            self.assertEqual(conn.total_changes, changes)
            deliveries.begin_delivery(conn, 1, 9876, "pending-undo", "undo", result={"tx_ids": [], "cursor": 0, "outcomes": []})
        baseline = self.state()
        data = {guard.LAST_TRANSPORT: transport, L.FRAME: {"flow_id": receipt["flow_id"], "pending_render": True, "accepted_delivery": transport}}
        self.assertFalse(guard.allow_dispatch(data, transport, old))
        _, pending = guard.read_delivery(self.db, 1, 9876)
        data[guard.LAST_TRANSPORT] = 9999
        self.assertTrue(guard.allow_dispatch(data, 9876, pending))
        self.assertEqual(data[guard.LAST_TRANSPORT], 9999)
        navigation = {guard.LAST_TRANSPORT: 10, "quiz_learning_pending_navigation": {"transport": 10}}
        old_back = {"flow_id": "back", "action_kind": "back", "state": "committed"}
        self.assertTrue(guard.allow_dispatch(navigation, 10, old_back))
        self.assertTrue(guard.allow_dispatch(navigation, 11, None))
        invalidate_study_frames(SimpleNamespace(user_data=navigation))
        self.assertEqual(navigation, {guard.LAST_TRANSPORT: 11})
        navigation["quiz_learning_pending_navigation"] = {"transport": 10}
        self.assertFalse(guard.allow_dispatch(navigation, 10, old_back))
        self.assertEqual(self.state(), baseline)


if __name__ == "__main__":
    unittest.main()
