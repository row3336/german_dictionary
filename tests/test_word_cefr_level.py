"""Per-word CEFR level (words.cefr_level) storage round-trip + validation.

The level is a controlled vocabulary (CEFR_LEVELS): add_word/update_word_fields
uppercase a valid value and drop anything else to '' (never store free text), and
get_word_by_id reads it back so the recheck diff and future level-aware topic
filters can use it.
"""

import os
import tempfile
import unittest

from bot.storage import (
    _safe_cefr_level,
    add_word,
    get_word_by_id,
    update_word_fields,
)


class CefrLevelStorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "words.db")

    def _add(self, **fields):
        base = {"type": "nomen", "german": "Haus", "article": "das", "russian": "дом"}
        base.update(fields)
        return add_word(self.db, base)["id"]

    def test_safe_cefr_level_normalizes_and_guards(self):
        self.assertEqual(_safe_cefr_level("a1"), "A1")
        self.assertEqual(_safe_cefr_level(" C2 "), "C2")
        self.assertEqual(_safe_cefr_level("B1"), "B1")
        for bad in ("", "Z9", "a11", "1", "level", None):
            self.assertEqual(_safe_cefr_level(bad), "")

    def test_add_word_stores_valid_level_uppercased(self):
        wid = self._add(cefr_level="a1")
        self.assertEqual(get_word_by_id(self.db, wid)["cefr_level"], "A1")

    def test_add_word_drops_invalid_level(self):
        wid = self._add(cefr_level="Z9")
        self.assertEqual(get_word_by_id(self.db, wid)["cefr_level"], "")

    def test_add_word_without_level_defaults_empty(self):
        wid = self._add()
        self.assertEqual(get_word_by_id(self.db, wid)["cefr_level"], "")

    def test_update_word_fields_sets_and_clears_level(self):
        wid = self._add(cefr_level="A1")
        update_word_fields(self.db, wid, {"cefr_level": "b2"})
        self.assertEqual(get_word_by_id(self.db, wid)["cefr_level"], "B2")
        # invalid value clears the field rather than storing garbage
        update_word_fields(self.db, wid, {"cefr_level": "xx"})
        self.assertEqual(get_word_by_id(self.db, wid)["cefr_level"], "")


if __name__ == "__main__":
    unittest.main()
