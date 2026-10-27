"""Synthetic clean-consumer fixtures; no source bundles or real databases opened."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from bot import storage
from bot.common import import_clean_bundle as importer
from bot.common import data18_builder as builder, db_safety
from bot.study import repair, repository
from scripts import dictionary_import_clean as cli

STAND = Path(os.environ.get("GD_TEST_STAND", str(Path(tempfile.gettempdir()) / "gd888-clean-import-tests")))


def update_item(row, **changes):
    return dict(id=row["id"], expected_before_row_sha256=importer.snapshot_row_sha256(row),
                expected_before={k: row[k] for k in changes}, set=changes, reasons=["synthetic approval"])


def insert_card(text, **fields):
    fields = dict(type="phrase", german=text, russian="текст", ukrainian="текст", english="text",
                  topic="T", **fields)
    return dict(id=builder.stable_id(fields), source_key="synthetic:" + text, fields=fields)


class CleanImportFixture(unittest.TestCase):
    """Reusable by retained scenario ports. Helpers never commit an import implicitly.

    add_word / bundle / approve / bound / apply; conn starts in a transaction.
    approve patches only the source SHA constant for this synthetic envelope.
    """
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.path = STAND / ("clean-" + uuid.uuid4().hex + ".sqlite")
        storage.init_db(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT OR IGNORE INTO topics(name,description,level) VALUES('T','test','A1')")
        self.conn.commit()
        self.conn.execute("BEGIN")

    def add_word(self, wid="ordinary", **fields):
        row = dict(id=wid, type="verb", german="lesen", infinitive="lesen", prateritum="las",
                   partizip2="gelesen", russian="читать", ukrainian="читати", english="read",
                   topic="T", example="Alt.", is_separable=0)
        row.update(fields)
        keys = sorted(row)
        self.conn.execute("INSERT INTO words(" + ",".join(keys) + ") VALUES(" +
                          ",".join("?" for _ in keys) + ")", [row[k] for k in keys])
        return self.word(wid)

    def word(self, wid="ordinary"):
        return dict(self.conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone())

    def bundle(self, updates=(), inserts=(), repairs=()):
        result = dict(schema=importer.SOURCE_SCHEMA, date="2026-10-27", preparation_complete=True,
            apply_authorized=False, runtime_apply_compatible=False,
            target_snapshot=dict(schema_version=17, words=self.conn.execute("SELECT count(*) FROM words").fetchone()[0]),
            words_insert=list(inserts), words_repair=list(repairs), existing_word_updates=list(updates),
            adjective_degree_variants=[], synonym_links=[],
            topics=dict(exact_legacy_merge={"OLD": "NEW"}, registry_operations=[],
                        book_display=dict(product_changes_applied=False)),
            deferred_manual=dict(apply=False, prepositions=[], pons_cards=[]),
            preserved=dict(protected_example_ids=[], existing_preposition_example_ids=[]),
            stage="synthetic", execution_gates=[])
        result["counts"] = importer._counts(result)
        return result

    def approve(self, bundle, *, confirmations=True, repairs=True):
        bundle["counts"] = importer._counts(bundle)
        raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        pin = patch.object(importer, "APPROVED_SOURCE_SHA256", sha)
        pin.start()
        self.addCleanup(pin.stop)
        approval = dict(owner=True, linguistic=True, evidence="synthetic reviewed source", source_sha256=sha)
        if confirmations:
            approval["ordinary_confirmations"] = [dict(word_id=u["id"], reflexive_case="none",
                expected_before_sha256=repair.digest(self.word(u["id"])), evidence="synthetic explicit ordinary review",
                source_sha256=importer.ORDINARY_EVIDENCE_SOURCE_SHA256,
                source_pointer="metadata/0", source_record_sha256="a" * 64)
                for u in bundle["existing_word_updates"] if "hilfsverb" in u["set"]]
        if bundle["words_repair"] and repairs:
            manifest = importer.draft_repair_manifest(self.conn, bundle, sha)
            manifest["approvals"] = {"owner": True, "linguistic": True}
            manifest["digest"] = repair.manifest_digest(manifest)
            approval.update(repair_manifest=manifest, repair_evidence="synthetic repair reviewed")
        return sha, approval

    def bound(self, bundle=None, **options):
        if bundle is None:
            row = self.add_word()
            bundle = self.bundle([update_item(row, hilfsverb="hat")])
        sha, approval = self.approve(bundle, **options)
        return importer.bind(self.conn, bundle, sha, approval)

    def apply(self, bound, **kwargs):
        return importer.apply(self.conn, bound, approved_digest=bound["digest"], **kwargs)

    def state(self):
        return "\n".join(self.conn.iterdump())

    def repair_cards(self):
        cards = []
        for lemma, pr, p2 in (("bedanken", "bedankte", "bedankt"), ("befinden", "befand", "befunden")):
            row = self.add_word("repair-" + lemma, german=lemma, infinitive=lemma, prateritum=pr, partizip2=p2)
            repository.set_progress(self.conn, 1, row["id"], "ru_de",
                                    dict(interval=8, repetitions=3, ease=2.5, due="2026-11-01"))
            self.conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES(2,'historic',?)",
                              (json.dumps({"word_id": row["id"]}),))
            fields = {k: row[k] for k in ("type", "russian", "ukrainian", "english", "topic", "is_separable", "partizip2")}
            fields.update(german="sich " + lemma, infinitive="sich " + lemma,
                          prateritum=pr + " sich", reflexive_case="akkusativ", hilfsverb="hat")
            cards.append(dict(id=row["id"], source_key="synthetic:" + lemma, fields=fields,
                expected_before={"infinitive": lemma}, expected_before_row_sha256=importer.snapshot_row_sha256(row),
                reset_profile="relearn_word", preserve_id=True, preserve_completed_history=True,
                requires_fresh_live_repair_generation_manifest=True))
        return cards


class CleanImportTest(CleanImportFixture):
    def test_missing_classification_rejected_read_only(self):
        for fields in ({}, {"german": "sich waschen", "infinitive": "sich waschen"}):
            wid = "w" + str(len(fields))
            row = self.add_word(wid, **fields)
            b = self.bundle([update_item(row, hilfsverb="hat")])
            sha, approval = self.approve(b, confirmations=False)
            before = self.state()
            with self.assertRaisesRegex(ValueError, "per-ID ordinary"):
                importer.bind(self.conn, b, sha, approval)
            self.assertEqual(before, self.state())

    def test_false_ordinary_confirmation_cannot_classify_sich(self):
        row = self.add_word(german="sich waschen", infinitive="sich waschen")
        with self.assertRaisesRegex(ValueError, "ordinary infinitive"):
            self.bound(self.bundle([update_item(row, hilfsverb="hat")]))

    def test_confirmation_preserves_progress_and_fields_zero_degree_table_replay(self):
        row = self.add_word()
        repository.set_progress(self.conn, 1, row["id"], "ru_de", dict(interval=12, repetitions=5, ease=2.5, due="2026-11-01"))
        progress = [tuple(r) for r in self.conn.execute("SELECT * FROM progress")]
        b = self.bound(self.bundle([update_item(row, hilfsverb="hat")]))
        before = self.state()
        self.assertFalse(importer.preflight(self.conn, json.loads(json.dumps(b)))["replayed"])
        self.assertEqual(before, self.state())
        self.apply(b)
        after = self.word()
        self.assertEqual(after["reflexive_case"], "none")
        self.assertEqual(after["hilfsverb"], "hat")
        self.assertEqual({k:v for k,v in row.items() if k not in ("updated_at", "reflexive_case", "hilfsverb")},
                         {k:v for k,v in after.items() if k not in ("updated_at", "reflexive_case", "hilfsverb")})
        self.assertEqual(progress, [tuple(r) for r in self.conn.execute("SELECT * FROM progress")])
        state = self.state()
        self.assertEqual(self.apply(b)["operations"], 0)
        self.assertEqual(state, self.state())

    def test_verified_none_requires_evidence_and_remains_none(self):
        row = self.add_word(reflexive_case="none")
        b = self.bundle([update_item(row, hilfsverb="hat")])
        sha, approval = self.approve(b, confirmations=False)
        with self.assertRaises(ValueError): importer.bind(self.conn, b, sha, approval)
        bound = self.bound(b)
        self.assertEqual(bound["binding"]["confirmations"], [])
        self.apply(bound)
        self.assertEqual(self.word()["reflexive_case"], "none")

    def test_confirmation_evidence_and_row_hash_are_strict(self):
        row = self.add_word()
        b = self.bundle([update_item(row, hilfsverb="hat")])
        sha, approval = self.approve(b)
        for key, value in (("expected_before_sha256", "0"*64), ("source_sha256", "0"*64),
                           ("source_record_sha256", "bad"), ("reflexive_case", "akkusativ")):
            bad = deepcopy(approval)
            bad["ordinary_confirmations"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): importer.bind(self.conn, b, sha, bad)

    def test_original_schema17_hash_still_guards_confirmed_none(self):
        row = self.add_word(reflexive_case="none")
        b = self.bundle([update_item(row, hilfsverb="hat")])
        self.conn.execute("UPDATE words SET image='changed' WHERE id='ordinary'")
        with self.assertRaisesRegex(ValueError, "baseline changed"): self.bound(b)

    def test_schema_missing_trigger_and_domain_rejected_read_only(self):
        b = self.bundle()
        sha, approval = self.approve(b)
        self.conn.execute("SAVEPOINT test_schema")
        self.conn.execute("DROP TRIGGER words_hilfsverb_type_update")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "trigger"): importer.bind(self.conn, b, sha, approval)
        self.assertEqual(before, self.state())
        self.conn.execute("ROLLBACK TO test_schema")
        self.conn.execute("RELEASE test_schema")
        self.conn.execute("DROP TRIGGER words_hilfsverb_type_insert")
        self.conn.execute("DROP TRIGGER words_hilfsverb_type_update")
        self.conn.execute("ALTER TABLE words DROP COLUMN hilfsverb")
        self.conn.execute("ALTER TABLE words ADD COLUMN hilfsverb TEXT")
        with self.assertRaisesRegex(ValueError, "CHECK"): importer.bind(self.conn, b, sha, approval)

    def test_schema21_rejected(self):
        self.conn.execute("PRAGMA user_version=21")
        with self.assertRaisesRegex(ValueError, "schema22"): self.bound(self.bundle())

    def test_incomplete_untouched_translation_rejected_before_write(self):
        self.add_word(english="")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "translation completeness"): self.bound(self.bundle())
        self.assertEqual(before, self.state())

    def test_post_bind_drift_rejected(self):
        b = self.bound()
        self.conn.execute("UPDATE words SET image='changed'")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "fingerprint"): self.apply(b)
        self.assertEqual(before, self.state())

    def test_two_repairs_then_fault_rolls_back_everything(self):
        b = self.bound(self.bundle(repairs=self.repair_cards()))
        before = self.state()
        with patch.object(importer.topics, "apply_plan", side_effect=RuntimeError("after repairs")):
            with self.assertRaisesRegex(RuntimeError, "after repairs"):
                self.apply(b, integration_verified=True)
        self.assertEqual(before, self.state())
        self.apply(b, integration_verified=True)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM progress").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM transaction_log").fetchone()[0], 2)
        self.assertEqual(self.apply(b, integration_verified=True)["operations"], 0)

    def test_repair_stale_generation_manifest_rejected(self):
        b = self.bundle(repairs=self.repair_cards())
        sha, approval = self.approve(b)
        manifest = approval["repair_manifest"]
        manifest["targets"][0]["users"][0]["expected_generation"] += 1
        manifest["digest"] = repair.manifest_digest(manifest)
        before = self.state()
        with self.assertRaises(ValueError): importer.bind(self.conn, b, sha, approval)
        self.assertEqual(before, self.state())

    def cli_files(self, bound):
        self.conn.commit()
        directory = STAND / uuid.uuid4().hex
        directory.mkdir()
        for name, data in (("bound.json", bound), ("approval.json", bound["approvals"])):
            (directory/name).write_text(json.dumps(data))
        return directory, ["apply", "--db", str(self.path), "--bound", str(directory/"bound.json"),
                           "--approval", str(directory/"approval.json"), "--approved-digest", bound["digest"], "--apply"]

    def test_cli_existing_output_rejected_before_write(self):
        b = self.bound()
        directory, args = self.cli_files(b)
        output = directory/"output.json"
        output.write_text("do not overwrite")
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError): cli.main(args + ["--output", str(output)])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(output.read_text(), "do not overwrite")

    def test_cli_output_failure_rolls_back(self):
        b = self.bound()
        directory, args = self.cli_files(b)
        before = self.state()
        with patch.object(cli, "_output", side_effect=OSError("output failure")):
            with self.assertRaises(OSError): cli.main(args + ["--output", str(directory/"result.json")])
        self.assertEqual(before, self.state())

    def test_cli_serialization_failure_rolls_back_before_commit(self):
        b = self.bound()
        _, args = self.cli_files(b)
        before = self.state()
        with patch.object(cli, "_encode", side_effect=TypeError("serialization failure")):
            with self.assertRaises(TypeError): cli.main(args)
        self.assertEqual(before, self.state())

    def test_cli_commit_failure_leaves_only_provisional_output(self):
        b = self.bound()
        directory, args = self.cli_files(b)
        output = directory/"result.json"
        before = self.state()
        original = cli.connection
        class FailCommit:
            def __init__(self, conn): self.conn = conn
            def __getattr__(self, name): return getattr(self.conn, name)
            def commit(self): raise sqlite3.OperationalError("injected commit failure")
        with patch.object(cli, "connection", side_effect=lambda *a, **kw: FailCommit(original(*a, **kw))):
            with self.assertRaisesRegex(sqlite3.OperationalError, "commit failure"):
                cli.main(args + ["--output", str(output)])
        self.assertEqual(before, self.state())
        result = json.loads(output.read_text())
        self.assertIs(result["committed"], False)
        self.assertEqual(result["operation_id"], b["operation_id"])

    def test_cli_file_success_is_one_committed_json(self):
        b = self.bound()
        directory, args = self.cli_files(b)
        output = directory/"result.json"
        self.assertEqual(cli.main(args + ["--output", str(output)]), 0)
        result = json.loads(output.read_text())
        self.assertIs(result["committed"], True)
        self.assertGreater(result["operations"], 0)
        self.assertEqual(result["operation_id"], b["operation_id"])

    def test_cli_postcommit_file_failure_reports_committed_operation(self):
        b = self.bound()
        directory, args = self.cli_files(b)
        output = directory/"result.json"
        real_output = cli._output
        def fail_final(result, stream):
            if result.get("committed"):
                raise OSError("injected final output failure")
            return real_output(result, stream)
        diagnostic = io.StringIO()
        with patch.object(cli, "_output", side_effect=fail_final), redirect_stderr(diagnostic):
            with self.assertRaisesRegex(RuntimeError, "committed=true"):
                cli.main(args + ["--output", str(output)])
        self.assertIn(b["operation_id"], diagnostic.getvalue())
        self.assertIn("БД зафиксирована", diagnostic.getvalue())
        self.assertIs(json.loads(output.read_text())["committed"], False)
        self.assertTrue(importer.preflight(self.conn, b)["replayed"])

    def test_cli_broken_stdout_reports_committed_operation(self):
        b = self.bound()
        _, args = self.cli_files(b)
        diagnostic = io.StringIO()
        with patch.object(cli, "_output", side_effect=BrokenPipeError("closed stdout")), redirect_stderr(diagnostic):
            with self.assertRaisesRegex(RuntimeError, "committed=true"):
                cli.main(args)
        self.assertIn(b["operation_id"], diagnostic.getvalue())
        self.assertTrue(importer.preflight(self.conn, b)["replayed"])

    def test_cli_apply_success_and_replay(self):
        b = self.bound()
        _, args = self.cli_files(b)
        output = io.StringIO()
        with redirect_stdout(output): self.assertEqual(cli.main(args), 0)
        self.assertGreater(json.loads(output.getvalue())["operations"], 0)
        self.assertIs(json.loads(output.getvalue())["committed"], True)
        output = io.StringIO()
        with redirect_stdout(output): self.assertEqual(cli.main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["operations"], 0)

    def test_insert_synonym_union(self):
        cards = [insert_card("guck mal"), insert_card("schau mal")]
        b = self.bundle(inserts=cards)
        b["synonym_links"] = [dict(word_ids=[c["id"] for c in cards])]
        self.apply(self.bound(b))
        rows = self.conn.execute("SELECT group_id FROM word_synonyms").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], rows[1][0])

    def test_degree_pair_insert(self):
        fields = dict(type="adjektiv", german="klein", komparativ="kleiner", superlativ="am kleinsten",
                      russian="малый", ukrainian="малий", english="small", topic="T")
        card = dict(id=builder.stable_id(fields), source_key="synthetic:klein", fields=fields)
        b = self.bundle(inserts=[card])
        b["adjective_degree_variants"] = [dict(word_id=card["id"], comparative="kleiner", superlative="am kleinsten")]
        self.apply(self.bound(b))
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT word_id,comparative,superlative FROM adjective_degree_variants")],
            [(card["id"], "kleiner", "am kleinsten")])

    def test_topic_merge_preserves_protected_example_and_filter_revision(self):
        for name in ("OLD", "NEW"):
            self.conn.execute("INSERT INTO topics(name,description,level) VALUES(?, 'test','A1')", (name,))
        row = self.add_word(topic="OLD")
        self.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'topic_filter','OLD+T')")
        self.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'filter_revision','7')")
        b = self.bundle([update_item(row, topic="NEW")])
        b["preserved"]["protected_example_ids"] = [row["id"]]
        b["topics"]["registry_operations"] = [dict(operation="retire_exact_registry_row",
            expected_old=dict(name="OLD", description="test", level="A1"), new=None)]
        self.apply(self.bound(b))
        self.assertEqual(self.word()["example"], row["example"])
        self.assertEqual(self.word()["topic"], "NEW")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM topics WHERE name='OLD'").fetchone())
        settings = dict(self.conn.execute("SELECT key,value FROM user_settings WHERE user_id=1"))
        self.assertEqual(set(settings["topic_filter"].split("+")), {"NEW", "T"})
        self.assertEqual(settings["filter_revision"], "8")
