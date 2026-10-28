"""Owner book structure: PONS chapter split and GSV rename on synthetic stand DBs.

Cards use the packaged manifest IDs/identities; users, progress and settings
are synthetic. Every database lives in the retained GD_TEST_STAND directory.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bot.common import book_topic_structure as bts
from bot.common import word_filter as wf
from bot.common.db_safety import SCHEMA_VERSION
from bot.storage import init_db
from scripts import book_topics_owner_structure as cli

PONS_KEYS = [f"PONS_{n:02}" for n in range(15)]
EXPECTED_COUNTS = [59, 54, 97, 79, 73, 59, 81, 172, 166, 93, 98, 74, 88, 66, 84]


def v2(levels, choices, revision):
    return json.dumps({"version": 2, "selected_levels": levels, "topics_by_level": choices,
                       "browsing_level": levels[0], "revision": revision})


class OwnerStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = cli.read_manifest()

    def setUp(self):
        root = os.environ.get("GD_TEST_STAND")
        if not root:
            raise RuntimeError("set GD_TEST_STAND to the retained session stand")
        self.root = Path(tempfile.mkdtemp(prefix="owner-structure-", dir=root))
        self.db = self.root / "words.db"
        init_db(self.db)
        self.conn = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.seed()

    def seed(self):
        c = self.conn
        c.execute("BEGIN")
        c.execute("DELETE FROM topics")
        c.executemany("INSERT INTO topics(name,description,level) VALUES(?,?,?)", [
            ("0", "Anfang", "A1"), ("3", "Häuser und Wohnungen", "A1"), ("A2_01", "Meine Geschichte", "A2"),
            ("B1_Tartorg", "Tartorg", "B1"), ("GSV", "ГСВМ", "B1"), ("PONS", "PONS_B1", "PONS")])
        for card in self.manifest["cards"]:
            c.execute("""INSERT INTO words(id,type,german,infinitive,russian,topic,cefr_level,updated_at)
                         VALUES(?,?,?,?,?,?,?,'')""",
                      (card["id"], card["type"], card["german"],
                       card["id"] if card["type"] == "verb" else "", "x", "PONS", "B1"))
        for wid, topic in (("a1-0", "0"), ("a1-3", "3"), ("a2-1", "A2_01"), ("b1-t", "B1_Tartorg"),
                           ("gsv-1", "GSV"), ("gsv-2", "GSV")):
            c.execute("INSERT INTO words(id,type,german,russian,topic,cefr_level) VALUES(?,?,?,?,?,?)",
                      (wid, "nomen", wid, "y", topic, "A1"))
        self.pons_word = self.manifest["cards"][0]["id"]
        for uid in range(1, 11):
            c.execute("INSERT INTO users(user_id,status) VALUES(?, 'approved')", (uid,))
        c.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) "
                  "VALUES(1,?,'ru_de',3,2,2.5,'2026-11-01')", (self.pons_word,))
        c.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES(1,?,'ru_de',1)", (self.pons_word,))
        c.execute("INSERT INTO user_levels(user_id,level) VALUES(4,'B1')")
        c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('topic_filter','all')")
        c.execute("INSERT INTO app_settings(key,value) VALUES('dummy_unrelated','synthetic')")
        settings = {
            1: {"topic_filter": "PONS+3"},
            2: {"book_topic_filter_v2": v2(["A1", "PONS"], {"A1": "all", "PONS": ["PONS"]}, 4),
                "filter_revision": "4"},
            3: {"book_topic_filter_v2": v2(["PONS"], {"PONS": "all"}, 2), "filter_revision": "2"},
            4: {"filter_mode": "cefr", "filter_revision": "6",
                "book_topic_filter_v2": v2(["A1"], {"A1": "all", "PONS": ["PONS", "old99"]}, 6)},
            5: {"topic_filter": "all"},
            6: {"topic_filter": ""},
            7: {"topic_filter": "GSV+0", "filter_revision": "3"},
            8: {"topic_filter": "PONS,old99,PONS"},
            9: {"topic_filter": "PONS", "filter_revision": "1",
                "book_topic_filter_v2": v2(["A1"], {"A1": ["0"]}, 1)},
            10: {"book_topic_filter_v2": v2(["B1"], {"B1": ["GSV"], "PONS": "all"}, 0)},
        }
        for uid, values in settings.items():
            c.executemany("INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)",
                          [(uid, k, v) for k, v in values.items()])
        c.execute("COMMIT")

    def settings(self, uid):
        return dict(self.conn.execute("SELECT key,value FROM user_settings WHERE user_id=?", (uid,)))

    def image(self):
        """Full logical image of the database for unchanged-state assertions."""
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: sorted(map(repr, self.conn.execute(f'SELECT * FROM "{t}"'))) for t in tables}

    def words_except_topic(self):
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(words)") if r[1] != "topic"]
        return self.conn.execute(f"SELECT {','.join(cols)} FROM words ORDER BY id").fetchall()

    def run_tx(self, fn, *args):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(self.conn, *args)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return result

    def assert_rejected(self, message, fn=bts.apply, manifest=None):
        before = self.image()
        with self.assertRaisesRegex(bts.TopicStructureError, message):
            self.run_tx(fn, manifest or self.manifest)
        self.assertEqual(self.image(), before)

    def test_manifest_is_pinned_one_topic_per_card_and_owner_counts(self):
        m = self.manifest
        self.assertEqual(bts.digest(m), bts.MANIFEST_DIGEST)
        self.assertEqual(len(m["cards"]), 1343)
        self.assertEqual(len({c["id"] for c in m["cards"]}), 1343)
        self.assertEqual(list(m["counts"]), PONS_KEYS)
        self.assertEqual(list(m["counts"].values()), EXPECTED_COUNTS)
        self.assertEqual([r["description"] for r in m["new_registry"]][:3],
                         ["Allgemeiner Wortschatz", "Das bin ich", "Eine neue Sprache lernen"])
        self.assertEqual(m["new_registry"][14]["description"], "Dienstleistungen und Behörden")
        self.assertEqual(m["gsv"]["new_description"], "Reflexive verbs")
        for mutate, error in (
                (lambda x: x["cards"][0].update(topic="PONS_14"), "first-occurrence"),
                (lambda x: x["cards"].append(dict(x["cards"][1])), "duplicate"),
                (lambda x: x["counts"].update(PONS_00=1), "counts"),
                (lambda x: x["new_registry"][0].update(level="A1"), "registry")):
            bad = deepcopy(m)
            mutate(bad)
            with self.subTest(error=error), self.assertRaisesRegex(bts.TopicStructureError, error):
                bts.validate_manifest(bad)
        tampered = deepcopy(m)
        tampered["provenance"]["snapshot_sha256"] = "0" * 64
        with self.assertRaisesRegex(bts.TopicStructureError, "digest"):
            bts.validate_manifest(tampered)

    def test_plan_is_read_only_and_lists_exact_filter_changes(self):
        before, changes = self.image(), self.conn.total_changes
        self.conn.execute("BEGIN")
        report = bts.plan(self.conn, self.manifest)
        self.conn.execute("ROLLBACK")
        self.assertEqual((self.image(), self.conn.total_changes), (before, changes))
        self.assertEqual((report["state"], report["moves"]), ("ready", 1343))
        touched = {(c["user_id"], c["key"]) for c in report["filters"]}
        self.assertEqual(touched, {
            (1, "topic_filter"), (1, "filter_revision"),
            (2, "book_topic_filter_v2"), (2, "filter_revision"),
            (4, "book_topic_filter_v2"), (4, "filter_revision"),
            (8, "topic_filter"), (8, "filter_revision"),
            (9, "topic_filter"), (9, "book_topic_filter_v2"), (9, "filter_revision")})

    def test_apply_moves_only_topics_and_expands_filters_as_the_same_set(self):
        words, progress = self.words_except_topic(), self.conn.execute("SELECT * FROM progress").fetchall()
        untouched = {uid: self.settings(uid) for uid in (3, 5, 6, 7, 10)}
        report = self.run_tx(bts.apply, self.manifest)
        self.assertEqual(report["state"], "applied")
        self.assertEqual(self.words_except_topic(), words)
        self.assertEqual(self.conn.execute("SELECT * FROM progress").fetchall(), progress)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 1)
        counts = dict(self.conn.execute("SELECT topic,COUNT(*) FROM words GROUP BY topic"))
        self.assertEqual([counts[k] for k in PONS_KEYS], EXPECTED_COUNTS)
        self.assertNotIn("PONS", counts)
        self.assertEqual(counts["GSV"], 2)
        self.assertEqual(sum(counts.values()), 1343 + 6)
        for card in self.manifest["cards"]:
            self.assertEqual(self.conn.execute("SELECT topic FROM words WHERE id=?", (card["id"],)).fetchone()[0],
                             card["topic"])
        registry = {r[0]: r[1:] for r in self.conn.execute("SELECT name,description,level FROM topics")}
        self.assertNotIn("PONS", registry)
        self.assertEqual(registry["GSV"], ("Reflexive verbs", "B1"))
        self.assertEqual(registry["PONS_08"], ("Mobilität", "PONS"))
        self.assertEqual(registry["0"], ("Anfang", "A1"))
        expanded = "+".join(PONS_KEYS)
        self.assertEqual(self.settings(1), {"topic_filter": "3+" + expanded, "filter_revision": "1"})
        self.assertEqual(self.settings(8), {"topic_filter": "old99+" + expanded, "filter_revision": "1"})
        s2 = json.loads(self.settings(2)["book_topic_filter_v2"])
        self.assertEqual((s2["topics_by_level"], s2["revision"], self.settings(2)["filter_revision"]),
                         ({"A1": "all", "PONS": PONS_KEYS}, 5, "5"))
        s4 = self.settings(4)
        v4 = json.loads(s4["book_topic_filter_v2"])
        self.assertEqual((v4["selected_levels"], v4["topics_by_level"]["PONS"], v4["revision"]),
                         (["A1"], ["old99"] + PONS_KEYS, 7))
        self.assertEqual((s4["filter_mode"], s4["filter_revision"]), ("cefr", "7"))
        self.assertEqual(self.conn.execute("SELECT level FROM user_levels WHERE user_id=4").fetchall(), [("B1",)])
        s9 = self.settings(9)
        self.assertEqual((s9["topic_filter"], json.loads(s9["book_topic_filter_v2"])["topics_by_level"],
                          json.loads(s9["book_topic_filter_v2"])["revision"], s9["filter_revision"]),
                         (expanded, {"A1": ["0"]}, 2, "2"))
        self.assertEqual({uid: self.settings(uid) for uid in untouched}, untouched)
        self.assertEqual(dict(self.conn.execute("SELECT key,value FROM app_settings WHERE key IN "
                                                "('topic_filter','dummy_unrelated')")),
                         {"topic_filter": "all", "dummy_unrelated": "synthetic"})
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(setattr, self.conn, "row_factory", None)
        for uid, total in ((2, 1343 + 2), (3, 1343)):
            state = wf.read_filter_state(uid, self.conn)
            self.assertEqual(wf.selected_topics(state, "PONS"), PONS_KEYS)
            self.assertEqual(wf.count_words_for_filter(uid, self.conn), total)

    def test_replay_is_a_zero_operation_and_seed_never_revives_old_topic(self):
        self.run_tx(bts.apply, self.manifest)
        before, changes = self.image(), self.conn.total_changes
        self.assertEqual(self.run_tx(bts.apply, self.manifest)["state"], "already_applied")
        self.conn.execute("BEGIN")
        self.assertEqual(bts.plan(self.conn, self.manifest)["operations"], 0)
        self.conn.execute("ROLLBACK")
        self.assertEqual((self.image(), self.conn.total_changes), (before, changes))
        init_db(self.db)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM topics WHERE name='PONS'").fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM topics WHERE level='PONS'").fetchone()[0], 15)

    def test_rollback_restores_sets_with_newer_revisions_and_allows_reapply(self):
        original = self.image()
        filters_before = {uid: self.settings(uid) for uid in range(1, 11)}
        self.run_tx(bts.apply, self.manifest)
        report = self.run_tx(bts.rollback, self.manifest)
        self.assertEqual((report["state"], report["filter_users"]), ("rolled_back", [1, 2, 4, 8, 9]))
        after = self.image()
        for table in set(original) - {"user_settings"}:
            self.assertEqual(after[table], original[table], table)
        for uid, before in filters_before.items():
            now = self.settings(uid)
            if uid not in report["filter_users"]:
                self.assertEqual(now, before)
                continue
            old_rev = int(before.get("filter_revision", "0"))
            self.assertEqual(now["filter_revision"], str(old_rev + 2))
            self.assertEqual(now.get("topic_filter"), before.get("topic_filter"))
            if "book_topic_filter_v2" in before:
                old, new = json.loads(before["book_topic_filter_v2"]), json.loads(now["book_topic_filter_v2"])
                self.assertEqual(new.pop("revision"), old_rev + 2)
                old.pop("revision")
                self.assertEqual(new, old)
        self.assertEqual(self.run_tx(bts.apply, self.manifest)["state"], "applied")

    def test_rollback_refuses_filters_changed_after_apply(self):
        self.run_tx(bts.apply, self.manifest)
        self.conn.execute("UPDATE user_settings SET value='0' WHERE user_id=1 AND key='topic_filter'")
        self.assert_rejected("filter changed after apply", bts.rollback)
        other = dict(deepcopy(self.manifest), provenance={"bundle_sha256": "x", "snapshot_sha256": "y"})
        self.assert_rejected("receipt conflicts", bts.rollback, other)
        self.assert_rejected("receipt conflicts", bts.apply, other)

    def test_guards_reject_without_any_write(self):
        cards = self.manifest["cards"]
        faults = {
            "unmanifested": "INSERT INTO words(id,type,german,russian,topic) VALUES('extra','nomen','X','x','PONS')",
            "missing": f"UPDATE words SET topic='3' WHERE id='{cards[1]['id']}'",
            "identity": f"UPDATE words SET german='changed' WHERE id='{cards[2]['id']}'",
            "old PONS registry": "UPDATE topics SET description='PONS' WHERE name='PONS'",
            "GSV registry": "UPDATE topics SET description='GSV' WHERE name='GSV'",
            "already exists": "INSERT INTO topics(name,description,level) VALUES('PONS_03','x','PONS')",
            "live dictionary job": "INSERT INTO vf_add_jobs(user_id,topic,status) VALUES(1,'PONS','running')",
            "malformed book filter": "UPDATE user_settings SET value='{bad' WHERE user_id=3 AND key='book_topic_filter_v2'",
            # Semantics changed (corrective 31): a common revision AHEAD of the v2
            # JSON is a normal state after CEFR/mode commands; only a v2 revision
            # in the FUTURE of the common one is inconsistent (user 2: v2 4 > 3).
            "ahead of common": "UPDATE user_settings SET value='3' WHERE user_id=2 AND key='filter_revision'",
            "invalid filter revision": "UPDATE user_settings SET value='x' WHERE user_id=7 AND key='filter_revision'",
            "receipt conflicts": f"INSERT INTO app_settings(key,value) VALUES('{bts.RECEIPT_KEY}','{{}}')",
            "requires schema": f"PRAGMA user_version={SCHEMA_VERSION - 1}",
        }
        for message, sql in faults.items():
            with self.subTest(fault=message):
                self.conn.execute("SAVEPOINT fault")
                self.conn.execute(sql)
                try:
                    with self.assertRaisesRegex(bts.TopicStructureError, message):
                        bts.apply(self.conn, self.manifest)
                finally:
                    self.conn.execute("ROLLBACK TO fault")
                    self.conn.execute("RELEASE fault")
                    self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        with self.assertRaisesRegex(bts.TopicStructureError, "caller-owned transaction"):
            bts.apply(self.conn, self.manifest)

    def command(self, uid, operation, level=None, token=None):
        """The real word-filter command path on this synthetic stand."""
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            state = wf.read_filter_state(uid, self.conn)
            wf.apply_filter_command(uid, state["revision"], operation, level, token, conn=self.conn)
            self.conn.execute("COMMIT")
        finally:
            self.conn.row_factory = None

    def filters(self, uid):
        rows = self.conn.execute("SELECT key,value FROM user_settings WHERE user_id=? AND key IN "
                                 "('filter_mode','topic_filter','book_topic_filter_v2','filter_revision')", (uid,))
        values = dict(rows)
        values["v2"] = json.loads(values.get("book_topic_filter_v2", "null"))
        return values

    def lagging_users(self):
        """Users whose stored v2 revision lags the common one via real commands."""
        self.conn.executemany("INSERT INTO users(user_id,status) VALUES(?, 'approved')", [(11,), (12,), (13,)])
        # 11: four books selected (PONS 'all'), then CEFR — the deployed dev shape.
        self.command(11, "cefr", "A1")
        for level in ("A1", "A2", "B1", "PONS"):
            self.command(11, "level", level)
        self.command(11, "cefr", "B1")
        # 12: explicit PONS topic (fresh topic tap), then CEFR.
        self.command(12, "cefr", "A1")
        self.command(12, "topic", "PONS", wf.topic_token("PONS"))
        self.command(12, "cefr", "B1")
        # 13: explicit PONS topic, then the compatibility mode API there and back.
        self.command(13, "cefr", "A1")
        self.command(13, "topic", "PONS", wf.topic_token("PONS"))
        with patch.object(wf, "get_db_path", return_value=str(self.db)):
            wf.set_filter_mode(13, wf.MODE_CEFR)
            wf.set_filter_mode(13, wf.MODE_TOPICS)

    def test_cefr_and_mode_commands_leave_a_supported_lagging_v2_revision(self):
        self.lagging_users()
        f11, f12, f13 = self.filters(11), self.filters(12), self.filters(13)
        self.assertEqual((f11["filter_mode"], f11["filter_revision"], f11["v2"]["revision"]), ("cefr", "6", 5))
        self.assertEqual(f11["v2"]["selected_levels"], ["A1", "A2", "B1", "PONS"])
        self.assertEqual((f12["filter_mode"], f12["filter_revision"], f12["v2"]["revision"]), ("cefr", "3", 2))
        self.assertEqual(f12["v2"]["topics_by_level"], {"A1": "all", "PONS": ["PONS"]})
        self.assertEqual((f13["filter_mode"], f13["filter_revision"], f13["v2"]["revision"]), ("topics", "4", 2))
        levels = self.conn.execute("SELECT user_id,level FROM user_levels WHERE user_id IN (11,12,13) ORDER BY 1,2").fetchall()
        raw11 = self.conn.execute("SELECT key,value FROM user_settings WHERE user_id=11 ORDER BY key").fetchall()
        progress = self.conn.execute("SELECT * FROM progress").fetchall()
        self.conn.execute("BEGIN")
        report = bts.plan(self.conn, self.manifest)
        self.conn.execute("ROLLBACK")
        touched = {(c["user_id"], c["key"]) for c in report["filters"]}
        self.assertFalse({k for u, k in touched if u == 11})
        self.assertEqual({k for u, k in touched if u == 12}, {"book_topic_filter_v2", "filter_revision"})
        self.assertEqual({k for u, k in touched if u == 13}, {"book_topic_filter_v2", "filter_revision"})
        self.run_tx(bts.apply, self.manifest)
        self.assertEqual(self.conn.execute("SELECT key,value FROM user_settings WHERE user_id=11 ORDER BY key").fetchall(), raw11)
        f12, f13 = self.filters(12), self.filters(13)
        self.assertEqual((f12["filter_mode"], f12["filter_revision"], f12["v2"]["revision"]), ("cefr", "4", 4))
        self.assertEqual(f12["v2"]["topics_by_level"], {"A1": "all", "PONS": PONS_KEYS})
        self.assertEqual((f13["filter_mode"], f13["filter_revision"], f13["v2"]["revision"]), ("topics", "5", 5))
        self.assertEqual(self.conn.execute("SELECT user_id,level FROM user_levels WHERE user_id IN (11,12,13) ORDER BY 1,2").fetchall(), levels)
        self.assertEqual(self.conn.execute("SELECT * FROM progress").fetchall(), progress)
        self.conn.row_factory = sqlite3.Row
        try:
            state = wf.read_filter_state(13, self.conn)
            self.assertTrue(state["valid"])
            self.assertEqual(wf.selected_topics(state, "PONS"), PONS_KEYS)
            self.assertEqual(wf.count_words_for_filter(13, self.conn), 1343)
            self.assertEqual(wf.read_filter_state(11, self.conn)["config"]["topics_by_level"]["PONS"], "all")
        finally:
            self.conn.row_factory = None
        # Real commands keep working on the migrated documents.
        self.command(12, "level", "PONS")
        self.assertEqual(self.filters(12)["filter_mode"], "topics")
        before_replay = self.image()
        self.assertEqual(self.run_tx(bts.apply, self.manifest)["state"], "already_applied")
        self.assertEqual(self.image(), before_replay)

    def test_lagging_revision_rollback_restores_choices_monotonically_and_reapplies(self):
        self.lagging_users()
        before = {uid: self.filters(uid) for uid in (11, 12, 13)}
        self.run_tx(bts.apply, self.manifest)
        after = {uid: self.filters(uid) for uid in (12, 13)}
        report = self.run_tx(bts.rollback, self.manifest)
        self.assertIn(12, report["filter_users"])
        self.assertNotIn(11, report["filter_users"])
        self.assertEqual(self.filters(11), before[11])
        for uid in (12, 13):
            now = self.filters(uid)
            revision = int(after[uid]["filter_revision"]) + 1
            self.assertEqual((now["filter_revision"], now["v2"]["revision"]), (str(revision), revision))
            self.assertEqual(now["v2"]["topics_by_level"], before[uid]["v2"]["topics_by_level"])
            self.assertEqual(now["filter_mode"], before[uid]["filter_mode"])
        self.assertEqual(self.run_tx(bts.apply, self.manifest)["state"], "applied")

    def test_future_v2_revision_ahead_of_common_rejected_atomically(self):
        self.lagging_users()
        self.conn.execute("UPDATE user_settings SET value='1' WHERE user_id=12 AND key='filter_revision'")
        self.assert_rejected("ahead of common")
        self.conn.execute("BEGIN")
        with self.assertRaisesRegex(bts.TopicStructureError, "ahead of common"):
            bts.plan(self.conn, self.manifest)
        self.conn.execute("ROLLBACK")

    def test_postflight_failure_rolls_back_every_write(self):
        with patch.object(bts, "_counts", return_value={}):
            self.assert_rejected("postflight mismatch")
        with patch.object(bts, "_protected", side_effect=["a", "b"]):
            self.assert_rejected("postflight mismatch")

    def test_cli_plan_default_apply_backup_replay_and_rollback(self):
        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--db", str(self.db), *args])
            return code, json.loads(out.getvalue() or err.getvalue())
        sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
        original = sha(self.db)
        code, report = run()
        self.assertEqual((code, report["state"], sha(self.db)), (0, "ready", original))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.main(["--db", str(self.db), "--apply"])
        backup = self.root / "pre-apply.db"
        code, report = run("--apply", "--backup", str(backup))
        self.assertEqual((code, report["state"], report["healthcheck"]["ok"]), (0, "applied", True))
        with sqlite3.connect(backup) as saved:
            self.assertEqual(saved.execute("SELECT COUNT(*) FROM words WHERE topic='PONS'").fetchone()[0], 1343)
        code, report = run("--apply", "--backup", str(self.root / "unused.db"))
        self.assertEqual((code, report["state"]), (0, "already_applied"))
        self.assertFalse((self.root / "unused.db").exists())
        code, report = run("--rollback", "--backup", str(self.root / "pre-rollback.db"))
        self.assertEqual((code, report["state"]), (0, "rolled_back"))
        code, report = run("--rollback", "--backup", str(self.root / "second.db"))
        self.assertEqual((code, report["state"]), (2, "error"))
        self.assertIn("no applied receipt", report["error"])


if __name__ == "__main__":
    unittest.main()
