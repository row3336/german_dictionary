"""Legacy schema21 envelope validation plus schema22 scalar hilfsverb storage."""
import copy
import json
import os
import sqlite3
import uuid
from pathlib import Path
import unittest
from unittest import mock
from bot import storage
from bot.common import db_safety, hilfsverb, perfekt
from bot.common import existing_user_filters as filters
from bot.search import read_search_entry
from bot.oral.repository import word as oral_word
from bot.study import repository, schema_v21, schema_v22, repair, generations
STAND = Path(os.environ.get('GD_TEST_STAND', '/Users/wf/py/_agent_work/888_german_dictionary/data18_7_20261025/foundation'))

def variant(lemma='treffen', pr='traf', p2='getroffen', aux='haben', case='none', plural=False):
    finite = {'haben': 'haben' if plural else 'hat', 'sein': 'sind' if plural else 'ist'}[aux]
    return dict(variant_id=lemma + ':' + pr, sense_id=lemma + ':sense', sense_ru='смысл', lemma_for_answer=lemma, reflexive_case=case, auxiliary=aux, finite_auxiliary=finite, person_scope='third_plural_reciprocal_reference' if plural else 'third_singular_reference', prateritum=pr, partizip2=p2, perfekt_fragment=finite + (' sich ' if case != 'none' else ' ') + p2, examples=[{'text_de': 'Ein unverändertes Beispiel.', 'status': 'historical_draft'}], evidence_pointer='synthetic:reference')

def entry(*variants, case='none', wid='one'):
    variants = variants or (variant(),)
    return dict(id=wid, type='verb', infinitive=variants[0]['lemma_for_answer'], german=variants[0]['lemma_for_answer'], reflexive_case=case, russian='перевод', **perfekt.scalar_projection(variants), perfekt_variants_json={'version': 1, 'status': 'verified', 'variants': list(variants)})

def word(lemma='treffen', pr='traf', p2='getroffen', wid='one', case='none', **extra):
    """Schema22 card: scalar forms plus words.hilfsverb."""
    return {**dict(id=wid, type='verb', infinitive=lemma, german=lemma, reflexive_case=case, russian='перевод',
                   prateritum=pr, partizip2=p2, hilfsverb='hat'), **extra}

def drop_hilfsverb(conn):
    """Recreate the pre-schema22 words shape."""
    for action in ('insert', 'update'):
        conn.execute('DROP TRIGGER IF EXISTS words_hilfsverb_type_' + action)
    conn.execute('ALTER TABLE words DROP COLUMN hilfsverb')

def new_db():
    STAND.mkdir(parents=True, exist_ok=True)
    path = STAND / ('perfekt-' + uuid.uuid4().hex + '.sqlite')
    storage.init_db(path)
    return path

class LegacyEnvelopeTest(unittest.TestCase):
    """Retired schema21 JSON envelope: the isolated legacy API still validates strictly."""

    def test_linked_tuples_no_cartesian_and_canonical_exact_metadata(self):
        e = entry(variant('hängen', 'hängte', 'gehängt'), variant('hängen', 'hing', 'gehangen'))
        raw = perfekt.serialize_payload(e[perfekt.FIELD], e)
        assert perfekt.serialize_payload(json.loads(raw), e) == raw
        assert json.loads(raw)['variants'][0]['examples'] == e[perfekt.FIELD]['variants'][0]['examples']
        assert perfekt.answer_tuples({**e, perfekt.FIELD: raw}) == (('hängen', 'hängte', 'hat gehängt'), ('hängen', 'hing', 'hat gehangen'))

    def test_verified_forms_do_not_require_variant_examples(self):
        v = variant()
        del v['examples']
        e = entry(v)
        raw = perfekt.serialize_payload(e[perfekt.FIELD], e)
        assert 'examples' not in json.loads(raw)['variants'][0]
        assert perfekt.answer_tuples({**e, perfekt.FIELD: raw}) == (
            ('treffen', 'traf', 'hat getroffen'),)

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


class ScalarStorageTest(unittest.TestCase):
    """Schema22 scalar words.hilfsverb through real storage/migration paths."""

    def setUp(self):
        self.db = new_db()

    def test_add_and_all_named_reader_paths_keep_scalar_hilfsverb(self):
        db = self.db
        added = storage.add_word(db, word())
        assert added['hilfsverb'] == 'hat' and 'perfekt_variants_json' not in added
        assert storage.load_words(db)[0]['hilfsverb'] == 'hat'
        assert storage.list_all_words(db)[0]['hilfsverb'] == 'hat'
        assert storage.list_words_by_topic(db, '1')[0]['hilfsverb'] == 'hat'
        assert storage.get_word_by_id(db, 'one')['hilfsverb'] == 'hat'
        assert storage.find_existing_word(db, 'treffen')['hilfsverb'] == 'hat'
        assert storage.find_word_by_value(db, 'treffen')['hilfsverb'] == 'hat'
        assert read_search_entry(db, 'one')['hilfsverb'] == 'hat'
        with repository.transaction(db) as conn:
            assert oral_word(conn, 1, 'one', 'ru')['hilfsverb'] == 'hat'
            assert storage.learning_pick_new_words(db, 1, 1)[0]['hilfsverb'] == 'hat'

    def test_writer_strict_about_type_and_scalar_and_unknown_stays_null(self):
        for bad in (word(type='noun', article='das', german='Haus', infinitive=''), word(hilfsverb='haben'),
                    word(hilfsverb='hat/ist'), word(hilfsverb=''), word(hilfsverb='wechsel', prateritum='a, b', partizip2='x, y'),
                    word(hilfsverb='hat', reflexive_case=None)):
            with self.subTest(bad=bad), self.assertRaises((hilfsverb.HilfsverbValidationError, ValueError, sqlite3.IntegrityError)):
                storage.add_word(self.db, bad)
        added = storage.add_word(self.db, word(hilfsverb=None))
        assert added['hilfsverb'] is None  # never inferred as 'hat'
        assert storage.get_word_by_id(self.db, 'one')['hilfsverb'] is None

    def test_german_is_not_rewritten_for_auxiliary_updates(self):
        storage.add_word(self.db, word(hilfsverb=None))
        before = storage.get_word_by_id(self.db, 'one')
        storage.update_word_fields(self.db, 'one', {'hilfsverb': 'wechsel'})
        after = storage.get_word_by_id(self.db, 'one')
        assert after['hilfsverb'] == 'wechsel'
        assert {k: v for k, v in after.items() if k != 'hilfsverb'} == {k: v for k, v in before.items() if k != 'hilfsverb'}

    def test_stale_and_bad_updates_are_atomic(self):
        db = self.db
        storage.add_word(db, word())
        before = db_safety.database_fingerprint(db)
        for patch in ({'perfekt_variants_json': None}, {'russian': 'x', 'perfekt_variants_json': '{}'},
                      {'hilfsverb': None}, {'hilfsverb': 'haben'}, {'hilfsverb': 'wechsel', 'prateritum': 'a, b', 'partizip2': 'x, y'},
                      {'russian': 'новый', 'hilfsverb': 'HAT'}):
            with self.subTest(patch=patch), self.assertRaises((hilfsverb.HilfsverbValidationError, ValueError, sqlite3.IntegrityError)):
                storage.update_word_fields(db, 'one', patch)
            assert db_safety.database_fingerprint(db) == before

    def test_guarded_patch_requires_confirmed_classification(self):
        storage.add_word(self.db, word(hilfsverb=None))
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET reflexive_case=NULL WHERE id='one'")
            repository.set_progress(conn, 1, 'one', 'ru_de', dict(interval=9, repetitions=3, ease=2.5, due='2026-11-01'))
            generations.advance_generation(conn, 1, 'one', 0)
            before_progress = [tuple(r) for r in conn.execute('SELECT * FROM progress')]
            before_generations = [tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')]
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            before = db_safety._connection_fingerprint(conn)
            with self.assertRaisesRegex(hilfsverb.HilfsverbValidationError, 'verified reflexive_case'):
                hilfsverb.apply_patch(conn, 'one', {'hilfsverb': 'hat'}, expected_old=old)
            self.assertEqual(db_safety._connection_fingerprint(conn), before)
            self.assertTrue(conn.in_transaction)
            self.assertTrue(hilfsverb.apply_patch(conn, 'one', {'example': 'Ein Beispiel.'}, expected_old=old))
            current = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            self.assertIsNone(current['reflexive_case'])
            self.assertIsNone(current['hilfsverb'])
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM progress')], before_progress)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')], before_generations)
            with self.assertRaisesRegex(hilfsverb.HilfsverbValidationError, 'expected-old'):
                hilfsverb.apply_patch(conn, 'one', {'example': 'Ein Beispiel.'}, expected_old=old)
            self.assertFalse(hilfsverb.apply_patch(conn, 'one', {'example': current['example']}, expected_old=current))
            with self.assertRaises(hilfsverb.HilfsverbValidationError):
                hilfsverb.apply_patch(conn, 'one', {'reflexive_case': 'none'}, expected_old=current)

    def test_explicit_confirmation_then_hilfsverb_preserves_progress_generation(self):
        storage.add_word(self.db, word(hilfsverb=None))
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET reflexive_case=NULL WHERE id='one'")
            repository.set_progress(conn, 1, 'one', 'ru_de', dict(interval=9, repetitions=3, ease=2.5, due='2026-11-01'))
            generations.advance_generation(conn, 1, 'one', 0)
            before_progress = [tuple(r) for r in conn.execute('SELECT * FROM progress')]
            before_generations = [tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')]
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            manifest = dict(schema='gd-verb-confirmation/v1', operation_id='explicit-confirm-before-hilfsverb',
                approvals={'owner': True, 'linguistic': True}, baseline_fingerprint=db_safety._connection_fingerprint(conn),
                entries=[dict(word_id='one', expected_before_sha256=repair.digest(old))])
            manifest['digest'] = repair.manifest_digest(manifest)
            self.assertEqual(repair.confirm_legacy_conn(conn, manifest)['confirmed'], ['one'])
            confirmed = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            self.assertEqual(confirmed['reflexive_case'], 'none')
            self.assertTrue(hilfsverb.apply_patch(conn, 'one', {'hilfsverb': 'hat'}, expected_old=confirmed))
            annotated = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            self.assertEqual(hilfsverb.answer_candidates(annotated)[0], {'treffen hat getroffen', 'treffen getroffen'})
            self.assertEqual({k: v for k, v in annotated.items() if k not in ('hilfsverb', 'updated_at')},
                             {k: v for k, v in confirmed.items() if k not in ('hilfsverb', 'updated_at')})
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM progress')], before_progress)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM study_word_generations')], before_generations)
            self.assertTrue(repair.confirm_legacy_conn(conn, manifest)['replayed'])
            self.assertFalse(hilfsverb.apply_patch(conn, 'one', {'hilfsverb': 'hat'}, expected_old=annotated))

    def test_guarded_patch_validates_complete_candidate(self):
        storage.add_word(self.db, word())
        with repository.transaction(self.db) as conn:
            old = dict(conn.execute("SELECT * FROM words WHERE id='one'").fetchone())
            before = db_safety._connection_fingerprint(conn)
            for patch in ({'hilfsverb': 'haben'}, {'hilfsverb': None}, {'is_separable': 3}, {'reflexive_case': 'dativ'},
                          {'german': 'x'}, {'perfekt_variants_json': None}, {'partizip2': 'a, b', 'prateritum': 'x, y, z'},
                          {'hilfsverb': 'wechsel', 'prateritum': 'a, b', 'partizip2': 'x, y'}):
                with self.subTest(patch=patch), self.assertRaises(hilfsverb.HilfsverbValidationError):
                    hilfsverb.apply_patch(conn, 'one', patch, expected_old={**old, **{k: old.get(k) for k in patch}})
                self.assertEqual(db_safety._connection_fingerprint(conn), before)

    def test_schema20_migration_adds_nullable_scalar_and_preserves_rows(self):
        db = self.db
        storage.add_word(db, word(hilfsverb=None))
        with repository.transaction(db) as conn:
            drop_hilfsverb(conn)
            conn.execute('PRAGMA user_version=20')
            before = dict(conn.execute('SELECT * FROM words').fetchone())
        with self.assertRaises(db_safety.DestructiveMigrationRequired):
            storage.init_db(db)
        report = db_safety._apply_migration_dispatcher(db)
        assert report['to_version'] == 22 and report['healthcheck']['ok']
        with repository.transaction(self.db) as conn:
            after = dict(conn.execute('SELECT * FROM words').fetchone())
            assert after.pop('hilfsverb') is None and 'perfekt_variants_json' not in after and after == before
        assert not db_safety._apply_migration_dispatcher(db)['migrated']

    def test_schema22_completeness_is_checked_before_any_write(self):
        db = self.db
        with repository.transaction(db) as conn:
            drop_hilfsverb(conn)
        before = db_safety.database_fingerprint(db)
        with self.assertRaisesRegex(RuntimeError, 'incomplete schema22: words.hilfsverb'):
            storage.init_db(db)
        assert not db_safety.run_healthcheck(db)['ok']
        assert db_safety.database_fingerprint(db) == before

    def test_retired_column_at_schema22_is_incomplete(self):
        with repository.transaction(self.db) as conn:
            conn.execute('ALTER TABLE words ADD COLUMN perfekt_variants_json TEXT')
        before = db_safety.database_fingerprint(self.db)
        with self.assertRaisesRegex(RuntimeError, 'incomplete schema22: retired'):
            storage.init_db(self.db)
        assert not db_safety.run_healthcheck(self.db)['ok']
        assert db_safety.database_fingerprint(self.db) == before

    def test_schema22_failure_rolls_back_schema_and_version(self):
        db = self.db
        storage.add_word(db, word(hilfsverb=None))
        with repository.transaction(db) as conn:
            drop_hilfsverb(conn)
            conn.execute('ALTER TABLE words ADD COLUMN perfekt_variants_json TEXT DEFAULT NULL')
            conn.execute('PRAGMA user_version=21')
        before = db_safety.database_fingerprint(db)
        original = schema_v22.migrate

        def fail(conn):
            original(conn)
            raise RuntimeError('injected22')
        self.enterContext(mock.patch.object(schema_v22, 'migrate', fail))
        with self.assertRaisesRegex(RuntimeError, 'injected22'):
            db_safety._apply_migration_dispatcher(db)
        assert db_safety.database_fingerprint(db) == before

    def test_schema21_step_failure_rolls_back_schema_and_version(self):
        db = self.db
        with repository.transaction(db) as conn:
            drop_hilfsverb(conn)
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

    def test_confirmation_conn_savepoint_rolls_back_after_dictionary_writes(self):
        storage.add_word(self.db, word(hilfsverb=None))
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
        storage.add_word(self.db, word())
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
            self.assertEqual(conn.execute("SELECT hilfsverb FROM words WHERE id='one'").fetchone()[0], 'hat')

    def test_synonym_and_due_named_readers_keep_hilfsverb(self):
        storage.add_word(self.db, word())
        storage.add_word(self.db, word('finden', 'fand', 'gefunden', wid='two', hilfsverb='wechsel'))
        candidates = storage.list_synonym_candidates_by_ru(self.db, 'one')
        self.assertEqual(candidates[0]['hilfsverb'], 'wechsel')
        self.assertTrue(storage.add_synonym_link(self.db, 'one', 'two')[0])
        group = {e['id']: e for e in storage.get_synonym_group(self.db, 'one')}
        self.assertEqual((group['one']['hilfsverb'], group['two']['hilfsverb']), ('hat', 'wechsel'))
        with repository.transaction(self.db) as conn:
            repository.append_event(conn,1,'one','ru_de',False)
            repository.set_progress(conn,1,'one','ru_de',dict(interval=1,repetitions=2,ease=2.5,due='2000-01-01'))
        self.assertEqual(storage.select_due(self.db,1,'ru_de')[0]['hilfsverb'], 'hat')

    def test_synonym_links_for_normal_and_reflexive_constructions(self):
        storage.add_word(self.db, word('treffen', 'traf', 'getroffen', wid='n1'))
        storage.add_word(self.db, word('finden', 'fand', 'gefunden', wid='n2', hilfsverb=None))
        storage.add_word(self.db, word('sich waschen', 'wusch sich', 'gewaschen', wid='a1', case='akkusativ'))
        storage.add_word(self.db, word('sich duschen', 'duschte sich', 'geduscht', wid='a2', case='akkusativ'))
        storage.add_word(self.db, word('sich vorstellen', 'stellte sich vor', 'vorgestellt', wid='d1', case='dativ'))
        assert storage.add_synonym_link(self.db, 'n1', 'n2')[0]   # normal + unknown auxiliary
        assert storage.add_synonym_link(self.db, 'a1', 'a2')[0]   # AKK + AKK
        assert not storage.add_synonym_link(self.db, 'a1', 'd1')[0]  # AKK + DAT
        assert not storage.add_synonym_link(self.db, 'n1', 'a1')[0]  # normal + reflexive

    def test_frozen_entries_roundtrip_without_requery_and_ignore_retired_key(self):
        storage.add_word(self.db, word(hilfsverb=None))
        with repository.transaction(self.db) as conn:
            frozen_legacy = json.loads(json.dumps(generations.selected_entry(
                conn, 1, storage._row_to_entry(conn.execute("SELECT * FROM words WHERE id='one'").fetchone()))))
            conn.execute("UPDATE words SET hilfsverb='hat' WHERE id='one'")
            frozen_new = json.loads(json.dumps(generations.selected_entry(
                conn, 1, storage._row_to_entry(conn.execute("SELECT * FROM words WHERE id='one'").fetchone()))))
        self.assertIsNone(frozen_legacy['hilfsverb'])
        self.assertEqual(hilfsverb.answer_candidates(frozen_legacy)[0], {'treffen getroffen'})
        self.assertEqual(hilfsverb.answer_candidates(frozen_new)[0], {'treffen hat getroffen', 'treffen getroffen'})
        old_frame = {**frozen_new, 'perfekt_variants_json': '{"ignored": true}'}
        self.assertEqual(hilfsverb.validate_entry(old_frame, allow_legacy_key=True)['hilfsverb'], 'hat')
        with self.assertRaises(hilfsverb.HilfsverbValidationError):
            hilfsverb.validate_entry(old_frame)


if __name__ == '__main__':
    unittest.main()
