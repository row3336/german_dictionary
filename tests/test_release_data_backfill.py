import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.common.db_safety import SCHEMA_VERSION
from bot.storage import init_db
from scripts.reconcile_book_topics import DESCRIPTIONS, MERGES
from scripts.release_data_backfill import (
    DEFAULT_PAYLOAD,
    ReleaseDataError,
    apply_release_data,
    load_payload,
)


class ReleaseDataBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "words.db"
        init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("DELETE FROM word_synonyms")
            conn.execute("DELETE FROM synonym_groups")
            conn.execute("DELETE FROM topics")
            conn.execute("DELETE FROM words")
            for name in sorted(set(DESCRIPTIONS) | set(MERGES)):
                conn.execute(
                    "INSERT INTO topics(name, description, level) VALUES (?, '', '')",
                    (name,),
                )
            conn.execute(
                "INSERT INTO words(id,type,german,russian,topic,cefr_level) "
                "VALUES ('w1','nomen','Alt','old','01','')"
            )
            conn.execute(
                "INSERT INTO words(id,type,german,russian,topic,cefr_level) "
                "VALUES ('w2','nomen','Other','same','02','')"
            )
        self.payload = self.root / "payload.json"
        self.payload.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "release": "test-release",
                    "source": {"word_count": 2},
                    "topics": {"merges": MERGES, "descriptions": DESCRIPTIONS},
                    "synonym_pairs": [
                        {"labels": ["Alt", "Other"], "word_ids": ["w1", "w2"]}
                    ],
                    "word_patches": [
                        {
                            "id": "w1",
                            "label": "Neu",
                            "fields": {
                                "german": {"old": "Alt", "new": "Neu"},
                                "russian": {"old": "old", "new": "new"},
                                "cefr_level": {"old": "", "new": "A1"},
                            },
                        },
                        {
                            "id": "w2",
                            "label": "Other",
                            "fields": {"cefr_level": {"old": "", "new": "A1"}},
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self):
        with sqlite3.connect(self.db) as conn:
            return {
                "words": conn.execute(
                    "SELECT id,german,russian,topic,cefr_level FROM words ORDER BY id"
                ).fetchall(),
                "topics": conn.execute("SELECT * FROM topics ORDER BY name").fetchall(),
                "synonyms": conn.execute("SELECT * FROM word_synonyms ORDER BY word_id").fetchall(),
            }

    def test_packaged_payload_is_structurally_valid(self):
        payload = load_payload(DEFAULT_PAYLOAD)
        self.assertEqual(payload["source"]["word_count"], 907)
        self.assertEqual(len(payload["word_patches"]), 907)
        self.assertEqual(len(payload["synonym_pairs"]), 3)
        self.assertIn(["dann", "danach"], [pair["labels"] for pair in payload["synonym_pairs"]])
        patches = {item["label"]: item["fields"] for item in payload["word_patches"]}
        # was is only the interrogative "что"; the indefinite "что-то" reading was
        # rejected by the owner 03.08. The patch runs backwards (old=long, new=short)
        # so contours that already applied the earlier payload roll back on re-run.
        self.assertEqual(patches["was"]["russian"]["new"], "что")
        self.assertEqual(patches["was"]["russian"]["old"], "что, что-то")
        self.assertEqual(patches["was"]["ukrainian"]["new"], "що")
        self.assertEqual(patches["was"]["ukrainian"]["old"], "що, щось")
        self.assertEqual(
            patches["Arbeitszeit"]["plural"]["new"],
            "Arbeitszeiten, Arbeitzeiten",
        )
        self.assertEqual(patches["um"]["preposition_type"]["new"], "temporal")
        self.assertEqual(patches["um"]["ukrainian"]["new"], "о, об")
        self.assertEqual(patches["von"]["preposition_type"]["new"], "temporal")
        # senden stays the "send a thing" verb: the broadcast reading would drag in
        # the weak sendete/gesendet pair and its translations (owner call 03.08).
        self.assertNotIn("prateritum", patches["senden"])
        self.assertNotIn("partizip2", patches["senden"])
        self.assertNotIn("транслировать", patches["senden"]["russian"]["new"])
        self.assertNotIn("broadcast", patches["senden"]["english"]["new"])
        self.assertNotIn("gesendet", patches["senden"]["example"]["new"])
        for verb in ("denken", "kosten", "spielen", "schwimmen", "geboren werden"):
            self.assertIn("conjugation_praesens", patches[verb])
            self.assertTrue(patches[verb]["conjugation_praesens"]["new"])
        for plural_only in ("Ferien", "Nebenkosten"):
            self.assertNotIn("article", patches[plural_only])
        for protected in ("Flur", "Kiwi", "Modell", "Moment", "Gehalt", "Paprika", "Teil"):
            self.assertNotIn("example", patches[protected])

    def test_dry_run_is_non_mutating(self):
        before = self._snapshot()
        report = apply_release_data(self.db, payload_path=self.payload)
        self.assertFalse(report["applied"])
        self.assertEqual(report["words"]["words_changed"], 2)
        self.assertEqual(report["synonyms"]["groups_created"], 1)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.root / "backups").exists())

    def test_apply_and_second_apply_are_idempotent(self):
        report = apply_release_data(self.db, payload_path=self.payload, apply=True)
        self.assertTrue(report["applied"])
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertTrue(report["healthcheck"]["ok"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT german,russian,topic,cefr_level FROM words WHERE id='w1'"
                ).fetchone(),
                ("Neu", "new", "1", "A1"),
            )
            groups = conn.execute(
                "SELECT group_id,COUNT(*) FROM word_synonyms GROUP BY group_id"
            ).fetchall()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0][1], 2)

        second = apply_release_data(self.db, payload_path=self.payload, apply=True)
        self.assertTrue(second["applied"])
        self.assertTrue(second["noop"])
        self.assertIsNone(second["backup"])
        self.assertEqual(second["words"]["words_changed"], 0)
        self.assertEqual(second["words"]["words_already_current"], 2)
        self.assertEqual(second["synonyms"]["groups_created"], 0)
        self.assertEqual(second["synonyms"]["groups_already_current"], 1)

    def test_partial_old_new_state_is_resumable(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE words SET german='Neu' WHERE id='w1'")
        report = apply_release_data(self.db, payload_path=self.payload)
        self.assertEqual(report["words"]["words_changed"], 2)
        self.assertGreater(report["words"]["fields_already_current"], 0)

    def test_conflict_aborts_before_backup(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE words SET russian='manual edit' WHERE id='w1'")
        before = self._snapshot()
        with self.assertRaisesRegex(ReleaseDataError, "precondition conflicts"):
            apply_release_data(self.db, payload_path=self.payload, apply=True)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.root / "backups").exists())

    def test_rejects_forbidden_payload_field(self):
        payload = json.loads(self.payload.read_text(encoding="utf-8"))
        payload["word_patches"][0]["fields"]["updated_at"] = {
            "old": "",
            "new": "unsafe",
        }
        self.payload.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseDataError, "forbidden fields"):
            load_payload(self.payload)

    def test_refuses_unmigrated_schema(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version=11")
        with self.assertRaisesRegex(ReleaseDataError, "Run scripts.migrate_db first"):
            apply_release_data(self.db, payload_path=self.payload)


if __name__ == "__main__":
    unittest.main()
