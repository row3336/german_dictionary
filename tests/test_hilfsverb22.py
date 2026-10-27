"""Schema22 scalar words.hilfsverb: migration, writers and pure helpers."""
import copy
import json
import os
import sqlite3
import unittest
import uuid
from pathlib import Path

from bot import storage
from bot.common import db_safety, hilfsverb, perfekt
from bot.common.hilfsverb import HilfsverbValidationError, MatchRules
from bot.study import repository

STAND = Path(os.environ.get('GD_TEST_STAND', '/Users/wf/py/_agent_work/888_german_dictionary/data18_7_20261025/foundation'))


def variant(lemma='treffen', pr='traf', p2='getroffen', aux='haben'):
    finite = {'haben': 'hat', 'sein': 'ist'}[aux]
    return dict(variant_id=lemma + ':' + pr + ':' + aux, sense_id=lemma + ':sense', sense_ru='смысл',
                lemma_for_answer=lemma, reflexive_case='none', auxiliary=aux, finite_auxiliary=finite,
                person_scope='third_singular_reference', prateritum=pr, partizip2=p2,
                perfekt_fragment=finite + ' ' + p2, evidence_pointer='synthetic:reference',
                examples=[{'text_de': 'Ein Beispiel.', 'status': 'historical_draft'}])


def legacy_row(*variants, wid='one'):
    lemma = variants[0]['lemma_for_answer']
    return dict(id=wid, type='verb', infinitive=lemma, german=lemma, reflexive_case='none',
                **perfekt.scalar_projection(variants),
                perfekt_variants_json=json.dumps({'version': 1, 'status': 'verified', 'variants': list(variants)}))


def new_db():
    STAND.mkdir(parents=True, exist_ok=True)
    path = STAND / ('hilfsverb22-' + uuid.uuid4().hex + '.sqlite')
    storage.init_db(path)
    return path


def downgrade_to_21(db, rows):
    """Recreate the retired schema21 shape and load legacy rows."""
    with repository.transaction(db) as conn:
        for action in ('insert', 'update'):
            conn.execute('DROP TRIGGER IF EXISTS words_hilfsverb_type_' + action)
        conn.execute('ALTER TABLE words DROP COLUMN hilfsverb')
        conn.execute('ALTER TABLE words ADD COLUMN perfekt_variants_json TEXT DEFAULT NULL')
        for row in rows:
            conn.execute('INSERT INTO words(' + ','.join(row) + ') VALUES(' + ','.join('?' * len(row)) + ')',
                         list(row.values()))
        conn.execute('PRAGMA user_version=21')


class LegacyConversionTest(unittest.TestCase):
    def test_one_p2_both_aux_shared_pr_is_wechsel(self):
        row = legacy_row(variant(aux='haben', pr='fuhr', p2='gefahren'), variant(aux='sein', pr='fuhr', p2='gefahren'))
        assert perfekt.legacy_hilfsverb(row) == 'wechsel'

    def test_single_aux_values(self):
        assert perfekt.legacy_hilfsverb(legacy_row(variant())) == 'hat'
        assert perfekt.legacy_hilfsverb(legacy_row(variant(aux='sein', pr='ging', p2='gegangen'))) == 'ist'

    def test_wechsel_with_different_pr_per_aux_is_unsupported(self):
        # Pairs (fuhr, gefahren)/(fahrte, gefahren) exist, but each only with one auxiliary.
        row = legacy_row(variant(aux='haben', pr='fuhr', p2='gefahren'), variant(aux='sein', pr='fahrte', p2='gefahren'))
        with self.assertRaisesRegex(perfekt.PerfektValidationError, 'every Präteritum'):
            perfekt.legacy_hilfsverb(row)

    def test_different_p2_and_aux_need_separate_cards(self):
        row = legacy_row(variant(aux='haben', pr='hängte', p2='gehängt'), variant(aux='sein', pr='hing', p2='gehangen'))
        with self.assertRaisesRegex(perfekt.PerfektValidationError, 'separate cards'):
            perfekt.legacy_hilfsverb(row)

    def test_coherent_pairs_kept_positionally_without_cartesian(self):
        row = legacy_row(variant('hängen', 'hängte', 'gehängt'), variant('hängen', 'hing', 'gehangen'))
        assert perfekt.legacy_hilfsverb(row) == 'hat'
        entry = {**row, 'hilfsverb': 'hat'}
        assert hilfsverb.form_pairs(entry) == (('hängte', 'gehängt'), ('hing', 'gehangen'))
        short, _ = hilfsverb.answer_candidates(entry)
        assert 'hängen hat gehängt' in short and 'hängen hat gehangen' in short
        assert 'hängen hat gehangen' in short and not any('hängte' in s and 'gehangen' in s for s in short)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.db = new_db()

    def test_migration_preserves_rows_and_sets_scalar(self):
        good = legacy_row(variant(aux='haben', pr='fuhr', p2='gefahren'), variant(aux='sein', pr='fuhr', p2='gefahren'), wid='w')
        plain = dict(id='n', type='verb', infinitive='gehen', german='gehen', reflexive_case='none',
                     prateritum='ging', partizip2='gegangen', example='Ich gehe.')
        downgrade_to_21(self.db, [{**good, 'example': 'Ich fahre.'}, plain])
        with repository.transaction(self.db) as conn:
            before = {r['id']: dict(r) for r in conn.execute('SELECT * FROM words')}
        db_safety._apply_migration_dispatcher(self.db)
        with repository.transaction(self.db) as conn:
            after = {r['id']: dict(r) for r in conn.execute('SELECT * FROM words')}
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 22
        assert 'perfekt_variants_json' not in after['w']
        assert after['w']['hilfsverb'] == 'wechsel'
        assert after['n']['hilfsverb'] is None  # unknown stays NULL, never 'hat'
        for wid, old in before.items():
            kept = {k: v for k, v in old.items() if k != 'perfekt_variants_json'}
            assert {k: after[wid][k] for k in kept} == kept  # id/german/forms/example/updated_at untouched

    def test_unsupported_value_aborts_atomically(self):
        ok = legacy_row(variant(), wid='ok')
        bad = legacy_row(variant('fahren', aux='haben', pr='fuhr', p2='gefahren'), variant('fahren', aux='sein', pr='fahrte', p2='gefahren'), wid='bad')
        downgrade_to_21(self.db, [ok, bad])
        before = db_safety.database_fingerprint(self.db)
        with self.assertRaisesRegex(Exception, '"id": "bad"'):
            db_safety._apply_migration_dispatcher(self.db)
        assert db_safety.database_fingerprint(self.db) == before
        with repository.transaction(self.db) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 21
            assert 'hilfsverb' not in {r[1] for r in conn.execute('PRAGMA table_info(words)')}


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        storage.add_word(self.db, dict(id='v', type='verb', infinitive='fahren', german='fahren', reflexive_case='none',
                                       prateritum='fuhr', partizip2='gefahren', hilfsverb='ist', russian='ехать'))

    def row(self):
        with repository.transaction(self.db) as conn:
            return dict(conn.execute("SELECT * FROM words WHERE id='v'").fetchone())

    def test_retired_key_rejected_before_filtering_with_no_partial_change(self):
        before = self.row()
        with self.assertRaises(HilfsverbValidationError):
            storage.update_word_fields(self.db, 'v', {'russian': 'новое', 'perfekt_variants_json': '{}'})
        assert self.row() == before

    def test_valid_scalar_update_and_invalid_values(self):
        storage.update_word_fields(self.db, 'v', {'hilfsverb': 'wechsel'})
        assert self.row()['hilfsverb'] == 'wechsel'
        before = self.row()
        for bad in ('haben', 'HAT', 1, '', None):
            with self.subTest(bad=bad), self.assertRaises((HilfsverbValidationError, ValueError, sqlite3.IntegrityError)):
                storage.update_word_fields(self.db, 'v', {'hilfsverb': bad})
        assert self.row() == before  # known auxiliary is never discarded

    def test_non_verb_cannot_carry_hilfsverb(self):
        with self.assertRaises((HilfsverbValidationError, ValueError, sqlite3.IntegrityError)):
            storage.add_word(self.db, dict(id='n', type='noun', german='Haus', article='das', hilfsverb='hat', russian='дом'))

    def test_dto_carries_hilfsverb(self):
        assert storage.get_word_by_id(self.db, 'v')['hilfsverb'] == 'ist'


class HelperTest(unittest.TestCase):
    ENTRY = dict(type='verb', infinitive='fahren', reflexive_case='none', prateritum='fuhr', partizip2='gefahren')

    def test_required_aux_unknown_raises_not_bare_fallback(self):
        entry = {**self.ENTRY, 'hilfsverb': None}
        with self.assertRaises(HilfsverbValidationError):
            hilfsverb.answer_candidates(entry, MatchRules(hilfsverb=True))
        with self.assertRaises(HilfsverbValidationError):
            hilfsverb.answer_parts(entry, MatchRules(hilfsverb=True))
        short, _ = hilfsverb.answer_candidates(entry)  # OFF: bare form, no 'hat' inference
        assert short == {'fahren gefahren'}

    def test_required_pr_missing_raises_not_short_fallback(self):
        entry = {**self.ENTRY, 'hilfsverb': 'ist', 'prateritum': ''}
        with self.assertRaises(HilfsverbValidationError):
            hilfsverb.answer_candidates(entry, MatchRules(prateritum=True))
        assert hilfsverb.answer_candidates(entry)[0] == {'fahren ist gefahren', 'fahren gefahren'}

    def test_required_forms_present(self):
        entry = {**self.ENTRY, 'hilfsverb': 'wechsel'}
        short, full = hilfsverb.answer_candidates(entry, MatchRules(prateritum=True, hilfsverb=True))
        assert short == set()
        assert full == {'fahren fuhr hat gefahren', 'fahren fuhr ist gefahren'}
        short, _ = hilfsverb.answer_candidates(entry, MatchRules(hilfsverb=True))
        assert short == {'fahren hat gefahren', 'fahren ist gefahren'}

    def test_validate_entry_strict(self):
        for bad in ({**self.ENTRY, 'type': 'noun', 'hilfsverb': 'hat'}, {**self.ENTRY, 'hilfsverb': 'haben'},
                    {**self.ENTRY, 'hilfsverb': 'hat', 'perfekt_variants_json': '{}'},
                    {**self.ENTRY, 'hilfsverb': 'wechsel', 'partizip2': 'a, b', 'prateritum': 'x, y'}):
            with self.subTest(bad=bad), self.assertRaises(HilfsverbValidationError):
                hilfsverb.validate_entry(bad)
        hilfsverb.validate_entry({**self.ENTRY, 'hilfsverb': None})


if __name__ == '__main__':
    unittest.main()
