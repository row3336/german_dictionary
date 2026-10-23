"""Retained synthetic checks for tolerant preposition patches and strict verbs."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot import storage
from bot.common.verb_construction import DuplicateIdentity, InvalidClassification, InvalidForms
from bot.study import repository

STAND = Path("/Users/wf/py/_agent_work/888_german_dictionary/k12_preposition_fix_20261023")


class PrepositionPatchCompatibilityTest(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.db = Path(tempfile.mkdtemp(prefix="patch-", dir=STAND)) / "synthetic.sqlite"
        storage.add_word(self.db, {"id": "prep", "type": "praeposition", "german": "mit",
                                   "preposition_type": "modal", "case_type": "dativ", "russian": "с"})
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET updated_at='2000-01-01T00:00:00' WHERE id='prep'")

    def row(self, word_id="prep"):
        with repository.transaction(self.db) as conn:
            return dict(conn.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone())

    def test_invalid_only_patch_does_not_issue_update_or_touch_timestamp(self):
        before = self.row()
        with repository.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER reject_unexpected_update BEFORE UPDATE ON words
                BEGIN SELECT RAISE(ABORT,'unexpected word UPDATE'); END""")
        for patch in ({"preposition_type": "bogus", "case_type": "nonsense"},
                      {"preposition_type": "bogus"}, {"case_type": "nonsense", "unknown": "ignored"}):
            with self.subTest(patch=patch):
                storage.update_word_fields(self.db, "prep", patch)
                self.assertEqual(self.row(), before)

    def test_invalid_category_keeps_curated_value_and_saves_valid_siblings(self):
        storage.update_word_fields(self.db, "prep", {
            "preposition_type": "bogus", "case_type": " GENITIV ", "russian": "  ПРОВЕРКА  "})
        row = self.row()
        self.assertEqual((row["preposition_type"], row["case_type"], row["russian"]),
                         ("modal", "genitiv", "проверка"))
        self.assertNotEqual(row["updated_at"], "2000-01-01T00:00:00")

    def test_invalid_case_keeps_curated_value_and_saves_valid_category(self):
        storage.update_word_fields(self.db, "prep", {"preposition_type": " LOCAL ", "case_type": "bogus"})
        row = self.row()
        self.assertEqual((row["preposition_type"], row["case_type"]), ("local", "dativ"))

    def test_valid_empty_values_still_clear_controlled_fields(self):
        storage.update_word_fields(self.db, "prep", {"preposition_type": "", "case_type": "   "})
        row = self.row()
        self.assertEqual((row["preposition_type"], row["case_type"]), ("", ""))

    def test_invalid_reflexive_metadata_remains_strict_and_atomic(self):
        storage.add_word(self.db, {"id": "verb", "type": "verb", "german": "gehen",
                                   "infinitive": "gehen", "reflexive_case": "none"})
        before = self.row("verb")
        for patch, error in (({"preposition_type": "bogus", "reflexive_case": "wechsel"}, InvalidClassification),
                             ({"case_type": "bogus", "conjugation_praesens": "ich gehe"}, InvalidForms)):
            with self.subTest(patch=patch):
                with self.assertRaises(error):
                    storage.update_word_fields(self.db, "verb", patch)
                self.assertEqual(self.row("verb"), before)

    def test_duplicate_identity_and_study_history_repairs_remain_strict(self):
        for word_id, infinitive in (("a", "gehen"), ("b", "kommen")):
            storage.add_word(self.db, {"id": word_id, "type": "verb", "german": infinitive,
                                       "infinitive": infinitive, "reflexive_case": "none"})
        before = self.row("a")
        with self.assertRaises(DuplicateIdentity):
            storage.update_word_fields(self.db, "a", {
                "preposition_type": "bogus", "german": "kommen", "infinitive": "kommen"})
        self.assertEqual(self.row("a"), before)
        with repository.transaction(self.db) as conn:
            repository.set_progress(conn, 1, "a", "ru_de", {
                "interval": 2, "repetitions": 1, "ease": 2.5, "due": "2099-01-01"})
        with self.assertRaisesRegex(ValueError, "repair manifest"):
            storage.update_word_fields(self.db, "a", {
                "case_type": "bogus", "german": "sich waschen", "infinitive": "sich waschen",
                "reflexive_case": "akkusativ"})
        self.assertEqual(self.row("a"), before)


if __name__ == "__main__":
    unittest.main()
