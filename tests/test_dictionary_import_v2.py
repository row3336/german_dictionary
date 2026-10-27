"""Retained 28 security scenarios, ported to clean schema22 consumer.

Historical builder/CLI preparation remains pure. All DB behavior uses the clean
consumer and synthetic stand databases; retired v2/v3 DB entry points refuse.
"""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import unittest
import uuid
from unittest.mock import patch
from bot.common import data18_builder as builder, import_clean_bundle as importer
from bot.common import book_topic_backfill as topics, db_safety
from bot.study import repair, repository, generations
from test_clean_import_bundle import CleanImportFixture, update_item, STAND

SOURCE = Path("/Users/wf/ai_wf/ai_wf_data/888_german-dictionary-doki/data18_6_technical_2026-10-25")


def phrase(text):
    fields = dict(type="phrase", german=text, russian="посмотри", ukrainian="подивись", english="look", topic="2")
    return dict(id=builder.stable_id(fields), source_key="synthetic:"+text, fields=fields)


class DictionaryImportV2Test(CleanImportFixture):
    @classmethod
    def setUpClass(cls):
        cls.full = builder.build_package(SOURCE)  # Historical pure source audit only.

    def setUp(self):
        super().setUp()
        for name in ("03", "2", "A2_03"):
            self.conn.execute("INSERT INTO topics(name,description,level) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description,level=excluded.level", (name,"topic","A2" if name.startswith("A2") else "A1"))
        self.add_word("ordinary", german="treffen", infinitive="treffen", prateritum="traf", partizip2="getroffen", topic="2", is_separable=None)
        self.add_word("topic-word", type="adverb", german="damals", infinitive="", prateritum="", partizip2="", topic="03", is_separable=None)
        repository.set_progress(self.conn,1,"ordinary","ru_de",dict(interval=12,repetitions=5,ease=2.5,due="2026-11-01"))
        self.conn.execute("INSERT INTO user_word_media(user_id,word_id,example) VALUES(1,'ordinary','personal untouched')")
        self.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'topic_filter','03+2')")
        self.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'filter_revision','7')")
        cfg=dict(version=2,selected_levels=["A2"],topics_by_level={"A1":["03","2"],"A2":["03","A2_03"]},browsing_level="A2",revision=7)
        self.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'book_topic_filter_v2',?)",(json.dumps(cfg),))
        self.conn.commit()
        self.conn.execute("BEGIN")

    def draft(self, *, repairs=False):
        cards = [phrase("guck mal"), phrase("schau mal")]
        resets = self.repair_cards() if repairs else []
        updates = [update_item(self.word("ordinary"), hilfsverb="hat"), update_item(self.word("topic-word"), topic="2")]
        bundle = self.bundle(updates, cards, resets)
        bundle["topics"] = dict(exact_legacy_merge={"03":"2"}, registry_operations=[dict(operation="retire_exact_registry_row", expected_old=dict(name="03",description="topic",level="A1"),new=None)],book_display=dict(product_changes_applied=False))
        bundle["synonym_links"] = [dict(word_ids=[c["id"] for c in cards])]
        bundle["preserved"]["protected_example_ids"] = ["ordinary", "topic-word"]
        bundle["counts"] = importer._counts(bundle)
        return bundle

    def approval(self, bundle):
        _, approval = self.approve(bundle)
        return approval

    def bind_draft(self, bundle, approval=None):
        approval = self.approval(bundle) if approval is None else approval
        return importer.bind(self.conn, bundle, approval["source_sha256"], approval)

    def bound(self, *, repairs=False):
        return self.bind_draft(self.draft(repairs=repairs))

    def test_full_pinned_builder_counts_mappings_provenance_determinism(self):
        p=self.full
        self.assertEqual(builder.validate_content(p),p["expected_counts"])
        self.assertEqual(builder.build_package(SOURCE),p)
        self.assertEqual(len(p["source_mapping"]),350)
        self.assertEqual(len(p["variant_mapping"]),486)
        self.assertEqual(sum(c["fields"].get("prateritum")!=c["provenance"].get("fields",{}).get("prateritum") for c in p["cards"] if c["fields"]["type"]=="verb"),104)
        self.assertIsNone(p["approvals"])
        self.assertIsNone(p["binding"])
        self.assertEqual(len(p["synonym_links"]),1)
        self.assertEqual(len(set(p["synonym_links"][0]["word_ids"])),2)


    def test_identity_article_case_semantic_nfc_and_no_casefold(self):
        see=dict(type="nomen",german="See",article="der")
        self.assertNotEqual(builder.stable_id(see),builder.stable_id({**see,"article":"die"}))
        v=dict(type="verb",infinitive="sich waschen",reflexive_case="akkusativ")
        self.assertNotEqual(builder.stable_id(v),builder.stable_id({**v,"reflexive_case":"dativ"}))
        p=dict(type="praeposition",german="in",preposition_type="lokal",case_type="dativ")
        self.assertNotEqual(builder.stable_id(p),builder.stable_id({**p,"case_type":"akkusativ"}))
        self.assertNotEqual(builder.canonical("Maße"),builder.canonical("Masse"))
        self.assertEqual(builder.canonical(" Ä "),builder.canonical("Ä"))


    def test_apply_replay_and_preserve_progress_overlay_non_a1_all_semantics(self):
        p=self.bound()
        before=[tuple(r) for r in self.conn.execute("SELECT * FROM progress")]
        personal=[tuple(r) for r in self.conn.execute("SELECT * FROM user_word_media")]
        result=importer.apply(self.conn,p,approved_digest=p["digest"])
        self.assertGreater(result["operations"],0)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(before,[tuple(r) for r in self.conn.execute("SELECT * FROM progress")])
        self.assertEqual(personal,[tuple(r) for r in self.conn.execute("SELECT * FROM user_word_media")])
        self.assertEqual(self.conn.execute("SELECT reflexive_case FROM words WHERE id='ordinary'").fetchone()[0],"none")
        cfg=json.loads(self.conn.execute("SELECT value FROM user_settings WHERE user_id=1 AND key='book_topic_filter_v2'").fetchone()[0])
        self.assertEqual(cfg["topics_by_level"],{"A1":["2"],"A2":["03","A2_03"]})
        self.assertEqual(cfg["revision"],8)
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT group_id) FROM word_synonyms").fetchone()[0],1)
        self.assertEqual(importer.apply(self.conn,p,approved_digest=p["digest"])["operations"],0)
        self.assertEqual(importer.preflight(self.conn,p)["operations"],0)
        self.assertEqual(result["receipt"]["report"]["global_translation_missing"],dict(russian=0,ukrainian=0,english=0))
        self.assertIsNone(self.word("ordinary")["is_separable"])


    def test_wrong_approval_digest_and_caller_transaction_guards(self):
        p=self.bound()
        for digest in (None,"f"*64):
            with self.assertRaises(PermissionError):
                importer.apply(self.conn,p,approved_digest=digest)
        q=deepcopy(p);q["approvals"]["owner"]=False;builder.seal(q)
        with self.assertRaises(PermissionError):
            importer.apply(self.conn,q,approved_digest=q["digest"])
        self.conn.rollback()
        with self.assertRaisesRegex(ValueError,"caller-owned"):
            importer.apply(self.conn,p,approved_digest=p["digest"])


    def test_metadata_requires_explicit_per_id_confirmation(self):
        p=self.draft(); a=self.approval(p)
        a.pop("ordinary_confirmations")
        before=self.state()
        with self.assertRaisesRegex(ValueError,"explicit per-ID"):
            self.bind_draft(p,a)
        self.assertEqual(self.state(),before)

    def test_baseline_and_source_digest_tampering_refused(self):
        p=self.bound()
        self.conn.execute("UPDATE progress SET repetitions=99 WHERE word_id='ordinary'")
        before=self.state()
        with self.assertRaisesRegex(ValueError,"baseline"):
            importer.preflight(self.conn,p)
        p["source"]["words_insert"][0]["fields"]["russian"]="changed"
        with self.assertRaisesRegex(ValueError,"invalid bound"):
            importer.preflight(self.conn,p)
        self.assertEqual(self.state(),before)

    def test_live_job_and_unmanifested_topic_reference_refused(self):
        p=self.draft()
        approval=self.approval(p)
        self.conn.execute("INSERT INTO vf_add_jobs(user_id,status,topic) VALUES(1,'running','2')")
        with self.assertRaisesRegex(ValueError,"live dictionary job"):
            self.bind_draft(p,approval)
        self.conn.execute("UPDATE vf_add_jobs SET status='completed'")
        self.conn.execute("INSERT INTO words(id,type,german,topic) VALUES('unexpected','phrase','unexpected','03')")
        with self.assertRaisesRegex(ValueError,"unmanifested"):
            self.bind_draft(p,approval)


    def test_caught_sql_receipt_failure_rolls_back_entire_savepoint(self):
        self.conn.execute("INSERT INTO app_settings(key,value) VALUES('outer-marker','keep')")
        self.conn.execute("CREATE TRIGGER reject_import_receipt BEFORE INSERT ON study_reset_operations WHEN NEW.id NOT LIKE '%:ordinary' BEGIN SELECT RAISE(ABORT,'injected outer receipt'); END")
        p=self.bound()
        before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaisesRegex(sqlite3.IntegrityError,"injected outer receipt"):
            importer.apply(self.conn,p,approved_digest=p["digest"])
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)
        self.assertEqual(self.conn.execute("SELECT value FROM app_settings WHERE key='outer-marker'").fetchone()[0],"keep")


    def test_caught_mid_insert_failure_rolls_back_confirmation_metadata_topics(self):
        self.conn.execute("CREATE TRIGGER reject_new_word BEFORE INSERT ON words WHEN NEW.german='schau mal' BEGIN SELECT RAISE(ABORT,'injected insert'); END")
        p=self.bound();before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaisesRegex(sqlite3.IntegrityError,"injected insert"):
            importer.apply(self.conn,p,approved_digest=p["digest"])
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)


    def test_receipt_conflict_and_corruption_refused_before_old_baseline(self):
        p=self.bound(); self.apply(p)
        for change in ("operation_id", "evidence"):
            q=deepcopy(p)
            if change=="operation_id": q["operation_id"]+="different"
            else: q["approvals"]["evidence"]="changed"
            builder.seal(q)
            before=self.state()
            with self.assertRaisesRegex(ValueError,"baseline" if change=="operation_id" else "receipt conflict"):
                importer.preflight(self.conn,q)
            self.assertEqual(self.state(),before)
        self.conn.execute("UPDATE study_reset_operations SET result_json='{}' WHERE id=?",(p["operation_id"],))
        with self.assertRaisesRegex(ValueError,"invalid clean import receipt"):
            importer.preflight(self.conn,p)

    def test_two_repairs_require_qa_exact_users_and_rollback_state(self):
        p=self.bound(repairs=True)
        before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaises(PermissionError):
            importer.apply(self.conn,p,approved_digest=p["digest"])
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)
        result=importer.apply(self.conn,p,approved_digest=p["digest"],integration_verified=True)
        for wid in ("repair-bedanken", "repair-befinden"):
            for uid in (1,2):
                self.assertEqual(generations.generation_for(self.conn,uid,wid),1)
            self.assertFalse(self.conn.execute("SELECT 1 FROM progress WHERE word_id=?",(wid,)).fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM progress WHERE word_id='ordinary'").fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0],2)
        receipt=self.conn.execute("SELECT result_json FROM study_reset_operations WHERE id=?",(p["operation_id"]+":ordinary",)).fetchone()[0]
        self.assertEqual(json.loads(receipt)["confirmed"],["ordinary"])
        self.assertIsNotNone(result["receipt"]["confirmation_digest"])
        self.assertEqual(importer.apply(self.conn,p,approved_digest=p["digest"],integration_verified=True)["operations"],0)


    def test_repair_manifest_omitted_durable_user_refused(self):
        p=self.draft(repairs=True);a=self.approval(p)
        for target in a["repair_manifest"]["targets"]:
            target["users"]=[u for u in target["users"] if u["user_id"]==1]
        a["repair_manifest"]["digest"]=repair.manifest_digest(a["repair_manifest"])
        with self.assertRaisesRegex(ValueError,"actual state/durable users exactly"):
            self.bind_draft(p,a)


    def test_malformed_v2_or_revision_mismatch_refuses_everything(self):
        p=self.draft();a=self.approval(p)
        self.conn.execute("UPDATE user_settings SET value='{}' WHERE key='book_topic_filter_v2'")
        with self.assertRaises(ValueError):
            self.bind_draft(p,a)
        self.conn.execute("DELETE FROM user_settings WHERE key='book_topic_filter_v2'")
        self.conn.execute("UPDATE user_settings SET value='-1' WHERE key='filter_revision'")
        with self.assertRaisesRegex(ValueError,"revision"):
            self.bind_draft(p,a)


    def test_all_absent_empty_stale_and_explicit_semantics(self):
        for raw in (None,"","all"," * ","stale","2+12"):
            self.assertEqual(topics.transform_legacy(raw),raw)
        self.assertEqual(topics.transform_legacy("03,2+011+12+012"),"2+12+13")


    def test_unicode_global_missing_translation_rolls_back_all_operations(self):
        self.add_word("untouched-blank",type="phrase",german="historical",russian=" ",ukrainian="   ",english="old",topic="2")
        p=self.draft(); a=self.approval(p); before=self.state()
        with self.assertRaisesRegex(ValueError,"global RU/UK/EN"):
            self.bind_draft(p,a)
        self.assertEqual(self.state(),before)
        self.assertTrue(self.conn.in_transaction)

    def test_silent_valid_repair_content_corruption_is_exactly_detected(self):
        p=self.draft(repairs=True)
        self.conn.execute("CREATE TRIGGER corrupt_repair AFTER UPDATE ON words WHEN NEW.id='repair-bedanken' AND NEW.german='sich bedanken' BEGIN UPDATE words SET russian='valid but unapproved translation' WHERE id=NEW.id; END")
        p=self.bind_draft(p);before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaisesRegex(ValueError,"exact differences"):
            importer.apply(self.conn,p,approved_digest=p["digest"],integration_verified=True)
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)


    def test_final_receipt_failure_rolls_back_both_repairs_and_ordinary_confirmation(self):
        p=self.draft(repairs=True)
        self.conn.execute("CREATE TRIGGER reject_final_receipt BEFORE INSERT ON study_reset_operations WHEN NEW.id LIKE 'gd888-clean-import:%' AND NEW.id NOT LIKE '%:ordinary' BEGIN SELECT RAISE(ABORT,'injected final after repairs'); END")
        p=self.bind_draft(p);before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaisesRegex(sqlite3.IntegrityError,"injected final after repairs"):
            importer.apply(self.conn,p,approved_digest=p["digest"],integration_verified=True)
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)
        self.assertFalse(self.conn.execute("SELECT 1 FROM study_word_generations").fetchone())


    def test_success_retains_historical_separability_and_empty_tables(self):
        p=self.bound()
        importer.apply(self.conn,p,approved_digest=p["digest"])
        row=dict(self.conn.execute("SELECT * FROM words WHERE id='ordinary'").fetchone())
        self.assertIsNone(row["is_separable"])
        self.assertEqual(row["conjugation_praesens"],"")
        self.assertEqual(row["conjugation_praeteritum"],"")


    def test_pinned_source_sha_mismatch_aborts_before_preparation(self):
        with patch.dict(builder.PINNED,{"word_plan.json":"0"*64}):
            with self.assertRaisesRegex(ValueError,"pinned source changed"):
                builder.build_package(SOURCE)


    def test_incomplete_schema21_is_rejected_without_writes(self):
        # Retained incomplete-schema guarantee now targets the schema22 contract.
        p=self.draft(); a=self.approval(p)
        self.conn.execute("DROP TRIGGER words_hilfsverb_type_update")
        before=self.state()
        with self.assertRaisesRegex(ValueError,"schema22 hilfsverb trigger"):
            self.bind_draft(p,a)
        self.assertEqual(self.state(),before)

    def test_cli_build_is_readonly_and_requires_no_configured_db(self):
        from scripts import dictionary_import_v2 as cli
        output=io.StringIO()
        with patch.object(cli,"_connection",side_effect=AssertionError("build opened DB")), redirect_stdout(output):
            self.assertEqual(cli.main(["build","--source-root",str(SOURCE)]),0)
        self.assertEqual(json.loads(output.getvalue())["digest"],self.full["digest"])


    def test_cli_database_and_apply_authority_are_explicit(self):
        from scripts import dictionary_import_clean as cli
        p=self.bound()
        with patch.object(builder,"load_json",return_value=p), patch.object(cli,"connection") as connection, redirect_stderr(io.StringIO()):
            for args in (["preflight","--bound","synthetic.json"],["apply","--bound","synthetic.json","--db",str(self.path)]):
                with self.assertRaises(SystemExit) as error: cli.main(args)
                self.assertEqual(error.exception.code,2)
            connection.assert_not_called()

    def test_cli_readonly_connection_cannot_write_or_create(self):
        from scripts import dictionary_import_clean as cli
        conn=cli.connection(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0],1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO app_settings(key,value) VALUES('forbidden','write')")
        finally:
            conn.close()
        missing=STAND/("nonexistent-"+uuid.uuid4().hex+".sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            cli.connection(missing,writable=True)
        self.assertFalse(missing.exists())


    def test_corrupt_preserved_repair_cefr_rolls_back(self):
        p=self.draft(repairs=True)
        self.conn.execute("CREATE TRIGGER corrupt_repair_cefr AFTER UPDATE ON words WHEN NEW.id='repair-bedanken' AND NEW.german='sich bedanken' BEGIN UPDATE words SET cefr_level='C2' WHERE id=NEW.id; END")
        p=self.bind_draft(p);before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaisesRegex(ValueError,"exact differences"):
            importer.apply(self.conn,p,approved_digest=p["digest"],integration_verified=True)
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)


    def test_two_see_articles_survive_actual_atomic_insert_as_distinct_ids(self):
        p=self.draft()
        for article in ("der","die"):
            fields=dict(type="nomen",german="See",article=article,plural="Seen",russian="смысл",ukrainian="значення",english="meaning",topic="2",example="Example one.\nExample two.")
            p["words_insert"].append(dict(id=builder.stable_id(fields),source_key="synthetic:see:"+article,fields=fields))
        p=self.bind_draft(p)
        self.apply(p)
        rows=self.conn.execute("SELECT id,article,example FROM words WHERE type='nomen' AND german='See' ORDER BY article").fetchall()
        self.assertEqual({r["article"] for r in rows},{"der","die"})
        self.assertEqual(len({r["id"] for r in rows}),2)
        self.assertTrue(all(r["example"]=="Example one.\nExample two." for r in rows))

    def test_wechsel_preposition_requires_two_examples_for_each_case(self):
        fields=dict(type="praeposition",german="in",preposition_type="local",case_type="wechsel",russian="в",ukrainian="в",english="in",topic="2")
        examples=[dict(text_de="Synthetic example "+str(i),translation_ru="Пример",sense_ids=["sense"],case=case) for i,case in enumerate(("Akk","Akk","Dat","Dat"))]
        fields["example"]="\n".join(e["text_de"] for e in examples)
        c=dict(id=builder.stable_id(fields),source_key="synthetic:prep",fields=fields,senses=[dict(sense_id="sense")],examples=examples,provenance={})
        builder.validate_card(c)
        p=self.draft()
        p["words_insert"].append(dict(id=c["id"],source_key=c["source_key"],fields=deepcopy(fields)))
        self.apply(self.bind_draft(p))
        self.assertEqual(self.word(c["id"])["example"],fields["example"])
        c["examples"].pop();fields["example"]="\n".join(e["text_de"] for e in examples)
        with self.assertRaisesRegex(ValueError,"two examples per case"):
            builder.validate_card(c)


    def test_incompatible_full_synonym_union_fails_without_repairing_old_group(self):
        self.conn.execute("INSERT INTO words(id,type,german,infinitive,reflexive_case,russian,ukrainian,english,topic) VALUES('bad-alias','verb','sich helfen','sich helfen','dativ','смысл','значення','meaning','2')")
        gid=self.conn.execute("INSERT INTO synonym_groups DEFAULT VALUES").lastrowid
        self.conn.executemany("INSERT INTO word_synonyms(word_id,group_id) VALUES(?,?)",[("ordinary",gid),("bad-alias",gid)])
        p=self.draft();p["synonym_links"][0]["word_ids"][0]="ordinary"
        before=db_safety._connection_fingerprint(self.conn)
        with self.assertRaisesRegex(ValueError,"full synonym group"):
            self.bind_draft(p)
        self.assertEqual(db_safety._connection_fingerprint(self.conn),before)


    def test_wrong_schema_is_refused_without_migration(self):
        p=self.draft(); a=self.approval(p)
        self.conn.execute("PRAGMA user_version=17")
        before=self.state()
        with self.assertRaisesRegex(ValueError,"schema22"):
            self.bind_draft(p,a)
        self.assertEqual(self.state(),before)

    def test_retired_v2_v3_database_entrypoints_refuse_schema22_without_writes(self):
        from bot.common import import_package_v2 as v2, import_package_v3 as v3
        before=self.state()
        for module in (v2,v3):
            with self.subTest(module=module.__name__), self.assertRaisesRegex(ValueError,"retired"):
                module.bind_package(self.conn,{}, {})
            self.assertEqual(self.state(),before)

if __name__ == "__main__":
    unittest.main()
