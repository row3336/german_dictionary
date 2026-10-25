"""Synthetic schema21 and linked-reference storage acceptance."""
import copy
import json
import os
import sqlite3
import uuid
from pathlib import Path
import unittest
from unittest import mock
from bot import storage
from bot.common import db_safety, perfekt
from bot.common import existing_user_filters as filters
from bot.search import read_search_entry
from bot.oral.repository import word as oral_word
from bot.study import repository, schema_v21, repair, generations
STAND = Path(os.environ.get('GD_TEST_STAND', '/Users/wf/py/_agent_work/888_german_dictionary/data18_7_20261025/foundation'))

def variant(lemma='treffen', pr='traf', p2='getroffen', aux='haben', case='none', plural=False):
    finite = {'haben': 'haben' if plural else 'hat', 'sein': 'sind' if plural else 'ist'}[aux]
    return dict(variant_id=lemma + ':' + pr, sense_id=lemma + ':sense', sense_ru='смысл', lemma_for_answer=lemma, reflexive_case=case, auxiliary=aux, finite_auxiliary=finite, person_scope='third_plural_reciprocal_reference' if plural else 'third_singular_reference', prateritum=pr, partizip2=p2, perfekt_fragment=finite + (' sich ' if case != 'none' else ' ') + p2, examples=[{'text_de': 'Ein unverändertes Beispiel.', 'status': 'historical_draft'}], evidence_pointer='synthetic:reference')

def entry(*variants, case='none', wid='one'):
    variants = variants or (variant(),)
    return dict(id=wid, type='verb', infinitive=variants[0]['lemma_for_answer'], german=variants[0]['lemma_for_answer'], reflexive_case=case, russian='перевод', **perfekt.scalar_projection(variants), perfekt_variants_json={'version': 1, 'status': 'verified', 'variants': list(variants)})

def new_db():
    STAND.mkdir(parents=True, exist_ok=True)
    path = STAND / ('perfekt-' + uuid.uuid4().hex + '.sqlite')
    storage.init_db(path)
    return path

class PerfektStorageTest(unittest.TestCase):

    def setUp(self):
        self.db = new_db()

    def test_linked_tuples_no_cartesian_and_canonical_exact_metadata(self):
        e = entry(variant('hängen', 'hängte', 'gehängt'), variant('hängen', 'hing', 'gehangen'))
        raw = perfekt.serialize_payload(e[perfekt.FIELD], e)
        assert perfekt.serialize_payload(json.loads(raw), e) == raw
        assert json.loads(raw)['variants'][0]['examples'] == e[perfekt.FIELD]['variants'][0]['examples']
        assert perfekt.answer_tuples({**e, perfekt.FIELD: raw}) == (('hängen', 'hängte', 'hat gehängt'), ('hängen', 'hing', 'hat gehangen'))
        assert perfekt.display_forms(e) == (('hängte', 'hat gehängt'), ('hing', 'hat gehangen'))

    def test_corrupt_annotated_is_diagnostic_case_0(self):
        value = ''
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.answer_tuples({**entry(), perfekt.FIELD: value})

    def test_corrupt_annotated_is_diagnostic_case_1(self):
        value = '{'
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.answer_tuples({**entry(), perfekt.FIELD: value})

    def test_corrupt_annotated_is_diagnostic_case_2(self):
        value = 'null'
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.answer_tuples({**entry(), perfekt.FIELD: value})

    def test_corrupt_annotated_is_diagnostic_case_3(self):
        value = '{}'
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.answer_tuples({**entry(), perfekt.FIELD: value})

    def test_corrupt_annotated_is_diagnostic_case_4(self):
        value = '{"version":1,"version":1,"status":"verified","variants":[]}'
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.answer_tuples({**entry(), perfekt.FIELD: value})

    def test_corrupt_annotated_is_diagnostic_case_5(self):
        value = {'version': True, 'status': 'verified', 'variants': []}
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.answer_tuples({**entry(), perfekt.FIELD: value})

    def test_only_null_and_missing_are_legacy(self):
        assert perfekt.answer_tuples({}) is None
        assert perfekt.answer_tuples({perfekt.FIELD: None}) is None

    def test_invalid_variant_rejected_case_0(self):
        change = {'auxiliary': 'werden'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_1(self):
        change = {'finite_auxiliary': 'ist'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_2(self):
        change = {'lemma_for_answer': 'waschen'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_3(self):
        change = {'reflexive_case': 'dativ'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_4(self):
        change = {'perfekt_fragment': 'hat getroffen extra'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_5(self):
        change = {'person_scope': 'first_plural'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_6(self):
        change = {'examples': []}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_7(self):
        change = {'variant_id': ''}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_8(self):
        change = {'auxiliary': []}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_invalid_variant_rejected_case_9(self):
        change = {'partizip2': 'sich getroffen'}
        e = entry()
        e[perfekt.FIELD]['variants'][0].update(change)
        with self.assertRaises(perfekt.PerfektValidationError):
            perfekt.read_variants(e)

    def test_accepted_special_linked_forms_case_0(self):
        v, case = (variant('sich kennenlernen', 'lernten sich kennen', 'kennengelernt', case='akkusativ', plural=True), 'akkusativ')
        e = entry(v, case=case)
        assert perfekt.answer_tuples(e) == ((v['lemma_for_answer'], v['prateritum'], v['perfekt_fragment']),)

    def test_accepted_special_linked_forms_case_1(self):
        v, case = (variant('sich begegnen', 'begegneten sich', 'begegnet', aux='sein', case='dativ', plural=True), 'dativ')
        e = entry(v, case=case)
        assert perfekt.answer_tuples(e) == ((v['lemma_for_answer'], v['prateritum'], v['perfekt_fragment']),)

    def test_accepted_special_linked_forms_case_2(self):
        v, case = ({**variant('geboren werden', 'wurde geboren', 'geboren worden', aux='sein'), 'partizip2_kind': 'passive_perfekt_complement'}, 'none')
        e = entry(v, case=case)
        assert perfekt.answer_tuples(e) == ((v['lemma_for_answer'], v['prateritum'], v['perfekt_fragment']),)

    def test_accepted_special_linked_forms_case_3(self):
        v, case = ({**variant('sich scheiden lassen', 'ließ sich scheiden', 'scheiden lassen', case='akkusativ'), 'partizip2_kind': 'ersatzinfinitiv_construction_complement'}, 'akkusativ')
        e = entry(v, case=case)
        assert perfekt.answer_tuples(e) == ((v['lemma_for_answer'], v['prateritum'], v['perfekt_fragment']),)

    def test_accepted_special_linked_forms_case_4(self):
        v, case = (variant('möchten', 'möchtete', 'gemöchtet'), 'none')
        e = entry(v, case=case)
        assert perfekt.answer_tuples(e) == ((v['lemma_for_answer'], v['prateritum'], v['perfekt_fragment']),)

    def test_add_and_all_named_reader_paths_keep_canonical_annotation(self):
        db = self.db
        added = storage.add_word(db, entry())
        raw = added[perfekt.FIELD]
        assert isinstance(raw, str)
        assert storage.load_words(db)[0][perfekt.FIELD] == raw
        assert storage.list_all_words(db)[0][perfekt.FIELD] == raw
        assert storage.list_words_by_topic(db, '1')[0][perfekt.FIELD] == raw
        assert storage.get_word_by_id(db, 'one')[perfekt.FIELD] == raw
        assert storage.find_existing_word(db, 'treffen')[perfekt.FIELD] == raw
        assert storage.find_word_by_value(db, 'treffen')[perfekt.FIELD] == raw
        assert read_search_entry(db, 'one')[perfekt.FIELD] == raw
        with repository.transaction(db) as conn:
            assert oral_word(conn, 1, 'one', 'ru')[perfekt.FIELD] == raw
            assert storage.learning_pick_new_words(db, 1, 1)[0][perfekt.FIELD] == raw

    def test_verified_payload_rejects_unknown_classification(self):
        for classification in (None, 'missing'):
            candidate = entry()
            if classification == 'missing':
                candidate.pop('reflexive_case')
            else:
                candidate['reflexive_case'] = classification
            with self.subTest(classification=classification):
                with self.assertRaisesRegex(perfekt.PerfektValidationError, 'verified reflexive_case'):
                    perfekt.normalize_payload(candidate[perfekt.FIELD], candidate)
                with self.assertRaises(perfekt.PerfektValidationError):
                    perfekt.read_variants(candidate)
                candidate[perfekt.FIELD] = None
                self.assertIsNone(perfekt.read_variants(candidate))

    def test_metadata_guard_rejects_annotation_until_classification_confirmed(self):
        storage.add_word(self.db, {**entry(), perfekt.FIELD: None})
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET reflexive_case=NULL WHERE id='one'")
            repository.set_progress(conn, 1, 'one', 'ru_de', dict(interval=9, repetitions=3, ease=2.5, due='2026-11-01'))
            generations.advance_generation(conn, 1, 'one', 0)
            before_progress = [tuple(r) for r in conn.execute('SELECT * FROM progress')]
            before_generations = [tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')]
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            before = db_safety._connection_fingerprint(conn)
            with self.assertRaisesRegex(perfekt.PerfektValidationError, 'verified reflexive_case'):
                perfekt.apply_metadata_patch(conn, 'one', {perfekt.FIELD: entry()[perfekt.FIELD]}, expected_old=old)
            self.assertEqual(db_safety._connection_fingerprint(conn), before)
            self.assertTrue(conn.in_transaction)
            self.assertTrue(perfekt.apply_metadata_patch(conn, 'one', {'russian': 'уточнённый перевод'}, expected_old=old))
            current = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            self.assertIsNone(current['reflexive_case'])
            self.assertIsNone(current[perfekt.FIELD])
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM progress')], before_progress)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')], before_generations)
            with self.assertRaisesRegex(perfekt.PerfektValidationError, 'expected-old'):
                perfekt.apply_metadata_patch(conn, 'one', {'russian': 'уточнённый перевод'}, expected_old=old)
            self.assertFalse(perfekt.apply_metadata_patch(conn, 'one', {'russian': current['russian']}, expected_old=current))
            with self.assertRaises(perfekt.PerfektValidationError):
                perfekt.apply_metadata_patch(conn, 'one', {'reflexive_case': 'none'}, expected_old=current)

    def test_explicit_confirmation_then_annotation_preserves_progress_generation(self):
        storage.add_word(self.db, {**entry(), perfekt.FIELD: None})
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET reflexive_case=NULL WHERE id='one'")
            repository.set_progress(conn, 1, 'one', 'ru_de', dict(interval=9, repetitions=3, ease=2.5, due='2026-11-01'))
            generations.advance_generation(conn, 1, 'one', 0)
            before_progress = [tuple(r) for r in conn.execute('SELECT * FROM progress')]
            before_generations = [tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')]
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            manifest = dict(schema='gd-verb-confirmation/v1', operation_id='explicit-confirm-before-annotation',
                approvals={'owner': True, 'linguistic': True}, baseline_fingerprint=db_safety._connection_fingerprint(conn),
                entries=[dict(word_id='one', expected_before_sha256=repair.digest(old))])
            manifest['digest'] = repair.manifest_digest(manifest)
            self.assertEqual(repair.confirm_legacy_conn(conn, manifest)['confirmed'], ['one'])
            confirmed = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            self.assertEqual(confirmed['reflexive_case'], 'none')
            self.assertTrue(perfekt.apply_metadata_patch(conn, 'one', {perfekt.FIELD: entry()[perfekt.FIELD]}, expected_old=confirmed))
            annotated = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            self.assertEqual(perfekt.answer_tuples(annotated), (('treffen', 'traf', 'hat getroffen'),))
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM progress')], before_progress)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')], before_generations)
            self.assertTrue(repair.confirm_legacy_conn(conn, manifest)['replayed'])
            self.assertFalse(perfekt.apply_metadata_patch(conn, 'one', {perfekt.FIELD: annotated[perfekt.FIELD]}, expected_old=annotated))

    def test_stale_scalar_and_annotation_drop_refused_atomically(self):
        db = self.db
        storage.add_word(db, entry())
        before = db_safety.database_fingerprint(db)
        for patch in ({'partizip2': 'geworfen'}, {'prateritum': 'warf'}, {perfekt.FIELD: None}):
            with self.assertRaises(perfekt.PerfektValidationError):
                storage.update_word_fields(db, 'one', patch)
            assert db_safety.database_fingerprint(db) == before

    def test_schema20_migration_is_nullable_and_preserves_existing_rows(self):
        db = self.db
        storage.add_word(db, {**entry(), perfekt.FIELD: None})
        with repository.transaction(db) as conn:
            conn.execute('ALTER TABLE words DROP COLUMN perfekt_variants_json')
            conn.execute('PRAGMA user_version=20')
            before = dict(conn.execute('SELECT * FROM words').fetchone())
        with self.assertRaises(db_safety.DestructiveMigrationRequired):
            storage.init_db(db)
        report = db_safety._apply_migration_dispatcher(db)
        assert report['to_version'] == 21 and report['healthcheck']['ok']
        with repository.transaction(db) as conn:
            after = dict(conn.execute('SELECT * FROM words').fetchone())
            assert after.pop(perfekt.FIELD) is None and after == before
        assert not db_safety._apply_migration_dispatcher(db)['migrated']

    def test_schema21_completeness_is_checked_before_any_write(self):
        db = self.db
        with repository.transaction(db) as conn:
            conn.execute('ALTER TABLE words DROP COLUMN perfekt_variants_json')
        before = db_safety.database_fingerprint(db)
        with self.assertRaisesRegex(RuntimeError, 'incomplete schema21: words.perfekt_variants_json'):
            storage.init_db(db)
        assert not db_safety.run_healthcheck(db)['ok']
        assert db_safety.database_fingerprint(db) == before

    def test_schema21_failure_rolls_back_schema_and_version(self):
        db = self.db
        with repository.transaction(db) as conn:
            conn.execute('ALTER TABLE words DROP COLUMN perfekt_variants_json')
            conn.execute('PRAGMA user_version=20')
        before = db_safety.database_fingerprint(db)
        original = schema_v21.migrate

        def fail(conn):
            original(conn)
            raise RuntimeError('injected21')
        self.enterContext(mock.patch.object(schema_v21, 'migrate', fail))
        with self.assertRaisesRegex(RuntimeError, 'injected21'):
            db_safety._apply_migration_dispatcher(db)
        assert db_safety.database_fingerprint(db) == before

    def test_metadata_writer_validates_complete_unannotated_candidate(self):
        storage.add_word(self.db, {**entry(), perfekt.FIELD: None})
        with repository.transaction(self.db) as conn:
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            before = db_safety._connection_fingerprint(conn)
            for patch in ({"conjugation_praesens": "ich treffe"}, {"is_separable": 3},
                          {"cefr_level": "C3"}, {"preposition_type": "invalid"}):
                with self.subTest(patch=patch):
                    with self.assertRaises(perfekt.PerfektValidationError):
                        perfekt.apply_metadata_patch(conn, 'one', patch, expected_old=old)
                    self.assertEqual(db_safety._connection_fingerprint(conn), before)

    def test_confirmation_conn_savepoint_rolls_back_after_dictionary_writes(self):
        storage.add_word(self.db, {**entry(), perfekt.FIELD: None})
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET reflexive_case=NULL WHERE id='one'")
            conn.execute("CREATE TRIGGER reject_confirmation BEFORE INSERT ON study_reset_operations BEGIN SELECT RAISE(ABORT,'injected receipt'); END")
            conn.execute("INSERT INTO app_settings(key,value) VALUES('outer-marker','keep')")
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            manifest = dict(schema='gd-verb-confirmation/v1', operation_id='synthetic-confirm',
                approvals={'owner':True, 'linguistic':True}, baseline_fingerprint=db_safety._connection_fingerprint(conn),
                entries=[dict(word_id='one', expected_before_sha256=repair.digest(old))])
            manifest['digest'] = repair.manifest_digest(manifest)
            before = db_safety._connection_fingerprint(conn)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected receipt'):
                repair.confirm_legacy_conn(conn, manifest)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(db_safety._connection_fingerprint(conn), before)
            conn.execute('DROP TRIGGER reject_confirmation')
            manifest['baseline_fingerprint'] = db_safety._connection_fingerprint(conn)
            manifest['digest'] = repair.manifest_digest(manifest)
            self.assertEqual(repair.confirm_legacy_conn(conn, manifest)['confirmed'], ['one'])
            self.assertTrue(repair.confirm_legacy_conn(conn, manifest)['replayed'])
            self.assertEqual(conn.execute("SELECT value FROM app_settings WHERE key='outer-marker'").fetchone()[0], 'keep')

    def test_reset_conn_keeps_approval_gate_and_savepoint_after_progress_changes(self):
        storage.add_word(self.db, entry())
        with repository.transaction(self.db) as conn:
            repository.set_progress(conn, 1, 'one', 'ru_de', dict(interval=9,repetitions=3,ease=2.5,due='2026-11-01'))
            conn.execute("CREATE TRIGGER reject_reset BEFORE INSERT ON study_reset_operations BEGIN SELECT RAISE(ABORT,'injected reset'); END")
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            manifest = dict(schema='gd-study-repair/v1', operation_id='synthetic-reset', reset_profile='relearn_word',
                approvals={'owner':True,'linguistic':True}, baseline_fingerprint=db_safety._connection_fingerprint(conn),
                targets=[dict(word_id='one', old_identity=['verb','treffen','none'], new_identity=['verb','treffen','none'],
                    replacement={'russian':'новый перевод'}, expected_before_sha256=repair.digest(old),
                    users=[dict(user_id=1,expected_generation=0,expected_rows_sha256=repair.digest(repair.state_rows(conn,1,'one')))])])
            manifest['digest'] = repair.manifest_digest(manifest)
            before = db_safety._connection_fingerprint(conn)
            with self.assertRaises(PermissionError):
                repair.apply_reset_conn(conn, manifest)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected reset'):
                repair.apply_reset_conn(conn, manifest, integration_verified=True)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(db_safety._connection_fingerprint(conn), before)
            conn.execute('DROP TRIGGER reject_reset')
            manifest['baseline_fingerprint'] = db_safety._connection_fingerprint(conn)
            manifest['digest'] = repair.manifest_digest(manifest)
            self.assertFalse(repair.apply_reset_conn(conn, manifest, integration_verified=True)['replayed'])
            self.assertEqual(generations.generation_for(conn,1,'one'), 1)
            self.assertTrue(repair.apply_reset_conn(conn, manifest, integration_verified=True)['replayed'])


    def test_synonym_and_due_named_readers_keep_annotation(self):
        first = storage.add_word(self.db, entry())
        second = storage.add_word(self.db, entry(variant('finden','fand','gefunden'),wid='two'))
        candidates = storage.list_synonym_candidates_by_ru(self.db, 'one')
        self.assertEqual(candidates[0][perfekt.FIELD], second[perfekt.FIELD])
        storage.add_synonym_link(self.db, 'one', 'two')
        group = {e['id']:e for e in storage.get_synonym_group(self.db, 'one')}
        self.assertEqual(group['one'][perfekt.FIELD], first[perfekt.FIELD])
        self.assertEqual(group['two'][perfekt.FIELD], second[perfekt.FIELD])
        with repository.transaction(self.db) as conn:
            repository.append_event(conn,1,'one','ru_de',False)
            repository.set_progress(conn,1,'one','ru_de',dict(interval=1,repetitions=2,ease=2.5,due='2000-01-01'))
        due = storage.select_due(self.db,1,'ru_de')
        self.assertEqual(due[0][perfekt.FIELD], first[perfekt.FIELD])

    def test_frozen_legacy_and_annotated_entries_roundtrip_without_requery(self):
        storage.add_word(self.db, {**entry(),perfekt.FIELD:None})
        with repository.transaction(self.db) as conn:
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            frozen_legacy = generations.selected_entry(conn,1,storage._row_to_entry(conn.execute("SELECT * FROM words WHERE id='one'").fetchone()))
            frozen_legacy = json.loads(json.dumps(frozen_legacy))
            perfekt.apply_metadata_patch(conn,'one',{perfekt.FIELD:entry()[perfekt.FIELD]},expected_old=old)
            frozen_new = generations.selected_entry(conn,1,storage._row_to_entry(conn.execute("SELECT * FROM words WHERE id='one'").fetchone()))
            frozen_new = json.loads(json.dumps(frozen_new))
        self.assertIsNone(perfekt.answer_tuples(frozen_legacy))
        self.assertEqual(perfekt.answer_tuples(frozen_new),(('treffen','traf','hat getroffen'),))
        self.assertIsNone(perfekt.answer_tuples({k:v for k,v in frozen_legacy.items() if k!=perfekt.FIELD}))

if __name__ == '__main__':
    unittest.main()
