"""Synthetic atomicity/replay/undo checks; fixtures deliberately stay in the stand."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot.common.study_lock import study_lock
from bot.storage import init_db
from bot.study import answers, deliveries, repository as repo
from bot.study.srs import apply_answer, update_fsrs

STAND = Path("/Users/wf/py/_agent_work/888_german_dictionary/new_logic_core_tests")
NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


class CoreTest(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.db = str(Path(tempfile.mkdtemp(prefix="core-", dir=STAND)) / "synthetic.sqlite")
        init_db(self.db)
        with repo.transaction(self.db) as conn:
            conn.executemany("INSERT INTO users(user_id,status) VALUES (?,'approved')", [(1,), (2,)])
            conn.executemany("INSERT INTO words(id,type,german) VALUES (?,'verb',?)",
                             [("a", "haben"), ("b", "sein")])
        self.serial = 0

    def grade(self, key="a1", word="a", kind="wrong", update_id=None, user=1, **kwargs):
        self.serial += 1
        return answers.apply_original_answer(
            self.db, user, word, "ru_de", kind, key, update_id or self.serial,
            1, NOW, flow_id="flow", live_revision=1, **kwargs)

    def rows(self, table):
        with repo.transaction(self.db) as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_idk_three_penalties_one_event_transaction_and_receipt(self):
        baseline = {"interval": 90, "repetitions": 8, "ease": 3.1, "due": "2026-10-20"}
        with repo.transaction(self.db) as conn:
            repo.set_progress(conn, 1, "a", "ru_de", baseline, NOW)
        receipt = self.grade(kind="idk")
        expected = baseline
        for _ in range(3):
            expected = update_fsrs(expected, 2, NOW.date())
        self.assertEqual(receipt["after"], expected)
        for table in ("quiz_events", "transaction_log", "study_actions", "study_deliveries"):
            self.assertEqual(len(self.rows(table)), 1)

    def test_delivery_lookup_precedes_new_target_and_revision(self):
        first = self.grade(update_id=100)
        self.grade(key="b1", word="b", update_id=101)
        with patch.object(answers, "_validate_target", side_effect=AssertionError("resolved")):
            replay = answers.apply_original_answer(
                self.db, 1, "b", "de_ru", "correct", "b2", 100, 100, NOW,
                flow_id="another", live_revision=200,
                target_resolver=lambda _: self.fail("picked next question"))
        self.assertEqual(replay["action_key"], first["action_key"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.rows("quiz_events")), 2)

    def test_new_delivery_same_action_returns_receipt_before_stale_revision(self):
        first = self.grade(update_id=10)
        result = answers.apply_original_answer(
            self.db, 1, "b", "de_ru", "correct", "a1", 11, 999, NOW,
            flow_id="stale", live_revision=1000)
        self.assertEqual(result["tx_id"], first["tx_id"])
        self.assertEqual(len(self.rows("study_deliveries")), 2)
        self.assertEqual(len(self.rows("transaction_log")), 1)

    def test_undone_action_and_original_delivery_stay_extinguished(self):
        self.grade(update_id=10)
        answers.undo_steps(self.db, 1, 1, 20)
        for update_id in (10, 11):
            result = self.grade(update_id=update_id)
            self.assertEqual(result["state"], "undone")
        self.assertEqual(self.rows("quiz_events"), [])
        self.assertEqual(self.rows("progress"), [])
        self.assertEqual(self.rows("transaction_log"), [])

    def test_correction_and_two_undos_restore_exact_wrong_then_original(self):
        with repo.transaction(self.db) as conn:
            repo.set_progress(conn, 1, "a", "ru_de",
                              {"interval": 80, "repetitions": 7, "ease": 2.9, "due": "2026-10-20"}, NOW)
        original = self.grade(kind="idk", update_id=10)
        wrong = self.rows("progress")[0]
        correction = answers.correct_last_answer(
            self.db, 1, "a1", "fix1", 11, 2, NOW, flow_id="flow", live_revision=2,
            interval_divisor=1.5)
        expected = apply_answer(original["before"]["progress"]["ru_de"], "correct", NOW.date(), 1.5)
        self.assertEqual(correction["after"], expected)
        self.assertEqual(len(self.rows("quiz_events")), 1)
        self.assertEqual(self.rows("quiz_events")[0]["is_correct"], 1)
        answers.undo_steps(self.db, 1, 1, 12)
        self.assertEqual(self.rows("progress")[0], wrong)
        self.assertEqual(self.rows("quiz_events")[0]["is_correct"], 0)
        with repo.transaction(self.db) as conn:
            self.assertEqual(answers.lookup_action(conn, 1, "fix1")["state"], "undone")
            self.assertEqual(answers.lookup_action(conn, 1, "a1")["state"], "applied")
        replay = answers.correct_last_answer(
            self.db, 1, "a1", "fix1", 13, 99, NOW, flow_id="flow", live_revision=99)
        self.assertEqual(replay["state"], "undone")
        answers.undo_steps(self.db, 1, 1, 14)
        self.assertEqual(self.rows("quiz_events"), [])
        self.assertEqual(self.rows("progress")[0]["interval"], 80)

    def test_correction_delivery_does_not_correct_next_question(self):
        self.grade(update_id=10)
        fixed = answers.correct_last_answer(self.db, 1, "a1", "fix", 11, 1, NOW,
                                            flow_id="flow", live_revision=1)
        self.grade("b1", "b", update_id=12)
        replay = answers.correct_last_answer(self.db, 1, "b1", "fix-b", 11, 99, NOW,
                                             flow_id="flow", live_revision=100)
        self.assertEqual(replay["event_id"], fixed["event_id"])
        self.assertEqual([r["is_correct"] for r in self.rows("quiz_events")], [1, 0])

    def test_new_stale_action_rolls_back_without_delivery(self):
        with self.assertRaises(answers.StaleStudyAction):
            answers.apply_original_answer(self.db, 1, "a", "ru_de", "correct", "stale",
                                          10, 1, NOW, flow_id="flow", live_revision=2)
        self.assertEqual(self.rows("study_deliveries"), [])

    def test_progress_event_tx_action_delivery_failures_are_atomic(self):
        for table in ("progress", "quiz_events", "transaction_log", "study_actions", "study_deliveries"):
            with self.subTest(table=table):
                with repo.transaction(self.db) as conn:
                    conn.execute(f"""CREATE TRIGGER fail_insert BEFORE INSERT ON {table}
                                 BEGIN SELECT RAISE(ABORT,'injected'); END""")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
                    self.grade(key=table)
                for checked in ("progress", "quiz_events", "transaction_log", "study_actions", "study_deliveries"):
                    self.assertEqual(self.rows(checked), [])
                with repo.transaction(self.db) as conn:
                    conn.execute("DROP TRIGGER fail_insert")

    def test_two_users_same_transport_and_key_are_independent(self):
        self.grade(update_id=10, user=1)
        self.grade(update_id=10, user=2)
        answers.undo_steps(self.db, 1, 1, 11)
        self.assertEqual([r["user_id"] for r in self.rows("quiz_events")], [2])

    def test_deleted_word_undo_is_safe_and_extinguishes_receipt(self):
        self.grade(update_id=10)
        with repo.transaction(self.db) as conn:
            conn.execute("DELETE FROM words WHERE id='a'")
        result = answers.undo_steps(self.db, 1, 1, 11)
        self.assertEqual(result["outcomes"][0]["status"], "skipped")
        self.assertEqual(self.rows("transaction_log"), [])
        self.assertEqual(self.grade(update_id=12)["state"], "undone")

    def test_undo_delivery_has_frozen_selection_and_replay(self):
        self.grade("first", update_id=10)
        self.grade("second", word="b", update_id=11)
        result = answers.undo_steps(self.db, 1, 1, 20)
        replay = answers.undo_steps(self.db, 1, 20, 20)
        self.assertEqual(result["tx_ids"], replay["tx_ids"])
        self.assertEqual(len(self.rows("transaction_log")), 1)
        self.assertTrue(replay["replayed"])

    def test_interrupted_undo_resumes_cursor_before_new_answer(self):
        self.grade("first", update_id=10)
        self.grade("second", word="b", update_id=11)
        advance = answers._advance_undo
        calls = []

        def interrupted(conn, uid, delivery, one_step=False):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("crash between commits")
            return advance(conn, uid, delivery, one_step)

        with patch.object(answers, "_advance_undo", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                answers.undo_steps(self.db, 1, 2, 20)
        self.assertEqual(len(self.rows("transaction_log")), 1)
        delivery = next(r for r in self.rows("study_deliveries") if r["transport_update_id"] == 20)
        self.assertEqual(json.loads(delivery["result_json"])["cursor"], 1)
        result = self.grade("new-after-restart", update_id=30)
        self.assertEqual(result["state"], "recovered_undo")
        self.assertEqual(self.rows("transaction_log"), [])
        self.assertEqual(self.rows("progress"), [])
        self.assertTrue(answers.undo_steps(self.db, 1, 2, 20)["replayed"])

    def test_fault_during_restore_rolls_back_cursor_and_state(self):
        self.grade(update_id=10)
        with repo.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER fail_delete BEFORE DELETE ON transaction_log
                         BEGIN SELECT RAISE(ABORT,'undo fault'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "undo fault"):
            answers.undo_steps(self.db, 1, 1, 20)
        self.assertEqual(len(self.rows("quiz_events")), 1)
        self.assertEqual(len(self.rows("progress")), 1)
        self.assertEqual(self.rows("study_actions")[0]["state"], "applied")
        with repo.transaction(self.db) as conn:
            conn.execute("DROP TRIGGER fail_delete")
        self.assertEqual(answers.undo_steps(self.db, 1, 1, 20)["restored"], 1)

    def test_composite_preview_undo_restores_other_rows_after_parent_delete(self):
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES ('batch',1)")
            conn.executemany("""INSERT INTO learning_items
                (user_id,word_id,batch_id,preview_done,next_due,first_due_pending)
                VALUES (1,?,'batch',0,'2026-10-20T16:00:00',1)""", [("a",), ("b",)])

        def finalize(conn, snapshot, now):
            conn.execute("""UPDATE learning_items SET preview_done=1,first_due_pending=0,
                         next_due='2026-10-21T00:00:00' WHERE user_id=1""")
            return {"newly_started_ids": ["a", "b"]}

        receipt = answers.apply_preview_transition(
            self.db, 1, "a", "preview_next", "preview", 10, 1, NOW,
            flow_id="learning", live_revision=1, transition=finalize,
            affected_learning_ids=["a", "b"])
        self.assertEqual(len(receipt["before"]["affected_learning_items"]), 2)
        self.assertEqual(self.rows("quiz_events"), [])
        with repo.transaction(self.db) as conn:
            conn.execute("DELETE FROM words WHERE id='a'")
        answers.undo_steps(self.db, 1, 1, 11)
        remaining = self.rows("learning_items")
        self.assertEqual(len(remaining), 1)
        self.assertEqual((remaining[0]["word_id"], remaining[0]["first_due_pending"]), ("b", 1))
        self.assertEqual(remaining[0]["next_due"], "2026-10-20T16:00:00")

    def test_control_replay_does_not_call_resolver_or_create_learning_result(self):
        calls = []
        for _ in range(2):
            result = answers.record_control_transition(
                self.db, 1, 10, "oral", "reveal",
                lambda conn: calls.append(1) or {"revision": 2, "phase": "revealed"})
            self.assertEqual(result["revision"], 2)
        self.assertEqual(calls, [1])
        self.assertEqual(self.rows("quiz_events"), [])
        self.assertEqual(self.rows("transaction_log"), [])

    def test_host_local_default_day_and_explicit_calendar_day(self):
        class LocalDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 20)

        class UtcClock(datetime):
            @classmethod
            def now(cls, tz=None):
                # An American host is still on the 20th while UTC is on the 21st.
                return cls(2026, 10, 21, 0, 30, tzinfo=timezone.utc)

        with patch.object(answers, "date", LocalDate), patch.object(answers, "datetime", UtcClock):
            result = answers.apply_original_answer(
                self.db, 1, "a", "ru_de", "correct", "local", 10, 1,
                flow_id="flow", live_revision=1)
        self.assertEqual(result["after"]["due"], "2026-10-21")
        self.assertTrue(self.rows("quiz_events")[0]["created_at"].startswith("2026-10-21"))
        result = self.grade("explicit", word="b", update_id=11, srs_day=date(2026, 12, 1))
        self.assertEqual(result["after"]["due"], "2026-12-02")

    def test_lock_serializes_grade_and_undo_frame_boundary(self):
        async def run():
            seen = []

            async def transition(kind):
                async with study_lock(1):
                    seen.append((kind, "begin"))
                    await asyncio.sleep(0)
                    seen.append((kind, "end"))

            await asyncio.gather(transition("grade"), transition("undo"), transition("reset"))
            return seen

        self.assertEqual(asyncio.run(run()), [(kind, point) for kind in
                         ("grade", "undo", "reset") for point in ("begin", "end")])


    def test_correction_sql_failure_restores_wrong_event_and_progress(self):
        self.grade(update_id=10)
        wrong_progress = self.rows("progress")
        wrong_event = self.rows("quiz_events")
        with repo.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER fail_correction BEFORE INSERT ON transaction_log
                         BEGIN SELECT RAISE(ABORT,'correction fault'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "correction fault"):
            answers.correct_last_answer(self.db, 1, "a1", "fix", 11, 1, NOW,
                                        flow_id="flow", live_revision=1)
        self.assertEqual(self.rows("progress"), wrong_progress)
        self.assertEqual(self.rows("quiz_events"), wrong_event)
        self.assertEqual(len(self.rows("study_actions")), 1)
        self.assertEqual(len(self.rows("study_deliveries")), 1)

    def test_admission_latches_reaching_two_even_after_later_wrong_and_undo(self):
        with repo.transaction(self.db) as conn:
            repo.set_progress(conn, 1, "a", "ru_de",
                              {"interval": 1, "repetitions": 1, "ease": 2.5, "due": "2026-10-20"}, NOW)
        self.grade("reach-two", kind="correct", update_id=10)
        self.grade("fall-back", update_id=11)
        answers.undo_steps(self.db, 1, 2, 12)
        self.assertEqual(self.rows("progress")[0]["repetitions"], 1)
        self.assertEqual([(r["user_id"],r["pair"],r["word_id"]) for r in self.rows("conjugation_admissions")],
                         [(1,"ru_de","a")])

    def test_malformed_undo_snapshot_rolls_back_partial_restore_then_skips(self):
        self.grade(update_id=10)
        with repo.transaction(self.db) as conn:
            payload = json.loads(conn.execute("SELECT payload FROM transaction_log").fetchone()[0])
            payload["snapshot"]["affected_learning_items"] = [{"user_id": 2,"word_id":"b","before":None}]
            conn.execute("UPDATE transaction_log SET payload=?", (json.dumps(payload),))
        result = answers.undo_steps(self.db, 1, 1, 20)
        self.assertEqual(result["outcomes"][0]["status"], "skipped")
        self.assertEqual(len(self.rows("progress")), 1)
        self.assertEqual(len(self.rows("quiz_events")), 1)
        self.assertEqual(self.rows("study_actions")[0]["state"], "undone")


    def test_nested_malformed_snapshots_skip_and_do_not_jam_frozen_undo(self):
        variants = [
            {"progress": [{"invalid": "shape"}]},
            {"progress": {"ru_de": []}},
            {"affected_learning_items": [[]]},
            {"affected_learning_items": [{"word_id": "a", "before": []}]},
            {"learning_item": ["invalid"]},
            {"event_before": ["invalid"]},
            {"event_before": {"1": []}},
            {"version": 1, "event_counts": ["invalid"]},
            {"created_event_ids": {"invalid": 1}},
            {"modes": {"invalid": "shape"}},
            {"user_id": float("inf")},
        ]
        for index, variant in enumerate(variants):
            with self.subTest(variant=variant):
                update_id = 100 + index * 3
                receipt = self.grade("nested-" + str(index), update_id=update_id)
                snapshot = {**receipt["before"], **variant}
                with repo.transaction(self.db) as conn:
                    repo.append_transaction(conn, 1, "malformed legacy", {"snapshot": snapshot})
                result = answers.undo_steps(self.db, 1, 2, update_id + 1)
                self.assertEqual(result["restored"], 1)
                self.assertEqual([item["status"] for item in result["outcomes"]], ["skipped", "restored"])
                self.assertEqual(result["cursor"], 2)
                replay = answers.undo_steps(self.db, 1, 99, update_id + 1)
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["tx_ids"], result["tx_ids"])
                for table in ("transaction_log", "quiz_events", "progress"):
                    self.assertEqual(self.rows(table), [])


if __name__ == "__main__":
    unittest.main()
