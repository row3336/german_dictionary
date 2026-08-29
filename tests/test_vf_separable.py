"""Sprint 290826 point 2: tri-state VF separable-verb storage contract."""

import os
import sqlite3
import tempfile
import unittest

from bot.storage import add_word, get_word_by_id, init_db, update_word_fields


class SeparableStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "words.db")
        init_db(self.db)

    def _add(self, infinitive, value):
        return add_word(
            self.db,
            {
                "type": "verb",
                "infinitive": infinitive,
                "russian": infinitive,
                "topic": "1",
                "is_separable": value,
            },
        )["id"]

    def test_add_and_row_round_trip_preserve_none_zero_one(self):
        ids = [self._add("unknown", None), self._add("no", 0), self._add("yes", 1)]
        values = [get_word_by_id(self.db, word_id)["is_separable"] for word_id in ids]
        self.assertEqual(values, [None, 0, 1])

        conn = sqlite3.connect(self.db)
        try:
            raw = conn.execute(
                "SELECT is_separable FROM words WHERE id = ?", (ids[0],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(raw)

    def test_update_round_trip_preserves_each_state(self):
        word_id = self._add("wechseln", None)
        for value in (0, 1, None):
            update_word_fields(self.db, word_id, {"is_separable": value})
            self.assertIs(get_word_by_id(self.db, word_id)["is_separable"], value)

    def test_add_and_update_reject_values_outside_exact_tristate(self):
        for value in (-1, 2, "0", "1", "", False, True, 1.0):
            with self.subTest(operation="add", value=value):
                with self.assertRaises(ValueError):
                    self._add(f"bad-{value!r}", value)

        word_id = self._add("valid", None)
        for value in (-1, 2, "0", "1", "", False, True, 1.0):
            with self.subTest(operation="update", value=value):
                with self.assertRaises(ValueError):
                    update_word_fields(self.db, word_id, {"is_separable": value})

    def test_row_conversion_rejects_invalid_database_value(self):
        word_id = self._add("corrupt", None)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE words SET is_separable = 2 WHERE id = ?", (word_id,))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            get_word_by_id(self.db, word_id)


if __name__ == "__main__":
    unittest.main()
