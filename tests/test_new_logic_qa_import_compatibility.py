"""Independent K11 acceptance with hostile inputs and full SQLite snapshots."""

from contextlib import closing
from copy import deepcopy
from itertools import product
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from bot import storage
from bot.conjugation.domain import forms_for


PRESENT = "ich fahre\ndu fährst\ner/sie/es fährt\nwir fahren\nihr fahrt\nsie/Sie fahren"
PAST = "ich fuhr\ndu fuhrst\ner/sie/es fuhr\nwir fuhren\nihr fuhrt\nsie/Sie fuhren"


class IndependentImportCompatibility(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(os.environ["TMPDIR"]) / f"qa-import-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": "0"})
        env.start()
        self.addCleanup(env.stop)
        storage.init_db(self.db)
        # Seed directly: the operation under test does not construct its oracle.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO words(id,type,german,infinitive,russian,ukrainian,english,image,example,topic,cefr_level,is_separable,conjugation_praesens,conjugation_praeteritum,updated_at) VALUES('retained','verb','fahren','fahren','ехать','їхати','drive','shared-image','shared-example','original-topic','B2',0,?,?,'old-date')", (PRESENT, PAST))
            for uid in (11, 22):
                conn.execute("INSERT INTO users(user_id,status) VALUES(?,'approved')", (uid,))
                for mode in ("ru_de", "uk_de", "en_de", "de_ru", "oral_ru_de"):
                    conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES(?,'retained',?,17,4,2.8,'2099-01-01')", (uid, mode))
                conn.execute("INSERT INTO conjugation_admissions VALUES(?,'ru_de','retained','old-admission')", (uid,))
                conn.execute("INSERT INTO conjugation_blocks(user_id,pair,word_id,tense,phase,due,cycle,revision) VALUES(?,'ru_de','retained','praesens','test','2099-02-01',3,8)", (uid,))
                conn.execute("INSERT INTO user_settings VALUES(?,'synthetic-retained','original')", (uid,))
                conn.execute("INSERT INTO user_word_media(user_id,word_id,image,example,russian) VALUES(?,'retained','personal-image','personal-example','личный перевод')", (uid,))
                conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES(?,'retained','ru_de',1)", (uid,))
            # Existing init_db registers topics from words on every call. Seed
            # those normal metadata rows before taking any atomicity snapshot.
            conn.executemany("INSERT INTO topics(name) VALUES(?)", [("original-topic",), ("first",), ("1",)])
            conn.commit()

    def dump(self, db=None):
        with closing(sqlite3.connect(db or self.db)) as conn:
            return tuple(conn.iterdump())

    def row(self, ident, db=None):
        with closing(sqlite3.connect(db or self.db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM words WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def verb(self, ident="new", infinitive="reisen", **fields):
        return {"id": ident, "type": "verb", "german": infinitive, "infinitive": infinitive,
                "conjugation_praesens": PRESENT, "conjugation_praeteritum": PAST,
                "is_separable": 0, **fields}

    def test_pos_order_matrix_and_same_pos_duplicates_have_independent_identity(self):
        for spelling, other in (("Bitte", "adverb"), ("Klasse", "adjektiv")):
            for order in (("nomen", other), (other, "nomen")):
                with self.subTest(spelling=spelling, order=order):
                    db = str(Path(self.db).with_name(f"pos-{uuid4().hex}.sqlite"))
                    for kind in order:
                        storage.add_word(db, {"id": kind, "type": f" {kind.upper()} ",
                            "german": f" {spelling.upper()} ", "article": "die" if kind == "nomen" else "",
                            "topic": "first", "english": kind})
                    baseline = self.dump(db)
                    for kind in order:
                        with self.assertRaises(ValueError):
                            storage.add_word(db, {"id": "duplicate", "type": kind,
                                "german": spelling.lower(), "topic": "later", "english": "overwrite"})
                    self.assertEqual(self.dump(db), baseline)
                    self.assertEqual(self.row("nomen", db)["german"], spelling)
                    self.assertEqual(self.row(other, db)["german"], spelling.lower())
                    self.assertEqual(self.row("nomen", db)["english"], "nomen")

    def test_all_semantic_triples_remain_distinct_and_casefold_duplicates_do_not_replace(self):
        categories = ("", "local", "temporal", "modal", "kausal", "final")
        cases = ("", "dativ", "akkusativ", "genitiv", "wechsel")
        expected = set(product(categories, cases))
        for ordinal, (category, case) in enumerate(sorted(expected)):
            storage.add_word(self.db, {"id": f"prep-{ordinal}", "type": "praeposition", "german": "zu",
                "preposition_type": category, "case_type": case, "english": f"meaning-{ordinal}", "topic": "first"})
        storage.add_word(self.db, {"id": "zu-adverb", "type": "adverb", "german": "zu"})
        baseline = self.dump()
        for category, case in expected:
            with self.assertRaises(ValueError):
                storage.add_word(self.db, {"id": "duplicate", "type": " PRAEPOSITION ", "german": " ZU ",
                    "preposition_type": f" {category.upper()} ", "case_type": f" {case.upper()} ", "topic": "later"})
        self.assertEqual(self.dump(), baseline)
        with closing(sqlite3.connect(self.db)) as conn:
            actual = set(conn.execute("SELECT preposition_type,case_type FROM words WHERE german='zu' AND type='praeposition'"))
        self.assertEqual(actual, expected)

    def test_six_plus_six_reference_forms_survive_without_expanding_test_eligibility(self):
        before = self.row("retained")
        for ident, infinitive, separable, past_eligible in (
                ("outside", "reisen", 0, False), ("separable", "anrufen", 1, False),
                ("inside", "haben", 0, True)):
            entry = self.verb(ident, infinitive, is_separable=separable,
                conjugation_praesens="\r\n" + PRESENT.replace(" ", "\t\t") + "\r\n")
            original = deepcopy(entry)
            result = storage.add_word(self.db, entry)
            self.assertEqual(entry, original, "add_word mutated its caller payload")
            saved = self.row(ident)
            self.assertEqual((saved["conjugation_praesens"], saved["conjugation_praeteritum"]), (PRESENT, PAST))
            self.assertEqual(result["conjugation_praesens"], PRESENT)
            self.assertEqual(saved["is_separable"], separable)
            self.assertEqual(forms_for(saved, "praesens")["du"], "fährst")
            self.assertEqual(forms_for(saved, "praeteritum") is not None, past_eligible)
        self.assertEqual(self.row("retained"), before)

    def test_malformed_tables_fail_before_initialization_or_any_filesystem_write(self):
        lines = PRESENT.splitlines()
        malformed = ["one line", "\n".join(lines[:5]), PRESENT + "\nextra seventh line",
                     PRESENT.replace("du fährst", "du"), PRESENT.replace("sie/Sie", "sie/sie")]
        for position in range(5):
            swapped = list(lines)
            swapped[position], swapped[position + 1] = swapped[position + 1], swapped[position]
            malformed.append("\n".join(swapped))
        baseline = self.dump()
        missing_parent = Path(self.db).with_name(f"not-created-{uuid4().hex}")
        for field, bad in product(("conjugation_praesens", "conjugation_praeteritum"), malformed):
            with self.subTest(field=field, bad=bad):
                entry = self.verb(**{field: bad})
                original = deepcopy(entry)
                with patch.object(storage, "init_db", side_effect=AssertionError("validation happened after initialization")) as initialize:
                    with self.assertRaises(ValueError):
                        storage.add_word(str(missing_parent / "word.sqlite"), entry)
                    initialize.assert_not_called()
                self.assertEqual(entry, original)
        self.assertFalse(missing_parent.exists())
        self.assertEqual(self.dump(), baseline)

    def test_invalid_separability_is_rejected_before_initialization(self):
        baseline = self.dump()
        for value in (True, False, -1, 2, 0.0, "0"):
            with self.subTest(value=value):
                with patch.object(storage, "init_db", side_effect=AssertionError("invalid flag reached initialization")) as initialize:
                    with self.assertRaises(ValueError):
                        storage.add_word(self.db, self.verb(is_separable=value))
                    initialize.assert_not_called()
        self.assertEqual(self.dump(), baseline)

    def test_after_insert_fail_rolls_back_word_trigger_effects_and_existing_user_data(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""CREATE TABLE qa_audit(word_id TEXT);
                CREATE TRIGGER qa_after_insert AFTER INSERT ON words WHEN NEW.id='fail' BEGIN
                    UPDATE progress SET repetitions=0;
                    UPDATE words SET english='corrupted' WHERE id='retained';
                    DELETE FROM conjugation_admissions;
                    UPDATE user_word_media SET image='corrupted';
                    INSERT INTO qa_audit VALUES(NEW.id);
                    SELECT RAISE(FAIL,'qa after-insert failure');
                END;""")
        baseline = self.dump()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "qa after-insert failure"):
            storage.add_word(self.db, self.verb("fail"))
        self.assertIsNone(self.row("fail"))
        self.assertEqual(self.dump(), baseline)

    def test_success_and_missing_tables_preserve_nonempty_admissions_and_all_legacy_rows(self):
        with closing(sqlite3.connect(self.db)) as source:
            copy = str(Path(self.db).with_name(f"copy-{uuid4().hex}.sqlite"))
            with closing(sqlite3.connect(copy)) as target:
                source.backup(target)
        source_before = self.dump()
        before = self.dump(copy)
        for ident, tables in (("present-only", {"conjugation_praesens": PRESENT}),
                              ("past-only", {"conjugation_praeteritum": PAST}), ("neither", {})):
            storage.add_word(copy, {"id": ident, "type": "verb", "german": ident,
                                   "infinitive": ident, "is_separable": 0, **tables})
            row = self.row(ident, copy)
            self.assertEqual(row["conjugation_praesens"], tables.get("conjugation_praesens", ""))
            self.assertEqual(row["conjugation_praeteritum"], tables.get("conjugation_praeteritum", ""))
        # Remove only new word INSERTs from the textual snapshot oracle.
        filtered = tuple(line for line in self.dump(copy) if not any(
            line.startswith('INSERT INTO "words" VALUES(\'' + ident + "'")
            for ident in ("present-only", "past-only", "neither")))
        self.assertEqual(filtered, before)
        self.assertEqual(self.dump(), source_before)

    def test_existing_id_and_verb_alias_duplicates_leave_every_table_unchanged(self):
        baseline = self.dump()
        with self.assertRaises(sqlite3.IntegrityError):
            storage.add_word(self.db, self.verb("retained", "unique-new-infinitive"))
        with self.assertRaises(ValueError):
            storage.add_word(self.db, self.verb("alias", " FAHREN ", german="display alias"))
        with self.assertRaises(ValueError):
            storage.add_word(self.db, {"id": "retained", "type": "verb", "german": "fahren", "infinitive": "fahren"})
        self.assertEqual(self.dump(), baseline)


if __name__ == "__main__":
    unittest.main()
