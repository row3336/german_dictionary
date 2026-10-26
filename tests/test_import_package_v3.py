"""PONS import contract checks; no production database writes."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from bot.common import import_package_v3 as importer


class ImportPackageV3Tests(unittest.TestCase):
    def test_source_rejects_unapproved_apply_flag(self):
        source = {"schema": importer.SOURCE_SCHEMA, "digest": importer.SOURCE_DIGEST,
                  "phase": "prepared_data_unbound", "binding": None,
                  "import_ready": True, "runtime_apply_compatible": False,
                  "approved_to_apply": False}
        with patch.object(importer, "digest", return_value=importer.SOURCE_DIGEST):
            with self.assertRaisesRegex(ValueError, "apply flags"):
                importer.validate_source(source)

    def test_combined_base_changes_leave_example_out_of_scope(self):
        baseline = {"id": "word1", "example": "old", "topic": "03",
                    "russian": "старый", "updated_at": "before",
                    "reflexive_case": None, "perfekt_variants_json": None}
        bound = {"original_bound": {"binding": {
            "confirmations": [{"word_id": "word1"}],
            "metadata": [{"word_id": "word1", "patch": {"perfekt_variants_json": "linked"}}],
            "topics": {"moves": [{"word_id": "word1", "new": {"topic": "2"}}]},
        }}}
        expected, may_update_timestamp = importer._expected_after_base(
            bound, {"id": "word1", "baseline": deepcopy(baseline)})
        self.assertEqual(expected["example"], "old")
        self.assertEqual(expected["topic"], "2")
        self.assertEqual(expected["reflexive_case"], "none")
        self.assertEqual(expected["perfekt_variants_json"], "linked")
        self.assertEqual(expected["russian"], "старый")
        self.assertTrue(may_update_timestamp)
        self.assertEqual(baseline["topic"], "03")

    def test_pons_topic_registry_rejects_existing_conflict(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE topics(name TEXT, description TEXT, level TEXT)")
        self.assertTrue(importer._topic_plan(conn)["insert"])
        conn.execute("INSERT INTO topics VALUES('PONS','Other','A1')")
        with self.assertRaisesRegex(ValueError, "conflicting PONS"):
            importer._topic_plan(conn)

    def test_rehearsal_approval_cannot_target_another_database(self):
        conn = sqlite3.connect(":memory:")
        approval = {"scope": "rehearsal_only",
                    "target_path": str(importer.REHEARSAL_ROOT / "words.sqlite")}
        with self.assertRaisesRegex(PermissionError, "one disposable DB"):
            importer._check_rehearsal_target(conn, approval)

    def test_savepoint_rolls_back_partial_word_change(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE words(id TEXT,example TEXT)")
        conn.execute("INSERT INTO words VALUES('one','original')")
        conn.commit()
        conn.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            with importer._savepoint(conn):
                conn.execute("UPDATE words SET example='changed' WHERE id='one'")
                raise RuntimeError("later import failed")
        self.assertEqual(conn.execute("SELECT example FROM words WHERE id='one'").fetchone()[0], "original")
        conn.rollback()

    def test_frozen_adjective_pairs_do_not_cross_unapproved_forms(self):
        path = Path("/Users/wf/ai_wf/ai_wf_data/888_german-dictionary-doki/"
                    "data18_merged_pons_2026-10-25/merged.package.v3.draft.json")
        if not path.is_file():
            self.skipTest("frozen PONS package is outside the repository")
        source = json.loads(path.read_text())
        pairs = importer._degree_pair_plans(source)
        self.assertEqual(len(pairs), 103)
        self.assertEqual(len({p["word_id"] for p in pairs}), 97)
        expected = {
            "88176cfb-94ac-51c4-ad7f-c74e1366aaa6": {
                ("blasser", "am blassesten"),
                ("blässer", "am blässesten"),
            },
            "b0b2b407-56d3-5d03-8fb4-c81db8dc53d5": {
                ("ungesünder", "am ungesündesten"),
                ("ungesunder", "am ungesundesten"),
            },
        }
        for word_id, allowed in expected.items():
            actual = {(p["comparative"], p["superlative"])
                      for p in pairs if p["word_id"] == word_id}
            self.assertEqual(actual, allowed)
        hoch = next(card for card in source["cards"]
                    if card["fields"].get("german") == "hoch"
                    and card["fields"]["type"] == "adjektiv")
        self.assertEqual([(p["comparative"], p["superlative"])
                          for p in pairs if p["word_id"] == hoch["id"]],
                         [("höher", "am höchsten")])
        by_id = {card["id"]:card for card in source["cards"]}
        article_only = set()
        for pair in pairs:
            card = by_id[pair["word_id"]]
            source_forms = card["comparison"]["superlative_forms"]
            if any(form.startswith("am ") for form in source_forms):
                self.assertFalse(pair["superlative"].startswith(("der ", "die ", "das ")))
            elif pair["superlative"].startswith(("der ", "die ", "das ")):
                article_only.add(card["fields"]["german"])
        self.assertEqual(article_only, {"verschieden", "innere"})

    def test_adjective_pair_table_contract_rejects_incompatible_existing_table(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE words(id TEXT PRIMARY KEY)")
        self.assertFalse(importer._degree_table_state(conn))
        conn.execute(importer.adjective_forms.CREATE_TABLE_SQL)
        self.assertTrue(importer._degree_table_state(conn))
        conn.execute("INSERT INTO words(id) VALUES('one')")
        conn.execute("INSERT INTO adjective_degree_variants VALUES('one','blasser','am blassesten')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO adjective_degree_variants VALUES('one','','')")
        conn2 = sqlite3.connect(":memory:")
        conn2.execute("CREATE TABLE adjective_degree_variants(word_id TEXT)")
        with self.assertRaisesRegex(ValueError, "incompatible adjective degree table"):
            importer._degree_table_state(conn2)
        conn.close()
        conn2.close()

    def test_accepted_frozen_package_when_available(self):
        path = Path("/Users/wf/ai_wf/ai_wf_data/888_german-dictionary-doki/"
                    "data18_merged_pons_2026-10-25/merged.package.v3.draft.json")
        if not path.is_file():
            self.skipTest("frozen PONS package is outside the repository")
        source = json.loads(path.read_text())
        self.assertEqual(importer.validate_source(source), importer.EXPECTED)
        source["cards"][350]["fields"]["german"] = "changed"
        with self.assertRaisesRegex(ValueError, "modified source envelope"):
            importer.validate_source(source)


if __name__ == "__main__":
    unittest.main()
