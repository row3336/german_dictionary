"""Synthetic schema19 acceptance scenarios DB01..DB13; no live-data access."""

import copy
import json
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import unittest
from unittest.mock import patch

from bot import storage
from bot.common import db_safety, import_package
from bot.common.verb_construction import (DuplicateIdentity, InvalidClassification,
                                          InvalidForms, LegacyIdentityCollision)
from bot.conjugation import repository as conjugation
from bot.study import answers, generations, repair, repository as repo, schema_v19

STAND = Path("/Users/wf/py/_agent_work/888_german_dictionary/k12_impl_20261023")


def block(case="akkusativ"):
    pronouns = ("mich", "dich", "sich", "uns", "euch", "sich") if case == "akkusativ" else ("mir", "dir", "sich", "uns", "euch", "sich")
    return "\n".join(f"{p} {v} {r}" for p, v, r in zip(
        ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie"),
        ("wasche", "wäschst", "wäscht", "waschen", "wascht", "waschen"), pronouns))


def verb(wid, case="none", infinitive=None):
    inf = infinitive or ("waschen" if case == "none" else "sich waschen")
    return dict(id=wid, type="verb", infinitive=inf, german=inf, reflexive_case=case,
                russian="мыть", ukrainian="мити", english="wash", is_separable=0,
                conjugation_praesens=block(case) if case != "none" else "",
                conjugation_praeteritum=block(case) if case != "none" else "")


def new_db():
    STAND.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="db19-", dir=STAND)) / "synthetic.sqlite"
    storage.init_db(path)
    with repo.transaction(path) as conn:
        conn.execute("INSERT INTO users(user_id,status) VALUES (1,'approved'),(2,'approved')")
    return path


def seed(db):
    storage.add_word(db, verb("a", "akkusativ"))
    storage.add_word(db, verb("b", "dativ"))
    with repo.transaction(db) as conn:
        conn.execute("INSERT INTO learning_batches(id,user_id) VALUES ('batch',1)")
        for wid in ("a", "b"):
            conn.execute("""INSERT INTO learning_items(user_id,word_id,batch_id,next_due,preview_done,
                first_due_pending) VALUES (1,?,'batch','2026-10-23T12:00:00',1,1)""", (wid,))
            repo.set_progress(conn, 1, wid, "ru_de", dict(interval=9,repetitions=3,ease=2.6,due="2026-10-23"))
            repo.append_event(conn, 1, wid, "ru_de", True)


def manifest(db, users=(1,)):
    with repo.transaction(db) as conn:
        old = dict(conn.execute("SELECT * FROM words WHERE id='a'").fetchone())
        result = {"schema": "gd-study-repair/v1", "operation_id": "synthetic-repair",
                  "reset_profile": "relearn_word", "approvals": {"owner": True, "linguistic": True},
                  "baseline_fingerprint": db_safety._connection_fingerprint(conn),
                  "targets": [{"word_id": "a", "old_identity": ["verb", "sich waschen", "akkusativ"],
                    "new_identity": ["verb", "sich waschen", "akkusativ"],
                    "replacement": {"russian": "новый перевод"}, "expected_before_sha256": repair.digest(old),
                    "users": [{"user_id": uid, "expected_generation": generations.generation_for(conn, uid, "a"),
                               "expected_rows_sha256": repair.digest(repair.state_rows(conn, uid, "a"))} for uid in users]}]}
    result["digest"] = repair.manifest_digest(result)
    return result


def import_fixture(conn, fields_list):
    package = {"schema": import_package.SCHEMA, "baseline_fingerprint": db_safety._connection_fingerprint(conn), "entries": []}
    for original in fields_list:
        fields = dict(original)
        cases = [fields.get("reflexive_case")] * 2
        if fields["type"] == "praeposition":
            cases = ["akkusativ", "akkusativ", "dativ", "dativ"] if fields.get("case_type") == "wechsel" else [fields.get("case_type")] * 2
        examples = [{"text": f"Synthetic example {i}", "sense_id": "one", "case": case} for i,case in enumerate(cases)]
        fields["example"] = "\n".join(e["text"] for e in examples)
        key = list(import_package.identity(fields)) if fields["type"] == "verb" else [fields["type"], fields.get("german"), fields.get("preposition_type"), fields.get("case_type")]
        package["entries"].append({"id": import_package.stable_id(fields), "identity": key, "fields": fields,
            "source": {"provenance": "synthetic identity fixture", "sha256": "c" * 64},
            "senses": [{"id": "one"}], "examples": examples})
    package["digest"] = import_package.package_digest(package)
    return package


def old_schema(db, version=18):
    with repo.transaction(db) as conn:
        for name in ("words_verb_identity", "words_verb_lookup", "quiz_events_generation"):
            conn.execute(f"DROP INDEX {name}")
        for name in ("words_reflexive_type_insert", "words_reflexive_type_update"):
            conn.execute(f"DROP TRIGGER {name}")
        for table, column in (("words", "reflexive_case"), ("quiz_events", "study_generation"),
                              ("conjugation_passes", "prompt_snapshot_json"), ("conjugation_admissions", "study_generation")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        for table in ("study_word_generations", "study_reset_operations"):
            conn.execute(f"DROP TABLE {table}")
        if version < 18:
            for table in ("conjugation_answers", "conjugation_passes", "conjugation_blocks", "conjugation_admissions", "study_actions", "study_deliveries"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("ALTER TABLE learning_items DROP COLUMN recovery_remaining")
            conn.execute("ALTER TABLE learning_items DROP COLUMN first_due_pending")
        conn.execute(f"PRAGMA user_version={version}")


class ReflexiveDB19Test(unittest.TestCase):
    def setUp(self):
        self.db = new_db()

    def test_DB01_migration_chain_fresh_repeat_future_and_health(self):
        db = self.db
        for source in (0, 14, 15, 16, 17, 18):
            old_schema(db, source)
            report = db_safety._apply_migration_dispatcher(db)
            assert report["to_version"] == 20
            assert report["healthcheck"]["ok"]
            assert not db_safety._apply_migration_dispatcher(db)["migrated"]
            with repo.transaction(db) as conn:
                conn.execute("PRAGMA user_version=21")
            with self.assertRaisesRegex(RuntimeError, "newer"):
                storage.init_db(db)
            with self.assertRaisesRegex(RuntimeError, "newer"):
                db_safety._apply_migration_dispatcher(db)

    def test_DB01_failure_rollback_and_backup(self):
        db = self.db
        class PatchAdapter:
            def setattr(inner, target, key, value):
                manager = patch.object(target, key, value)
                manager.start()
                self.addCleanup(manager.stop)
        monkeypatch = PatchAdapter()
        old_schema(db)
        before = db_safety.database_fingerprint(db)
        real = schema_v19.migrate
        def fail(conn):
            real(conn)
            raise RuntimeError("injected19")
        monkeypatch.setattr(schema_v19, "migrate", fail)
        with self.assertRaisesRegex(RuntimeError, "injected19"):
            db_safety._apply_migration_dispatcher(db)
        assert db_safety.database_fingerprint(db) == before
        monkeypatch.setattr(schema_v19, "migrate", real)
        backup = db.parent / "online-backup.sqlite"
        db_safety.backup_database(db, backup)
        assert db_safety.database_fingerprint(backup) == before
        with self.assertRaises(db_safety.DestructiveMigrationRequired):
            storage.load_words(db)

    def test_DB01_public_dry_run_and_safe_apply_keep_online_backup(self):
        db = self.db
        old_schema(db)
        fingerprint = db_safety.database_fingerprint(db)
        with patch.object(tempfile, "tempdir", str(db.parent)):
            dry = db_safety.dry_run_migration(db)
            assert dry["ok"] and dry["to_version"] == 19
            assert db_safety.database_fingerprint(db) == fingerprint
            result = db_safety.apply_migrations_safely(db, db.parent / "backups")
        assert result["healthcheck"]["ok"]
        assert result["backup"] and db_safety.database_fingerprint(result["backup"]) == fingerprint
        assert not db_safety.apply_migrations_safely(db, db.parent / "backups")["migrated"]

    def test_DB02_identity_atomic_add_update_and_unicode(self):
        db = self.db
        for wid, case in (("ordinary", "none"), ("a", "akkusativ"), ("b", "dativ")):
            storage.add_word(db, verb(wid, case))
        assert len(storage.find_verb_identity_candidates(db, "  SICH   WASCHEN  ")) == 2
        with self.assertRaises(DuplicateIdentity):
            storage.add_word(db, verb("duplicate", "akkusativ"))
        with self.assertRaises(DuplicateIdentity):
            storage.update_word_fields(db, "b", {"reflexive_case": "akkusativ", "conjugation_praesens": block(), "conjugation_praeteritum": block()})
        assert storage.get_word_by_id(db, "b")["reflexive_case"] == "dativ"
        entry = verb("unicode", "none", "  ÄUSSERN  ")
        assert storage.add_word(db, entry)["infinitive"] == "äussern"

    def test_DB03_legacy_null_and_invalid_classification(self):
        db = self.db
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO words(id,type,infinitive,german) VALUES ('legacy','verb','bedanken','bedanken')")
        assert storage.get_word_by_id(db, "legacy")["reflexive_case"] is None
        for value in (None, "", "wechsel", "genitiv", "garbage"):
            with self.assertRaises(InvalidClassification):
                storage.add_word(db, {**verb("bad"), "reflexive_case": value})
        with self.assertRaises(LegacyIdentityCollision):
            storage.add_word(db, verb("duplicate", "none", "bedanken"))
        with self.assertRaises(InvalidForms):
            storage.add_word(db, {**verb("wrong", "akkusativ"), "conjugation_praesens": block("dativ")})
        storage.add_word(db, dict(id="prep",type="praeposition",german="in",case_type="wechsel"))
        assert storage.get_word_by_id(db, "prep")["case_type"] == "wechsel"
        with repo.transaction(db) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE words SET reflexive_case='none' WHERE id='prep'")

    def test_DB04_DTO_and_both_reference_tables_personal_overlay(self):
        db = self.db
        seed(db)
        with repo.transaction(db) as conn:
            conn.execute("UPDATE learning_items SET preview_done=1,first_due_pending=0,next_due='2000-01-01' WHERE user_id=1")
        storage.set_user_word_media(db, 2, "a", example="personal example")
        storage.set_user_word_translation(db, 2, "a", "ru", "личное")
        storage.add_word_flag(db, "a", 1, "synthetic flag")
        collections = {
            "load_words": storage.load_words(db),
            "get_word_by_id": [storage.get_word_by_id(db, "a")],
            "list_all_words": storage.list_all_words(db),
            "list_words_by_topic": storage.list_words_by_topic(db, "1"),
            "learning_pick_new_words": storage.learning_pick_new_words(db, 2, 20),
            "list_learning_queue_words": storage.list_learning_queue_words(db, 2),
            "list_synonym_candidates_by_ru": storage.list_synonym_candidates_by_ru(db, "a"),
            "list_pending_word_flags": storage.list_pending_word_flags(db),
            "find_word_by_value": [storage.find_word_by_value(db, "a")],
            "find_existing_word": [storage.find_existing_word(db, "sich waschen")],
            "search_word": [storage.search_word(db, "a")],
            "personal_overlay": [storage.apply_user_word_media(db, 2, storage.get_word_by_id(db, "a"))],
            "list_learning_active_words": storage.list_learning_active_words(db, 1),
            "learning_due_entries": storage.learning_due_entries(db, 1),
        }
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES ('preview-batch',2)")
            conn.execute("""INSERT INTO learning_items(user_id,word_id,batch_id,preview_done,first_due_pending,next_due)
                VALUES (2,'a','preview-batch',0,1,'2000-01-01')""")
        collections["learning_preview_entry"] = [storage.learning_preview_entry(db, 2)]
        assert len(collections) == 15
        assert {e["id"] for e in collections["learning_due_entries"]} == {"a", "b"}
        assert {e["id"] for e in collections["list_synonym_candidates_by_ru"]} == {"b"}
        assert {e["id"] for e in collections["list_learning_queue_words"]} == {"a", "b"}
        assert {e["id"] for e in collections["list_pending_word_flags"]} == {"a"}
        for surface, entries in collections.items():
            with self.subTest(surface=surface):
                assert entries and all(entry is not None for entry in entries), surface
                for entry in entries:
                    assert entry["reflexive_case"] in ("akkusativ", "dativ")
                    assert entry["conjugation_praesens"] == block(entry["reflexive_case"]), surface
                    assert entry["conjugation_praeteritum"] == block(entry["reflexive_case"]), surface
                    assert entry["is_separable"] == 0, surface
        overlaid = collections["personal_overlay"][0]
        assert overlaid["example"] == "personal example" and overlaid["reflexive_case"] == "akkusativ"

    def test_DB05_concurrent_writers_and_migration_collision(self):
        db = self.db
        def add(index):
            try:
                storage.add_word(db, verb(str(index), "akkusativ"))
                return "ok"
            except (DuplicateIdentity, sqlite3.IntegrityError):
                return "duplicate"
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(add, range(2))) == ["duplicate", "ok"]
        old_schema(db)
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO words(id,type,infinitive) VALUES ('collision','verb',' SICH   WASCHEN ')")
        before = db_safety.database_fingerprint(db)
        with self.assertRaisesRegex(ValueError, "preflight"):
            db_safety._apply_migration_dispatcher(db)
        assert db_safety.database_fingerprint(db) == before

    def test_DB06_legacy_pass_and_new_prompt_freeze(self):
        db = self.db
        storage.add_word(db, verb("a", "akkusativ"))
        with repo.transaction(db) as conn:
            repo.set_progress(conn, 1, "a", "ru_de", dict(interval=1,repetitions=2,ease=2.5,due="2026-01-01"))
            conjugation.sync_blocks(conn,1,"ru_de")
            conn.execute("UPDATE conjugation_blocks SET phase='test',due='2026-01-01'")
            current = conjugation.select_pass(conn,1,"ru_de",date(2026,10,23),"2026-10-23")
            pid, expected = current["id"], current["expected_forms_json"]
        old_schema(db)
        report = db_safety._apply_migration_dispatcher(db)
        assert report["steps"]["reflexive_v19"]["legacy_pass_diagnostics"] == []
        with repo.transaction(db) as conn:
            current = conjugation.pass_row(conn,1,pid)
            prompt = json.loads(current["prompt_snapshot_json"])
            assert prompt == dict(v=1,word="sich waschen",reflexive_case=None,study_generation=0,legacy=True)
            assert current["expected_forms_json"] == expected
            conn.execute("UPDATE words SET german='changed' WHERE id='a'")
            assert conjugation.question(conn,current)["word"] == "sich waschen"

    def test_DB06_invalid_legacy_parent_closes_without_grade_or_history_loss(self):
        variants = (("nonverb", "type='phrase'"), ("separable", "is_separable=1"),
                    ("unknown_separability", "is_separable=NULL"),
                    ("missing_label", "infinitive='',german=''"))
        for label, invalid_parent in variants:
            with self.subTest(parent=label):
                db = new_db()
                storage.add_word(db, verb("a", "akkusativ"))
                with repo.transaction(db) as conn:
                    repo.set_progress(conn, 1, "a", "ru_de", dict(interval=9,repetitions=3,ease=2.6,due="2026-10-23"))
                    repo.append_event(conn, 1, "a", "ru_de", True)
                    conjugation.sync_blocks(conn, 1, "ru_de")
                    conn.execute("UPDATE conjugation_blocks SET phase='test',due='2026-01-01' WHERE word_id='a'")
                    current = conjugation.select_pass(conn, 1, "ru_de", date(2026,10,23), "2026-10-23")
                    pid = current["id"]
                    pronoun = json.loads(current["pronoun_order_json"])[0]
                    conn.execute("""INSERT INTO conjugation_answers
                        (user_id,pass_id,pronoun_group,ordinal,answer_text,is_correct,original_text,
                         original_correct,answered_at,answer_day,correction_session_id,action_key)
                        VALUES (1,?,?,1,'legacy answer',0,'legacy answer',0,
                                '2026-10-22','2026-10-22','legacy flow','legacy action')""", (pid, pronoun))
                    conn.execute("UPDATE conjugation_passes SET applied_result='wrong',result_day='2026-10-22' WHERE id=?", (pid,))
                    completed = dict(conn.execute("SELECT * FROM conjugation_passes WHERE id=?", (pid,)).fetchone())
                    completed.update(id="completed-history", status="completed", completed_at="2026-10-22")
                    columns = list(completed)
                    conn.execute("INSERT INTO conjugation_passes (" + ",".join(columns) + ") VALUES (" +
                                 ",".join("?" for _ in columns) + ")", [completed[k] for k in columns])
                old_schema(db)
                preserved = ("progress", "quiz_events", "learning_items", "conjugation_blocks", "conjugation_answers")
                with repo.transaction(db) as conn:
                    conn.execute("UPDATE words SET " + invalid_parent + " WHERE id='a'")
                    before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                              for table in preserved}
                    active_before = dict(conn.execute("SELECT * FROM conjugation_passes WHERE id=?", (pid,)).fetchone())
                    completed_before = dict(conn.execute("SELECT * FROM conjugation_passes WHERE id='completed-history'").fetchone())
                report = db_safety._apply_migration_dispatcher(db)
                assert report["steps"]["reflexive_v19"]["legacy_pass_diagnostics"] == [
                    {"pass_id": pid, "outcome": "invalid_legacy_parent"}]
                with repo.transaction(db) as conn:
                    active = dict(conn.execute("SELECT * FROM conjugation_passes WHERE id=?", (pid,)).fetchone())
                    assert active.pop("prompt_snapshot_json") is None
                    assert active == {**active_before, "status": "reset", "revision": active_before["revision"] + 1}
                    completed = dict(conn.execute("SELECT * FROM conjugation_passes WHERE id='completed-history'").fetchone())
                    assert completed.pop("prompt_snapshot_json") is None
                    assert completed == completed_before
                    for table, old_rows in before.items():
                        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                        if table == "quiz_events":
                            for row in rows:
                                assert row.pop("study_generation") == 0
                        assert rows == old_rows, table
                assert report["healthcheck"]["ok"]

    def test_DB07_composite_undo_after_reset_preserves_B_and_legacy(self):
        db = self.db
        seed(db)
        with repo.transaction(db) as conn:
            snapshot = repo.capture_snapshot(conn,1,"a",["ru_de"],affected_learning_ids=["b"])
            eid = repo.append_event(conn,1,"a","ru_de",False)
            repo.record_created_events(conn,snapshot,[eid])
            conn.execute("UPDATE learning_items SET step_index=2 WHERE word_id='b'")
        repair.apply_reset(db,manifest(db),integration_verified=True)
        with repo.transaction(db) as conn:
            assert repo.restore_snapshot(conn,snapshot)
            assert snapshot["restore_outcomes"] == {"a":"stale_generation","b":"restored"}
            assert conn.execute("SELECT step_index FROM learning_items WHERE word_id='b'").fetchone()[0] == 0
            assert conn.execute("SELECT 1 FROM progress WHERE word_id='a'").fetchone() is None
            assert conn.execute("SELECT 1 FROM quiz_events WHERE id=?",(eid,)).fetchone()
            legacy = {k:v for k,v in snapshot.items() if k not in ("generations","created_event_owners")}
            legacy["version"] = 2
            assert repo.restore_snapshot(conn,legacy)
            assert generations.generation_for(conn,1,"a") == 1
        assert not storage.restore_user_word_state(db,{"user_id":1,"word_id":"a","modes":["ru_de"],"progress":{"ru_de":None},"event_counts":{"ru_de":0}})

    def test_DB08_manifest_replay_conflicts_shared_users_and_gate(self):
        db = self.db
        seed(db)
        with repo.transaction(db) as conn:
            repo.set_progress(conn,2,"a","ru_de",dict(interval=5,repetitions=2,ease=2.5,due="2026-10-23"))
        partial = manifest(db)
        with repo.transaction(db) as conn:
            with self.assertRaisesRegex(ValueError, "omits"):
                repair.preflight(conn,partial)
        full = manifest(db,(1,2))
        with self.assertRaises(PermissionError):
            repair.apply_reset(db,full)
        repair.apply_reset(db,full,integration_verified=True)
        storage.set_progress(db,1,"a","ru_de",dict(interval=20,repetitions=5,ease=2.5,due="2026-11-23"))
        assert repair.apply_reset(db,full,integration_verified=True)["replayed"]
        assert storage.get_progress(db,1,"a","ru_de")["interval"] == 20
        bad = copy.deepcopy(full)
        bad["reset_profile"] = "other"
        bad["digest"] = repair.manifest_digest(bad)
        with self.assertRaisesRegex(ValueError, "conflict"):
            repair.apply_reset(db,bad,integration_verified=True)

    def test_DB09_old_admission_does_not_requalify_and_history_retained(self):
        db = self.db
        seed(db)
        with repo.transaction(db) as conn:
            conjugation.sync_blocks(conn,1,"ru_de")
            assert conjugation.current_admission(conn,1,"ru_de","a")
        repair.apply_reset(db,manifest(db),integration_verified=True)
        with repo.transaction(db) as conn:
            assert not conjugation.current_admission(conn,1,"ru_de","a")
            assert conjugation.current_admission(conn,1,"ru_de","b")
            assert not [b for b in conjugation.blocks(conn,1,"ru_de") if b["word_id"] == "a"]
            repo.set_progress(conn,1,"a","ru_de",dict(interval=1,repetitions=2,ease=2.5,due="2026-10-24"))
            assert conjugation.current_admission(conn,1,"ru_de","a")
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_DB10_repair_failure_rolls_back_and_preserves_media_B(self):
        db = self.db
        class PatchAdapter:
            def setattr(inner, target, key, value):
                manager = patch.object(target, key, value)
                manager.start()
                self.addCleanup(manager.stop)
        monkeypatch = PatchAdapter()
        seed(db)
        storage.set_user_word_media(db,1,"a",image="personal-image",example="personal-example")
        man = manifest(db)
        before = db_safety.database_fingerprint(db)
        real = repair.advance_generation
        def fail(*args):
            real(*args)
            raise RuntimeError("injected repair")
        monkeypatch.setattr(repair,"advance_generation",fail)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            repair.apply_reset(db,man,integration_verified=True)
        assert db_safety.database_fingerprint(db) == before
        monkeypatch.setattr(repair,"advance_generation",real)
        with repo.transaction(db) as conn:
            b = repair.state_rows(conn,1,"b")
            media = list(conn.execute("SELECT * FROM user_word_media"))[0]
        repair.apply_reset(db,man,integration_verified=True)
        with repo.transaction(db) as conn:
            assert repair.state_rows(conn,1,"b") == b
            assert tuple(list(conn.execute("SELECT * FROM user_word_media"))[0]) == tuple(media)
        assert db_safety.run_healthcheck(db)["ok"]

    def test_DB11_events_current_generation_and_eligibility(self):
        db = self.db
        seed(db)
        repair.apply_reset(db,manifest(db),integration_verified=True)
        assert "a" in {e["id"] for e in storage.learning_pick_new_words(db,1,20)}
        assert "a" not in {e["id"] for e in storage.select_due(db,1,"ru_de")}
        storage.log_quiz_event(db,1,"a","ru_de",False)
        with repo.transaction(db) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM quiz_events WHERE word_id='a' ORDER BY id")]
            assert [r["study_generation"] for r in rows] == [0,1]
            assert rows[-1]["prev_is_correct"] is None
            repo.append_event(conn,1,"a","ru_de",True)
            assert conn.execute("SELECT prev_is_correct FROM quiz_events ORDER BY id DESC LIMIT 1").fetchone()[0] == 0

    def test_DB12_targeted_learning_profiles_and_stale_preview(self):
        db = self.db
        for status, preview, pending in (("active",0,1),("active",1,1),("active",1,0),("completed",1,0),("skipped",0,0),(None,0,0)):
            db = new_db()
            seed(db)
            with repo.transaction(db) as conn:
                if status is None:
                    conn.execute("DELETE FROM learning_items WHERE word_id='a'")
                else:
                    conn.execute("UPDATE learning_items SET status=?,preview_done=?,first_due_pending=? WHERE word_id='a'",(status,preview,pending))
            man = manifest(db)
            repair.apply_reset(db,man,integration_verified=True)
            with repo.transaction(db) as conn:
                assert conn.execute("SELECT 1 FROM learning_items WHERE word_id='a'").fetchone() is None
                assert conn.execute("SELECT first_due_pending FROM learning_items WHERE word_id='b'").fetchone()[0] == 1
            with self.assertRaises(generations.StaleGeneration):
                answers.apply_preview_transition(db,1,"b","preview_next","new",77,1,flow_id="flow",live_revision=1,
                     transition=lambda *args: self.fail("stale preview wrote"),affected_learning_ids=["a","b"],expected_generations={"a":0,"b":0})
            invalid = manifest(db)
            invalid["operation_id"] = "invalid-mode-reset"
            invalid["modes"] = ["ru_de"]
            invalid["digest"] = repair.manifest_digest(invalid)
            with repo.transaction(db) as conn:
                with self.assertRaisesRegex(ValueError, "full"):
                    repair.preflight(conn,invalid)

    def test_DB13_import_round_trip_all_POS_and_retained_examples(self):
        db = self.db
        package = {"schema":import_package.SCHEMA,"entries":[]}
        with repo.transaction(db) as conn:
            package["baseline_fingerprint"] = db_safety._connection_fingerprint(conn)
            for kind, german in (("nomen","Haus"),("adjektiv","gut"),("phrase","Guten Tag"),("praeposition","in"),("verb","sich waschen")):
                fields = verb("unused","akkusativ") if kind == "verb" else {"type":kind,"german":german,"reflexive_case":None}
                if kind == "praeposition":
                    fields["case_type"] = "wechsel"
                cases = ["akkusativ","akkusativ","dativ","dativ"] if kind == "praeposition" else [fields.get("reflexive_case")]*3
                examples = [{"text":f"Example {kind} {i}","sense_id":"one","case":case} for i,case in enumerate(cases)]
                fields["example"] = "\n".join(e["text"] for e in examples)
                key = list(import_package.identity(fields)) if kind == "verb" else [kind,german,fields.get("preposition_type"),fields.get("case_type")]
                package["entries"].append({"id":import_package.stable_id(fields),"identity":key,"fields":fields,
                    "source":{"provenance":"synthetic fixture","sha256":"a"*64},"senses":[{"id":"one"}],"examples":examples})
            package["digest"] = import_package.package_digest(package)
            assert import_package.round_trip(package) == package
            result = import_package.preflight(conn,package)
            assert not result["apply_allowed"] and len(result["entries"]) == 5
            assert all(e["fields"]["example"].count("\n") >= 2 for e in result["entries"])

    def test_DB13_nonverb_existing_identity_requires_original_id_and_mapping(self):
        db = self.db
        cases = [
            dict(type="nomen", german="Haus", article="das"),
            dict(type="adjektiv", german="gut"),
            dict(type="phrase", german="Guten Tag"),
            dict(type="praeposition", german="in", preposition_type="local", case_type="dativ"),
        ]
        for index, fields in enumerate(cases):
            wid = "established-" + str(index)
            storage.add_word(db, {**fields, "id": wid})
            with repo.transaction(db) as conn:
                old = dict(conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone())
                package = import_fixture(conn, [fields])
                with self.assertRaisesRegex(ValueError, "original ID/mapping"):
                    import_package.preflight(conn, package)
                item = package["entries"][0]
                item["id"] = wid
                package["digest"] = import_package.package_digest(package)
                with self.assertRaisesRegex(ValueError, "mapping/baseline"):
                    import_package.preflight(conn, package)
                item.update(existing_id=wid, expected_before_sha256=repair.digest(old))
                package["digest"] = import_package.package_digest(package)
                checked = import_package.preflight(conn, package)
                assert checked["entries"][0]["id"] == wid and not checked["apply_allowed"]
                item["expected_before_sha256"] = "0" * 64
                package["digest"] = import_package.package_digest(package)
                with self.assertRaisesRegex(ValueError, "mapping/baseline"):
                    import_package.preflight(conn, package)
        # Noun gender and irrelevant preposition metadata do not split identity.
        with repo.transaction(db) as conn:
            for fields in (dict(type="nomen", german="HAUS", article="die"),
                           dict(type="nomen", german="Haus", preposition_type="local", case_type="dativ")):
                with self.assertRaisesRegex(ValueError, "original ID/mapping"):
                    import_package.preflight(conn, import_fixture(conn, [fields]))

    def test_DB13_package_internal_identity_and_distinct_POS_preposition_cases(self):
        db = self.db
        with repo.transaction(db) as conn:
            duplicates = [
                [dict(type="nomen", german="Haus"), dict(type="nomen", german="haus", preposition_type="local")],
                [dict(type="nomen", german="Haus"), dict(type="nomen", german="  Haus  ")],
                [dict(type="phrase", german="Guten   Tag"), dict(type="phrase", german=" Guten Tag ")],
                [dict(type="praeposition", german="in", preposition_type="local", case_type="dativ"),
                 dict(type="praeposition", german=" IN ", preposition_type="local", case_type="dativ")],
            ]
            for entries in duplicates:
                with self.assertRaisesRegex(ValueError, "duplicate/invalid import identity"):
                    import_package.preflight(conn, import_fixture(conn, entries))
            distinct = [dict(type="nomen", german="See", article="der"), dict(type="adjektiv", german="See"),
                        dict(type="praeposition", german="in", preposition_type="local", case_type="dativ"),
                        dict(type="praeposition", german="in", preposition_type="local", case_type="akkusativ"),
                        dict(type="praeposition", german="in", preposition_type="temporal", case_type="dativ")]
            checked = import_package.preflight(conn, import_fixture(conn, distinct))
            assert len(checked["entries"]) == 5
            assert checked["entries"][0]["fields"]["german"] == "See"
            assert checked["entries"][1]["fields"]["german"] == "See"
        storage.add_word(db, dict(id="old-prep", type="praeposition", german="in", preposition_type="local", case_type="dativ"))
        storage.add_word(db, dict(id="old-noun", type="nomen", german="See"))
        with repo.transaction(db) as conn:
            checked = import_package.preflight(conn, import_fixture(conn, [distinct[1], distinct[3], distinct[4]]))
            assert len(checked["entries"]) == 3

    def test_DB13_historical_ID_mappings_preserved_without_new_duplicates(self):
        db = self.db
        with repo.transaction(db) as conn:
            conn.executemany("INSERT INTO words(id,type,german,article) VALUES (?,'nomen','See',?)",
                             [("der-see", "der"), ("die-see", "die")])
            conn.execute("INSERT INTO words(id,type,german,article) VALUES ('house','nomen','Haus','das')")
            before = db_safety._connection_fingerprint(conn)
            old = {wid: dict(conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone())
                   for wid in ("der-see", "die-see", "house")}

            def mapped_package(word_ids):
                package = import_fixture(conn, [old[wid] for wid in word_ids])
                for item, wid in zip(package["entries"], word_ids):
                    item.update(id=wid, existing_id=wid, expected_before_sha256=repair.digest(old[wid]))
                package["digest"] = import_package.package_digest(package)
                return package

            for ids in (["der-see"], ["die-see"], ["der-see", "die-see"], ["die-see", "der-see"]):
                with self.subTest(existing_ids=ids):
                    checked = import_package.preflight(conn, mapped_package(ids))
                    assert [entry["id"] for entry in checked["entries"]] == ids
                    assert [entry["fields"]["article"] for entry in checked["entries"]] == [old[wid]["article"] for wid in ids]
            # New IDs remain duplicates, independently of noun article.
            for fields in (dict(type="nomen", german="See", article="das"),
                           dict(type="nomen", german=" see ", article="der")):
                with self.assertRaises(ValueError):
                    import_package.preflight(conn, import_fixture(conn, [fields]))
            # A new duplicate cannot be bundled alongside a valid old mapping.
            mixed = mapped_package(["der-see"])
            mixed["entries"].extend(import_fixture(conn, [dict(type="nomen", german="See", article="die")])["entries"])
            mixed["digest"] = import_package.package_digest(mixed)
            with self.assertRaisesRegex(ValueError, "duplicate/invalid import identity"):
                import_package.preflight(conn, mixed)
            # Mapping does not authorize changing an unrelated existing ID into See.
            moved = mapped_package(["house"])
            item = moved["entries"][0]
            item["fields"]["german"] = "See"
            item["identity"] = ["nomen", "See", "", ""]
            moved["digest"] = import_package.package_digest(moved)
            with self.assertRaisesRegex(ValueError, "original ID/mapping"):
                import_package.preflight(conn, moved)
            # Neither a stale before digest nor a repeated same ID is legalized.
            stale = mapped_package(["der-see", "die-see"])
            stale["entries"][1]["expected_before_sha256"] = "0" * 64
            stale["digest"] = import_package.package_digest(stale)
            with self.assertRaisesRegex(ValueError, "mapping/baseline"):
                import_package.preflight(conn, stale)
            with self.assertRaisesRegex(ValueError, "duplicate/invalid import identity"):
                import_package.preflight(conn, mapped_package(["der-see", "der-see"]))
            assert db_safety._connection_fingerprint(conn) == before

    def test_metadata_only_confirmation_preserves_study_and_prohibits_direct_patch(self):
        db = self.db
        storage.add_word(db,verb("a"))
        with repo.transaction(db) as conn:
            conn.execute("UPDATE words SET reflexive_case=NULL WHERE id='a'")
            repo.set_progress(conn,1,"a","ru_de",dict(interval=20,repetitions=4,ease=2.7,due="2026-11-01"))
            before = repair.state_rows(conn,1,"a")
            man = {"schema":"gd-verb-confirmation/v1","operation_id":"confirm","approvals":{"owner":True,"linguistic":True},
                   "baseline_fingerprint":db_safety._connection_fingerprint(conn),"entries":[{"word_id":"a","expected_before_sha256":repair.digest(dict(conn.execute("SELECT * FROM words WHERE id='a'").fetchone()))}]}
        man["digest"] = repair.manifest_digest(man)
        with self.assertRaisesRegex(ValueError, "manifest"):
            storage.update_word_fields(db,"a",{"reflexive_case":"none"})
        repair.confirm_legacy(db,man)
        with repo.transaction(db) as conn:
            assert repair.state_rows(conn,1,"a") == before
            assert generations.generation_for(conn,1,"a") == 0
        assert repair.confirm_legacy(db,man)["replayed"]

    def test_old_receipt_correction_and_delayed_original_are_barred(self):
        db = self.db
        seed(db)
        receipt = answers.apply_original_answer(db,1,"a","ru_de","wrong","old",101,1,flow_id="flow",live_revision=1)
        repair.apply_reset(db,manifest(db),integration_verified=True)
        assert answers.apply_original_answer(db,1,"a","ru_de","wrong","old",101,1,flow_id="flow",live_revision=1)["replayed"]
        with self.assertRaises(generations.StaleGeneration):
            answers.correct_last_answer(db,1,receipt,"correction",102,1,flow_id="flow",live_revision=1)
        with self.assertRaises(generations.StaleGeneration):
            answers.apply_original_answer(db,1,"a","ru_de","correct","new",103,1,flow_id="flow",live_revision=1)
        assert storage.get_progress(db,1,"a","ru_de")["repetitions"] == 0

    def test_legacy_direct_SQL_event_and_count_snapshot_undo(self):
        db = self.db
        storage.add_word(db, verb("a"))
        before = storage.snapshot_user_word_state(db, 1, "a", ["ru_de"])
        with repo.transaction(db) as conn:
            conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES (1,'a','ru_de',0)")
        assert storage.restore_user_word_state(db, before)
        with repo.transaction(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0] == 0
        with repo.transaction(db) as conn:
            generations.advance_generation(conn, 1, "a", 0)
            current_event = repo.append_event(conn, 1, "a", "ru_de", True)
            old = {"version": 1, "user_id": 1, "word_id": "a", "modes": ["ru_de"],
                   "progress": {"ru_de": None}, "event_counts": {"ru_de": 0}}
            assert not repo.restore_snapshot(conn, old)
            assert conn.execute("SELECT 1 FROM quiz_events WHERE id=?", (current_event,)).fetchone()
        storage.add_word(db, verb("b", infinitive="gehen"))
        with repo.transaction(db) as conn:
            repo.set_progress(conn, 1, "b", "ru_de", dict(interval=7,repetitions=3,ease=2.5,due="2026-11-01"))
            malformed = {"version": 3, "user_id": 1, "word_id": "b", "modes": ["ru_de"], "progress": {"ru_de": None}}
            assert not repo.restore_snapshot(conn, malformed)
            assert malformed["restore_outcomes"] == {"b": "unknown_generation"}
            assert conn.execute("SELECT interval FROM progress WHERE word_id='b'").fetchone()[0] == 7

    def test_processing_composite_undo_resumes_selectively_after_reset(self):
        db = self.db
        seed(db)
        def transition(conn, snapshot, now):
            conn.execute("UPDATE learning_items SET step_index=2 WHERE user_id=1")
            return {"changed": ["a", "b"]}
        answers.apply_preview_transition(db, 1, "a", "preview_next", "preview", 220, 1,
            flow_id="learning", live_revision=1, transition=transition,
            affected_learning_ids=["a", "b"], expected_generations={"a": 0, "b": 0})
        with patch.object(answers, "_advance_undo", side_effect=RuntimeError("restart")):
            with self.assertRaisesRegex(RuntimeError, "restart"):
                answers.undo_steps(db, 1, 1, 221)
        repair.apply_reset(db, manifest(db), integration_verified=True)
        result = answers.resume_pending_undo(db, 1)[0]
        assert result["outcomes"][0]["participants"] == {"a": "stale_generation", "b": "restored"}
        assert answers.undo_steps(db, 1, 10, 221)["replayed"]
        with repo.transaction(db) as conn:
            assert conn.execute("SELECT 1 FROM learning_items WHERE word_id='a'").fetchone() is None
            assert conn.execute("SELECT step_index FROM learning_items WHERE word_id='b'").fetchone()[0] == 0
            assert generations.generation_for(conn, 1, "a") == 1

    def test_stale_generation_manifest_and_SQL_duplicate_rejected_before_writes(self):
        db = self.db
        seed(db)
        invalid = manifest(db)
        invalid["targets"][0]["users"][0]["expected_generation"] = 1
        invalid["digest"] = repair.manifest_digest(invalid)
        fingerprint = db_safety.database_fingerprint(db)
        with self.assertRaises(generations.StaleGeneration):
            repair.apply_reset(db, invalid, integration_verified=True)
        assert db_safety.database_fingerprint(db) == fingerprint
        with repo.transaction(db) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO words(id,type,infinitive,reflexive_case) VALUES ('dup','verb','sich waschen','akkusativ')")

if __name__ == "__main__":
    unittest.main()
