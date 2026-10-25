"""Synthetic release-operation coverage; all fixtures are deliberately retained.

Run with GD_TEST_STAND and TMPDIR explicitly pointing inside the session stand.
No real databases, Telegram, network, or automatic fixture cleanup is used.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bot import storage
from bot.common import existing_user_filters as ef
from bot.common import word_filter as wf
from bot.common.db_safety import _connection_fingerprint
from scripts import backfill_existing_a1_filters as cli


def config(levels, choices, revision=4):
    return {"version": 2, "selected_levels": levels, "topics_by_level": choices,
            "browsing_level": levels[0], "revision": revision}


class ExistingUserFiltersTest(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("GD_TEST_STAND")
        if not root:
            raise RuntimeError("set GD_TEST_STAND to an explicit retained session stand")
        self.root = Path(tempfile.mkdtemp(prefix="existing-a1-", dir=root))
        self.db = self.root / "synthetic.db"
        storage.init_db(str(self.db))
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA user_version=21")
        self.addCleanup(self.conn.close)
        for uid in range(101, 109):
            storage.register_or_update_user(str(self.db), uid, "synthetic", "Fixture")
        self.conn.execute("UPDATE users SET status='approved'")
        self.conn.execute("UPDATE users SET status='pending' WHERE user_id=107")
        self.conn.execute("UPDATE users SET status='rejected' WHERE user_id=108")
        for uid in range(101, 109):
            self.conn.execute("DELETE FROM user_settings WHERE user_id=? AND key IN (?,?,?,?,?)",
                              (uid, *ef.FILTER_KEYS))
            self.put(uid, "interface_lang", "uk")
            self.put(uid, "dict_lang", "ru")
            self.put(uid, "session_retry_gap", "9")
            self.conn.execute("DELETE FROM user_levels WHERE user_id=?", (uid,))
            self.conn.executemany("INSERT INTO user_levels(user_id,level) VALUES(?,?)",
                                  [(uid, "A2"), (uid, "B1")])
        # All forms of old preferences must be forced once, including partial A1.
        self.put(101, "filter_mode", "cefr")
        self.put(101, "filter_revision", "4")
        self.put(102, "filter_mode", "topics")
        self.put(102, "active_book_level", "A2")
        self.put(102, "topic_filter", "02")
        self.put(102, "filter_revision", "4")
        self.put(103, "book_topic_filter_v2", json.dumps(config(
            ["A2", "B1"], {"A2": ["02"], "B1": "all"})))
        self.put(103, "filter_revision", "4")
        # 104 deliberately has none of the five settings.
        self.put(105, "book_topic_filter_v2", json.dumps(config(["A1"], {"A1": ["01"]})))
        self.put(105, "filter_revision", "4")
        self.put(106, "book_topic_filter_v2", '{"version":99,"broken":true}')
        self.put(106, "filter_revision", "4")
        self.conn.commit()
        for german, topic, cefr in (("Haus", "01", "B1"), ("Auto", "03", "A1"),
                                    ("Buch", "02", "A1"), ("Baum", "04", "A1")):
            storage.add_word(str(self.db), {"german": german, "type": "nomen", "article": "das",
                             "russian": "fixture", "topic": topic, "cefr_level": cefr})
        for topic, level in (("01", "A1"), ("03", "A1"), ("02", "A2"), ("04", "B1")):
            self.conn.execute("UPDATE topics SET level=? WHERE name=?", (level, topic))
        # Populate actual progress/history as well as unrelated preferences.
        word_id = self.conn.execute("SELECT id FROM words WHERE german='Haus'").fetchone()[0]
        self.conn.execute("""INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due)
                             VALUES(101,?,'ru_de',7,3,2.5,'2026-11-01')""", (word_id,))
        self.conn.execute("INSERT INTO app_settings(key,value) VALUES('unrelated','keep')")
        self.conn.commit()
        self.manifest = ef.prepare(self.conn)

    def put(self, uid, key, value):
        self.conn.execute("""INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)
                             ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value""",
                          (uid, key, value))

    def settings(self, uid):
        return dict(self.conn.execute("SELECT key,value FROM user_settings WHERE user_id=?", (uid,)))

    def apply(self, manifest=None):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            report = ef.apply(self.conn, manifest or self.manifest)
            self.conn.commit()
            return report
        except Exception:
            self.conn.rollback()
            raise

    def unchanged_scope(self):
        result = {}
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = [tuple(row) for row in self.conn.execute("SELECT * FROM " + quoted)]
            if name == "user_settings":
                rows = [row for row in rows if not (row[0] in range(101, 107) and row[1] in ef.FILTER_KEYS)]
            elif name == "app_settings":
                rows = [row for row in rows if row[0] != ef.RECEIPT_KEY]
            result[name] = sorted(rows, key=repr)
        result["schema"] = [tuple(row) for row in self.conn.execute("SELECT * FROM sqlite_master ORDER BY name")]
        return result

    def write_manifest(self, manifest=None, name="manifest.json"):
        path = self.root / name
        path.write_text(ef.canonical_json(manifest or self.manifest), encoding="utf-8")
        return path

    def test_force_every_old_filter_form_and_preserve_every_other_table(self):
        preserved = self.unchanged_scope()
        self.assertEqual([t["user_id"] for t in self.manifest["targets"]], list(range(101, 107)))
        self.assertEqual(self.apply()["state"], "applied")
        for target in self.manifest["targets"]:
            settings = self.settings(target["user_id"])
            self.assertEqual([settings[k] for k in ef.FILTER_KEYS[:3]], ["topics", "A1", "all"])
            document = wf.validate_book_config(json.loads(settings["book_topic_filter_v2"]))
            self.assertEqual(document["selected_levels"], ["A1"])
            self.assertEqual(document["topics_by_level"]["A1"], "all")
            self.assertEqual(document["browsing_level"], "A1")
            self.assertEqual(document["revision"], target["revision"] + 1)
            self.assertEqual(int(settings["filter_revision"]), document["revision"])
        self.assertEqual(json.loads(self.settings(103)["book_topic_filter_v2"])["topics_by_level"],
                         {"A1": "all", "A2": ["02"], "B1": "all"})
        self.assertEqual(self.unchanged_scope(), preserved)

    def test_prepare_is_deterministic_and_read_only_and_remembers_absence(self):
        baseline = _connection_fingerprint(self.conn)
        changes = self.conn.total_changes
        self.assertEqual(ef.prepare(self.conn), self.manifest)
        self.assertEqual(ef.check(self.conn, self.manifest)["state"], "ready")
        self.assertEqual(_connection_fingerprint(self.conn), baseline)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertTrue(all(not e["present"] and e["value"] is None
                            for e in self.manifest["targets"][3]["before"].values()))

    def test_schema20_approval_applies_on21_and_old_receipt_replays(self):
        self.conn.execute("PRAGMA user_version=20")
        manifest20 = ef.prepare(self.conn)
        self.assertEqual(manifest20, self.manifest)
        self.conn.execute("PRAGMA user_version=21")
        self.assertEqual(self.apply(manifest20)["state"], "applied")
        receipt = json.loads(self.conn.execute("SELECT value FROM app_settings WHERE key=?", (ef.RECEIPT_KEY,)).fetchone()[0])
        self.assertEqual(receipt["schema_version"], 20)
        fingerprint = _connection_fingerprint(self.conn)
        self.assertEqual(self.apply(manifest20)["state"], "already_applied")
        self.assertEqual(_connection_fingerprint(self.conn), fingerprint)

    def test_late_approved_user_and_later_approval_are_outside_frozen_cohort(self):
        storage.register_or_update_user(str(self.db), 901, "late", "Synthetic")
        self.conn.execute("UPDATE users SET status='approved' WHERE user_id IN (107,901)")
        self.put(901, "filter_mode", "cefr")
        self.put(107, "filter_mode", "cefr")
        self.conn.commit()
        before = {uid: self.settings(uid) for uid in (107, 108, 901)}
        self.apply()
        self.assertEqual({uid: self.settings(uid) for uid in before}, before)

    def test_status_drift_refuses_all_targets(self):
        self.conn.execute("UPDATE users SET status='rejected' WHERE user_id=106")
        self.conn.commit()
        baseline = _connection_fingerprint(self.conn)
        with self.assertRaisesRegex(ef.ExistingUserFilterError, "status"):
            self.apply()
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_each_of_five_before_settings_is_checked_including_new_presence(self):
        for key in ef.FILTER_KEYS:
            with self.subTest(key=key):
                self.conn.execute("BEGIN")
                self.put(104, key, "0" if key == "filter_revision" else "drift")
                baseline = _connection_fingerprint(self.conn)
                with self.assertRaisesRegex(ef.ExistingUserFilterError, "settings changed"):
                    ef.apply(self.conn, self.manifest)
                self.assertEqual(_connection_fingerprint(self.conn), baseline)
                self.conn.rollback()

    def test_progress_and_other_preferences_may_change_after_prepare(self):
        self.conn.execute("UPDATE progress SET interval=8")
        self.put(101, "dict_lang", "en")
        self.conn.commit()
        preserved = self.unchanged_scope()
        self.apply()
        self.assertEqual(self.unchanged_scope(), preserved)

    def test_invalid_revisions_fail_before_any_mutation(self):
        for value in ("-1", "1.5", "junk", ""):
            with self.subTest(value=value):
                self.conn.execute("BEGIN")
                self.put(106, "filter_revision", value)
                baseline = _connection_fingerprint(self.conn)
                with self.assertRaisesRegex(ef.ExistingUserFilterError, "revision"):
                    ef.prepare(self.conn)
                self.assertEqual(_connection_fingerprint(self.conn), baseline)
                self.conn.rollback()

    def test_old_callback_revision_is_stale(self):
        self.apply()
        self.conn.execute("BEGIN IMMEDIATE")
        baseline = _connection_fingerprint(self.conn)
        with self.assertRaisesRegex(wf.FilterCommandError, "stale"):
            wf.apply_filter_command(101, 4, "level", "A2", conn=self.conn)
        self.assertEqual(_connection_fingerprint(self.conn), baseline)
        self.conn.rollback()

    def test_repeat_after_actual_manual_a2_and_cefr_commands_and_restart(self):
        self.apply()
        self.conn.execute("BEGIN IMMEDIATE")
        wf.apply_filter_command(101, 5, "level", "A2", conn=self.conn)
        wf.apply_filter_command(101, 6, "level", "A1", conn=self.conn)
        wf.apply_filter_command(102, 5, "cefr", "A1", conn=self.conn)
        self.conn.commit()
        # A restart sees persisted UI commands through a new actual resolver connection.
        restarted = sqlite3.connect(self.db)
        restarted.row_factory = sqlite3.Row
        try:
            self.assertEqual(wf.resolve_quiz_filter(101, restarted).topics, ["02"])
            self.assertEqual(wf.resolve_quiz_filter(102, restarted).mode, "cefr")
        finally:
            restarted.close()
        self.conn.execute("UPDATE users SET status='rejected' WHERE user_id=101")
        self.put(102, "filter_revision", "corrupt-later")
        self.conn.execute("UPDATE topics SET level='B2' WHERE level='A1'")
        self.conn.commit()
        baseline = _connection_fingerprint(self.conn)
        changes = self.conn.total_changes
        self.assertEqual(ef.check(self.conn, self.manifest)["state"], "already_applied")
        self.assertEqual(self.apply()["state"], "already_applied")
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(_connection_fingerprint(self.conn), baseline)
        with self.assertRaisesRegex(ef.ExistingUserFilterError, "already applied"):
            ef.prepare(self.conn)

    def test_a1_resolves_by_topic_and_new_a1_topic_joins_automatically(self):
        self.apply()
        actual = wf.resolve_quiz_filter(101, self.conn)
        self.assertEqual(actual.topics, ["01", "03"])
        picked = storage.learning_pick_new_words(str(self.db), user_id=104, limit=100,
                                                topics=actual.topics, cefr_levels=actual.cefr_levels)
        self.assertEqual({word["german"] for word in picked}, {"Haus", "Auto"})
        self.assertEqual(wf.count_words_for_filter(101, self.conn), len(picked))
        storage.add_word(str(self.db), {"german": "Neu", "type": "nomen", "article": "das",
                         "russian": "fixture", "topic": "05", "cefr_level": "C2"})
        self.conn.execute("UPDATE topics SET level='A1' WHERE name='05'")
        self.conn.commit()
        self.assertEqual(wf.resolve_quiz_filter(101, self.conn).topics, ["01", "03", "05"])
        self.assertEqual(wf.count_words_for_filter(101, self.conn), 3)

    def test_savepoint_rolls_back_partial_settings_and_keeps_outer_work(self):
        self.conn.execute("""CREATE TRIGGER fail_settings BEFORE INSERT ON user_settings
            WHEN NEW.user_id=102 AND NEW.key='active_book_level'
            BEGIN SELECT RAISE(ABORT,'synthetic settings failure'); END""")
        self.conn.commit()
        baseline = _connection_fingerprint(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("INSERT INTO app_settings(key,value) VALUES('outer','keep')")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "settings failure"):
            ef.apply(self.conn, self.manifest)
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT value FROM app_settings WHERE key='outer'").fetchone()[0], "keep")
        self.conn.execute("DELETE FROM app_settings WHERE key='outer'")
        self.conn.commit()
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_receipt_failure_rolls_back_every_target_even_if_caller_commits(self):
        self.conn.execute(f"""CREATE TRIGGER fail_receipt BEFORE INSERT ON app_settings
            WHEN NEW.key='{ef.RECEIPT_KEY}' BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END""")
        self.conn.commit()
        baseline = _connection_fingerprint(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt failure"):
            ef.apply(self.conn, self.manifest)
        self.conn.commit()
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_apply_never_commits_the_callers_transaction(self):
        baseline = _connection_fingerprint(self.conn)
        with self.assertRaisesRegex(ef.ExistingUserFilterError, "transaction"):
            ef.apply(self.conn, self.manifest)
        self.conn.execute("BEGIN IMMEDIATE")
        ef.apply(self.conn, self.manifest)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_schema_versions_and_empty_a1_and_empty_cohort_refuse_without_writes(self):
        for version in (0, 19, 22):
            self.conn.execute(f"PRAGMA user_version={version}")
            baseline = _connection_fingerprint(self.conn)
            with self.assertRaises(ef.ExistingUserFilterError):
                ef.prepare(self.conn)
            with self.assertRaises(ef.ExistingUserFilterError):
                self.apply()
            self.assertEqual(_connection_fingerprint(self.conn), baseline)
        self.conn.execute("PRAGMA user_version=20")
        self.conn.execute("BEGIN")
        self.conn.execute("UPDATE topics SET level='' WHERE level='A1'")
        with self.assertRaisesRegex(ef.ExistingUserFilterError, "registry"):
            ef.prepare(self.conn)
        self.conn.rollback()
        self.conn.execute("UPDATE users SET status='rejected'")
        self.conn.commit()
        with self.assertRaisesRegex(ef.ExistingUserFilterError, "cohort"):
            ef.prepare(self.conn)

    def test_manifest_tampering_and_noncanonical_ids_are_rejected(self):
        variants = []
        changed = deepcopy(self.manifest)
        changed["targets"][0]["before"]["filter_mode"]["value"] = "topics"
        variants.append(changed)
        for change in (lambda m: m.update(extra=True),
                       lambda m: m["targets"].reverse(),
                       lambda m: m["targets"].append(m["targets"][0]),
                       lambda m: m["targets"][0].update(user_id=True),
                       lambda m: m["targets"][0].update(revision=99),
                       lambda m: m["targets"][0]["before"].pop("topic_filter"),
                       lambda m: m.update(version=True),
                       lambda m: m.update(schema_version=21)):
            candidate = deepcopy(self.manifest)
            change(candidate)
            candidate["digest"] = ef._digest({k: v for k, v in candidate.items() if k != "digest"})
            variants.append(candidate)
        baseline = _connection_fingerprint(self.conn)
        for candidate in variants:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ef.ExistingUserFilterError):
                    ef.check(self.conn, candidate)
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_corrupt_and_conflicting_receipts_are_never_ignored(self):
        for raw in ("null", "{}", "not-json", '{"version":1,"version":1}', 'NaN'):
            self.conn.execute("BEGIN")
            self.conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (ef.RECEIPT_KEY, raw))
            baseline = _connection_fingerprint(self.conn)
            with self.assertRaises(ef.ExistingUserFilterError):
                ef.check(self.conn, self.manifest)
            self.assertEqual(_connection_fingerprint(self.conn), baseline)
            self.conn.rollback()
        self.apply()
        altered = deepcopy(self.manifest)
        altered["targets"] = altered["targets"][:-1]
        altered["digest"] = ef._digest({k: v for k, v in altered.items() if k != "digest"})
        with self.assertRaisesRegex(ef.ExistingUserFilterError, "conflicts"):
            self.apply(altered)

    def test_cli_prepare_check_are_ro_query_only_and_no_overwrite(self):
        baseline = _connection_fingerprint(self.conn)
        manifest_path = self.root / "prepared.json"
        actual_open = cli.open_database
        observations = []

        def instrument(path, *, readonly=True):
            self.assertTrue(readonly)
            conn = actual_open(path, readonly=readonly)
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE app_settings SET value='forbidden'")
            observations.append(True)
            return conn

        with patch.object(cli, "open_database", side_effect=instrument):
            self.assertEqual(cli.prepare_file(self.db, manifest_path)["state"], "prepared")
            self.assertEqual(cli.check_file(self.db, manifest_path)["state"], "ready")
        saved = manifest_path.read_bytes()
        with self.assertRaises(FileExistsError):
            cli.prepare_file(self.db, manifest_path)
        self.assertEqual(manifest_path.read_bytes(), saved)
        self.assertEqual(len(observations), 2)
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_cli_missing_database_is_not_created_in_any_mode(self):
        missing = self.root / "missing.db"
        manifest_path = self.write_manifest()
        for action in (lambda: cli.prepare_file(missing, self.root / "missing.json"),
                       lambda: cli.check_file(missing, manifest_path),
                       lambda: cli.apply_file(missing, manifest_path, self.root / "missing-backup.db")):
            with self.assertRaises(FileNotFoundError):
                action()
            self.assertFalse(missing.exists())
        self.assertFalse((self.root / "missing-backup.db").exists())

    def test_cli_wal_backup_matches_actual_pre_dml_state_and_repeat_has_no_backup(self):
        # Keep a WAL writer open so committed content really remains in its sidecar.
        self.assertEqual(self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.conn.execute("UPDATE progress SET interval=23")
        self.conn.commit()
        self.assertGreater(Path(str(self.db) + "-wal").stat().st_size, 0)
        baseline = _connection_fingerprint(self.conn)
        preserved = self.unchanged_scope()
        manifest_path = self.write_manifest()
        backup = self.root / "backup.db"
        report = cli.apply_file(self.db, manifest_path, backup)
        self.assertEqual(report["state"], "applied")
        saved = sqlite3.connect(backup)
        try:
            self.assertEqual(_connection_fingerprint(saved), baseline)
            self.assertEqual(saved.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(saved.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(saved.execute("SELECT interval FROM progress").fetchone()[0], 23)
            self.assertIsNone(saved.execute("SELECT value FROM app_settings WHERE key=?", (ef.RECEIPT_KEY,)).fetchone())
        finally:
            saved.close()
        self.assertEqual(report["backup"]["sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        self.assertEqual(self.unchanged_scope(), preserved)
        self.put(101, "filter_mode", "cefr")
        self.conn.commit()
        after = _connection_fingerprint(self.conn)
        second = self.root / "must-not-exist.db"
        with patch.object(cli, "verified_backup", side_effect=AssertionError("repeat tried backup")):
            self.assertEqual(cli.apply_file(self.db, manifest_path, second)["state"], "already_applied")
        self.assertFalse(second.exists())
        self.assertEqual(_connection_fingerprint(self.conn), after)

    def test_cli_backup_failure_and_preflight_failure_leave_database_unchanged(self):
        manifest_path = self.write_manifest()
        baseline = _connection_fingerprint(self.conn)
        with patch.object(cli, "verified_backup", side_effect=OSError("synthetic backup failure")):
            with self.assertRaisesRegex(OSError, "backup failure"):
                cli.apply_file(self.db, manifest_path, self.root / "failed.db")
        self.assertEqual(_connection_fingerprint(self.conn), baseline)
        occupied = self.root / "occupied.db"
        occupied.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            cli.apply_file(self.db, manifest_path, occupied)
        self.assertEqual(occupied.read_bytes(), b"do not overwrite")
        self.assertEqual(_connection_fingerprint(self.conn), baseline)
        self.put(106, "filter_revision", "changed")
        self.conn.commit()
        with patch.object(cli, "verified_backup", side_effect=AssertionError("preflight tried backup")):
            with self.assertRaises(ef.ExistingUserFilterError):
                cli.apply_file(self.db, manifest_path, self.root / "no-backup.db")

    def test_cli_real_backup_health_failure_blocks_all_writes(self):
        manifest_path = self.write_manifest()
        # Real invalid FK in a synthetic DB must be detected in the snapshot.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("""INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due)
            VALUES(101,'missing-word','de_ru',1,1,2.5,'2026-11-01')""")
        self.conn.commit()
        baseline = _connection_fingerprint(self.conn)
        with self.assertRaisesRegex(ValueError, "foreign-key"):
            cli.apply_file(self.db, manifest_path, self.root / "bad-fk-backup.db")
        self.assertEqual(_connection_fingerprint(self.conn), baseline)

    def test_cli_subprocess_is_explicit_standalone_and_has_no_runtime_config_import(self):
        script = Path(cli.__file__)
        manifest_path = self.root / "subprocess.json"
        result = subprocess.run([sys.executable, str(script), "prepare", "--db", str(self.db),
                                 "--manifest", str(manifest_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "prepared")
        result = subprocess.run([sys.executable, str(script), "check", "--db", str(self.db),
                                 "--manifest", str(manifest_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "ready")
        result = subprocess.run([sys.executable, "-c",
            "import scripts.backfill_existing_a1_filters,sys; "
            "assert 'bot.storage' not in sys.modules; assert 'bot.app_config' not in sys.modules; "
            "assert 'bot.main' not in sys.modules"], cwd=script.parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(self.conn.execute("SELECT value FROM app_settings WHERE key=?", (ef.RECEIPT_KEY,)).fetchone())


if __name__ == "__main__":
    unittest.main()
