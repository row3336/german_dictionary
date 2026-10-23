"""K14 S01–S03: exact/variant semantics on isolated SQLite dictionaries."""

import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bot import storage
from bot.search import match_entries, read_search_entry


class SearchMatcherTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix="-k14.sqlite")
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("""CREATE TABLE words (
            id TEXT PRIMARY KEY, created_at TEXT, type TEXT, article TEXT,
            german TEXT, plural TEXT, infinitive TEXT, prateritum TEXT,
            partizip2 TEXT, russian TEXT, ukrainian TEXT, english TEXT,
            image TEXT, example TEXT, topic TEXT, komparativ TEXT,
            superlativ TEXT, conjugation_praesens TEXT,
            conjugation_praeteritum TEXT, is_separable INTEGER, reflexive_case TEXT)""")

    def seed(self, wid, german="", **extra):
        values = {"id": wid, "german": german, "created_at": "2026-01-01", "type": "nomen", **extra}
        self.conn.execute(f"INSERT INTO words ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                          tuple(values.values()))
        self.conn.commit()

    def lookup(self, query):
        with patch.object(storage, "init_db", side_effect=AssertionError("search must not initialize schema")):
            return storage.search_entries(self.db, query)

    def test_s01_all_exact_ids_and_pos_article_phrase_cases_are_independent(self):
        self.seed("see1", "See", article="der")
        self.seed("see2", "See", article="die")
        self.assertEqual(self.lookup("der See")["ordered_ids"], ["see1", "see2"])
        for text, pos in (("Bitte", "nomen"), ("bitte", "adverb"), ("bitte", "phrase")):
            self.seed(pos, text, type=pos)
        result = self.lookup("  BITTE  ")
        self.assertEqual(result["kind"], "exact")
        self.assertEqual(set(result["ordered_ids"]), {"nomen", "adverb", "phrase"})
        self.seed("klasse-noun", "Klasse", type="nomen")
        self.seed("klasse-adj", "klasse", type="adjektiv")
        self.assertEqual(self.lookup("klasse")["ordered_ids"], ["klasse-adj", "klasse-noun"])
        self.seed("prep1", "vor", type="praeposition", russian="перед")
        self.seed("prep2", "vor", type="praeposition", russian="до")
        self.assertEqual(self.lookup("vor")["ordered_ids"], ["prep1", "prep2"])
        self.seed("ref1", "sich waschen", type="verb", reflexive_case="akkusativ")
        self.seed("ref2", "sich waschen", type="verb", reflexive_case="dativ")
        self.assertEqual(self.lookup("sich waschen")["ordered_ids"], ["ref1", "ref2"])
        self.assertEqual(read_search_entry(self.db, "ref2")["reflexive_case"], "dativ")

    def test_s01_no_exact_cap_and_one_id_once_even_many_matching_fields(self):
        for i in range(27):
            self.seed(f"w{i:02}", "gleich", russian="gleich", plural="gleich")
        result = self.lookup("gleich")
        self.assertEqual(result, {"kind": "exact", "ordered_ids": [f"w{i:02}" for i in range(27)],
                                  "has_more_similar": False})
        rows = [{"id": "one", "german": "x"}, {"id": "one", "russian": "x"}]
        self.assertEqual(match_entries(rows, "x")["ordered_ids"], ["one"])

    def test_s02_whole_fields_unicode_whitespace_and_translation_variants(self):
        self.seed("unicode", "ÄRGER   über\nÖl", russian="смятение, тревога; волнение/страх")
        self.assertEqual(self.lookup(" ärger über öl ")["ordered_ids"], ["unicode"])
        for query in ("смятение", "тревога", "волнение", "страх", "смятение, тревога; волнение/страх"):
            self.assertEqual(self.lookup(query)["kind"], "exact", query)
        self.seed("german", "a/b", infinitive="c, d", partizip2="hat gemacht", prateritum="war; ist")
        for query in ("a", "b", "c", "d", "gemacht", "war"):
            self.assertEqual(self.lookup(query)["kind"], "similar", query)
        self.seed("fields", "field", plural="Plural", prateritum="ging", partizip2="gegangen",
                  infinitive="gehen", ukrainian="переклад", english="one, two")
        for query in ("Plural", "ging", "gegangen", "gehen", "переклад", "one", "two"):
            self.assertEqual(self.lookup(query)["ordered_ids"], ["fields"])
        self.seed("not-search-fields", "ordinary", article="article-only", komparativ="better-only",
                  reflexive_case="case-only", example="example-only", topic="topic-only")
        for query in ("article-only", "better-only", "case-only", "example-only", "topic-only"):
            self.assertEqual(self.lookup(query)["kind"], "empty", query)

    def test_s02_raw_id_strip_preserves_case_and_does_not_fall_back_to_similar(self):
        self.seed("ID-Case", "first")
        self.seed("other", "ID-Case component")
        self.assertEqual(self.lookup("  ID-Case  "),
                         {"kind": "exact", "ordered_ids": ["ID-Case"], "has_more_similar": False})
        self.assertEqual(self.lookup("id-case")["ordered_ids"], ["other"])
        self.assertEqual(storage.search_word(self.db, "  ID-Case  ")["id"], "ID-Case")

    def test_s02_empty_article_only_and_literal_wildcards(self):
        self.seed("plain", "axb")
        self.seed("symbols", "xa%b a_b a\\bz")
        for query in ("%", "_", "\\"):
            result = self.lookup(query)
            self.assertEqual(result["kind"], "similar")
            self.assertEqual(result["ordered_ids"], ["symbols"])
        for query in ("", " \n\t ", "der", "die", "das"):
            self.assertEqual(self.lookup(query)["kind"], "empty")
        self.seed("article", "der", type="pronomen")
        self.assertEqual(self.lookup("der")["ordered_ids"], ["article"])

    def test_s02_exact_never_adds_substring_phrase_or_other_similar(self):
        self.seed("exact", "Tafel")
        self.seed("phrase", "die Tafel Schokolade", type="phrase")
        self.assertEqual(self.lookup("Tafel")["ordered_ids"], ["exact"])

    def test_s03_similar_limit_order_created_at_then_id(self):
        self.assertEqual(self.lookup("nothing")["kind"], "empty")
        for i in reversed(range(21)):
            self.seed(f"w{i:02}", "match component")
        self.seed("early", "match component", created_at="2025-01-01")
        result = self.lookup("match")
        self.assertEqual(result["kind"], "similar")
        self.assertTrue(result["has_more_similar"])
        self.assertEqual(result["ordered_ids"], ["early"] + [f"w{i:02}" for i in range(19)])
        self.assertEqual(storage.search_word(self.db, "match")["id"], "early")
        self.conn.execute("DELETE FROM words WHERE id IN ('w19','w20')")
        self.conn.commit()
        twenty = self.lookup("match")
        self.assertEqual(len(twenty["ordered_ids"]), 20)
        self.assertFalse(twenty["has_more_similar"])

    def test_s02_schema18_reader_preserves_metadata_without_ddl(self):
        self.seed("legacy", "sich freuen", conjugation_praesens="ich freue mich", is_separable=0)
        self.conn.execute("ALTER TABLE words DROP COLUMN reflexive_case")
        self.conn.commit()
        before = self.conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        self.assertEqual(self.lookup("sich freuen")["ordered_ids"], ["legacy"])
        entry = read_search_entry(self.db, "legacy")
        self.assertIsNone(entry["reflexive_case"])
        self.assertEqual(entry["conjugation_praesens"], "ich freue mich")
        self.assertEqual(entry["is_separable"], 0)
        self.assertEqual(before, self.conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())


if __name__ == "__main__":
    unittest.main()
