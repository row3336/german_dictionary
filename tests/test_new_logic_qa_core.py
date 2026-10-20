"""Independent risk checks for legacy undo and frozen atomic recovery."""

import json
import unittest
from unittest.mock import patch

from tests import test_new_logic_core as fixture
from bot.study import answers, repository as repo




class IndependentCoreQA(unittest.TestCase):
    setUp = fixture.CoreTest.setUp
    grade = fixture.CoreTest.grade
    rows = fixture.CoreTest.rows

    def test_non_object_legacy_payload_is_skipped_without_jamming_frozen_undo(self):
        self.grade(update_id=10)
        with repo.transaction(self.db) as conn:
            for payload in ("legacy text", 7, None, []):
                conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES (1,'legacy',?)",
                             (json.dumps(payload),))
        result = answers.undo_steps(self.db, 1, 5, 20)
        self.assertEqual(result["restored"], 1)
        self.assertEqual([x["status"] for x in result["outcomes"]], ["skipped"] * 4 + ["restored"])
        self.assertEqual(self.rows("transaction_log"), [])
        self.assertEqual(self.rows("quiz_events"), [])
        self.assertTrue(answers.undo_steps(self.db, 1, 5, 20)["replayed"])

    def test_nested_malformed_legacy_snapshots_skip_then_restore_and_replay(self):
        self.grade(update_id=10)
        base = {"version": 2, "user_id": 1, "word_id": "a", "modes": ["ru_de"],
                "progress": {}, "affected_learning_items": []}
        bad_snapshots = [
            dict(base, progress=[{"invalid": "shape"}]),
            dict(base, affected_learning_items=[[]]),
            dict(base, event_before=[{"invalid": "shape"}]),
            dict(base, version=1, event_counts=[{"invalid": "shape"}]),
        ]
        with repo.transaction(self.db) as conn:
            for snapshot in bad_snapshots:
                conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES (1,'legacy',?)",
                             (json.dumps({"snapshot": snapshot}),))
        result = answers.undo_steps(self.db, 1, 5, 20)
        self.assertEqual(result["restored"], 1)
        self.assertEqual([x["status"] for x in result["outcomes"]], ["skipped"] * 4 + ["restored"])
        for table in ("transaction_log", "quiz_events", "progress"):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(len(result["tx_ids"]), 5)
        replay = answers.undo_steps(self.db, 1, 99, 20)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["tx_ids"], result["tx_ids"])
        self.assertEqual(replay["outcomes"], result["outcomes"])

    def test_cursor_write_failure_rolls_back_restore_and_same_request_recovers(self):
        self.grade(update_id=10)
        before = {table: self.rows(table) for table in ("progress", "quiz_events", "transaction_log", "study_actions")}
        with repo.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER qa_fail_cursor BEFORE UPDATE ON study_deliveries
                WHEN OLD.action_kind='undo' BEGIN SELECT RAISE(ABORT,'QA cursor failure'); END""")
        with self.assertRaisesRegex(Exception, "QA cursor failure"):
            answers.undo_steps(self.db, 1, 1, 20)
        for table, expected in before.items():
            self.assertEqual(self.rows(table), expected)
        with repo.transaction(self.db) as conn:
            cursor = json.loads(conn.execute("SELECT result_json FROM study_deliveries WHERE transport_update_id=20").fetchone()[0])
            self.assertEqual(cursor["cursor"], 0)
            conn.execute("DROP TRIGGER qa_fail_cursor")
        result = answers.undo_steps(self.db, 1, 20, 20)
        self.assertEqual(len(result["tx_ids"]), 1)
        self.assertEqual(result["restored"], 1)

    def test_transport_replay_across_flow_kind_never_invokes_resolver(self):
        original = self.grade(update_id=10)
        with patch.object(answers, "_validate_target", side_effect=AssertionError("target")):
            replay = answers.record_control_transition(self.db, 1, 10, "different-flow", "stop",
                lambda _: self.fail("duplicate delivery resolved another transition"))
        self.assertEqual(replay["action_key"], original["action_key"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.rows("quiz_events")), 1)



if __name__ == "__main__":
    unittest.main()
