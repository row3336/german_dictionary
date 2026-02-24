import unittest

from bot.retry_logic import maybe_take_retry_entry, register_retry_result


class RetryLogicTests(unittest.TestCase):
    def test_no_queue(self):
        queue = []
        counts = {}
        entry, since_last, took = maybe_take_retry_entry(queue, counts, 3, 3, lambda wid: {"id": wid})
        self.assertIsNone(entry)
        self.assertEqual(since_last, 3)
        self.assertFalse(took)

    def test_gap_not_reached(self):
        queue = ["w1"]
        counts = {}
        entry, since_last, took = maybe_take_retry_entry(queue, counts, 2, 3, lambda wid: {"id": wid})
        self.assertIsNone(entry)
        self.assertEqual(queue, ["w1"])
        self.assertEqual(since_last, 2)
        self.assertFalse(took)

    def test_take_retry_when_gap_reached(self):
        queue = ["w1"]
        counts = {}
        entry, since_last, took = maybe_take_retry_entry(queue, counts, 3, 3, lambda wid: {"id": wid})
        self.assertEqual(entry["id"], "w1")
        self.assertEqual(queue, [])
        self.assertEqual(counts["w1"], 1)
        self.assertEqual(since_last, 0)
        self.assertTrue(took)

    def test_missing_entry_removes_count(self):
        queue = ["w1"]
        counts = {"w1": 2}
        entry, since_last, took = maybe_take_retry_entry(queue, counts, 3, 3, lambda wid: None)
        self.assertIsNone(entry)
        self.assertNotIn("w1", counts)
        self.assertFalse(took)

    def test_register_wrong_adds_queue(self):
        queue = []
        counts = {}
        register_retry_result(queue, counts, "w1", ok=False, was_retry=False, max_repeats=3)
        self.assertEqual(queue, ["w1"])
        self.assertEqual(counts["w1"], 0)

    def test_register_ok_no_change(self):
        queue = []
        counts = {}
        register_retry_result(queue, counts, "w1", ok=True, was_retry=False, max_repeats=3)
        self.assertEqual(queue, [])
        self.assertEqual(counts, {})

    def test_retry_requeue_until_max(self):
        queue = []
        counts = {"w1": 1}
        register_retry_result(queue, counts, "w1", ok=True, was_retry=True, max_repeats=3)
        self.assertEqual(queue, ["w1"])
        self.assertEqual(counts["w1"], 1)

    def test_retry_stops_at_max(self):
        queue = []
        counts = {"w1": 3}
        register_retry_result(queue, counts, "w1", ok=True, was_retry=True, max_repeats=3)
        self.assertEqual(queue, [])
        self.assertNotIn("w1", counts)


if __name__ == "__main__":
    unittest.main()
