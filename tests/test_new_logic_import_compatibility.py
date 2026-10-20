"""K11 source compatibility on retained copies of synthetic SQLite only."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from _telegram_stub import install
install()

from bot import storage
from bot.app_config import PREPOSITION_TYPES
from bot.common.conjugation import parse_conjugation
from bot.conjugation.domain import forms_for
from bot.handlers.add_words import _ai_prompt, _normalize_ai_entry
from bot.handlers.clarify_fields import _set_entry_field
from bot.study import repository
from bot.ui.keyboards import _preposition_type_markup

STAND = Path('/Users/wf/py/_agent_work/888_german_dictionary/new_logic_import_compatibility')
PRESENT = 'ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen'
PAST = 'ich ging\ndu gingst\ner/sie/es ging\nwir gingen\nihr gingt\nsie/Sie gingen'


class ImportCompatibilityTest(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='compat-', dir=STAND))
        self.source, self.db = str(self.folder / 'source.sqlite'), str(self.folder / 'copy.sqlite')
        env = patch.dict(os.environ, {'DB_PATH': self.db, 'OWNER_ID': '0'})
        env.start(); self.addCleanup(env.stop)
        storage.init_db(self.source)
        storage.add_word(self.source, {'id': 'old', 'type': 'verb', 'infinitive': 'laufen',
            'german': 'laufen', 'russian': 'бежать', 'ukrainian': 'бігти', 'english': 'run',
            'topic': 'first', 'cefr_level': 'B1', 'is_separable': 0,
            'image': 'old-image', 'example': 'old-example'})
        storage.set_word_conjugation(self.source, 'old', PRESENT, PAST)
        # Existing paused aliases remain dictionary data, not deletion targets.
        for word in ('unterhalten', 'erinnern'):
            storage.add_word(self.source, {'id': 'old-' + word, 'type': 'verb', 'infinitive': word,
                                          'german': word, 'is_separable': 0})
        with repository.transaction(self.source) as conn:
            conn.executemany("INSERT INTO users(user_id,status) VALUES(?,'approved')", [(1,), (2,)])
            for uid, mode in ((1, 'ru_de'), (2, 'de_en')):
                repository.set_progress(conn, uid, 'old', mode,
                    {'interval': 17, 'repetitions': 4, 'ease': 2.9, 'due': '2099-01-01'})
                conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES(?,?,?,0)",
                             (uid, 'old', mode))
            conn.execute("INSERT INTO user_word_media(user_id,word_id,image,example,russian) VALUES(1,'old','personal-image','personal-example','личный')")
            conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(1,'synthetic','keep')")
        with closing(sqlite3.connect(self.source)) as source, closing(sqlite3.connect(self.db)) as target:
            source.backup(target)

    def rows(self, table, db=None):
        with closing(sqlite3.connect(db or self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def dump(self, db=None):
        with closing(sqlite3.connect(db or self.db)) as conn:
            return tuple(conn.iterdump())

    def verb(self, ident='new', **fields):
        return {'id': ident, 'type': 'verb', 'infinitive': 'gehen', 'german': 'gehen',
                'is_separable': 0, 'conjugation_praesens': PRESENT,
                'conjugation_praeteritum': PAST, **fields}

    def test_nomen_bitte_klasse_coexist_with_other_parts_of_speech_and_keep_first_topic(self):
        for word, other in (('bitte', 'adverb'), ('klasse', 'adjektiv')):
            with self.subTest(word=word):
                storage.add_word(self.db, {'id': word + '-other', 'type': other, 'german': word, 'topic': 'first'})
                storage.add_word(self.db, {'id': word + '-noun', 'type': ' NOMEN ', 'german': ' ' + word + ' ',
                    'article': 'die', 'topic': 'first', 'russian': 'существительное'})
                before = self.dump()
                for typ in ('nomen', other):
                    with self.assertRaises(ValueError):
                        storage.add_word(self.db, {'id': word + '-duplicate', 'type': typ.upper(),
                                                  'german': word.upper(), 'topic': 'second'})
                self.assertEqual(self.dump(), before)
                rows = {row['id']: row for row in self.rows('words')}
                self.assertEqual(rows[word + '-noun']['german'], word.capitalize())
                self.assertEqual(rows[word + '-noun']['type'], 'nomen')
                self.assertEqual(rows[word + '-other']['type'], other)
                self.assertEqual(rows[word + '-noun']['topic'], 'first')

    def test_reverse_pos_order_and_verb_infinitive_identity_are_preserved(self):
        storage.add_word(self.db, {'id': 'noun-first', 'type': 'nomen', 'german': 'Bitte', 'article': 'die'})
        storage.add_word(self.db, {'id': 'adverb-next', 'type': 'adverb', 'german': 'bitte'})
        # A noun spelling can also be a verb's display alias; verb identity remains infinitive.
        storage.add_word(self.db, self.verb('verb-alias', german='Bitte'))
        before = self.dump()
        with self.assertRaises(ValueError):
            storage.add_word(self.db, self.verb('verb-duplicate', infinitive=' GEHEN ', german='other alias'))
        self.assertEqual(self.dump(), before)

    def test_find_existing_word_keeps_its_broad_lookup_contract(self):
        storage.add_word(self.db, {'id': 'adverb-first', 'type': 'adverb', 'german': 'bitte'})
        original = storage.find_existing_word(self.db, ' BITTE ')
        storage.add_word(self.db, {'id': 'noun-second', 'type': 'nomen', 'german': 'Bitte', 'article': 'die'})
        self.assertEqual(storage.find_existing_word(self.db, ' BITTE ')['id'], original['id'])

    def test_final_uses_existing_semantic_triple_without_merging_other_meanings(self):
        for ident, category, case in (('purpose', 'final', 'akkusativ'), ('time', 'temporal', 'akkusativ'),
                                      ('other-case', 'final', 'dativ')):
            storage.add_word(self.db, {'id': ident, 'type': 'praeposition', 'german': 'um',
                                      'preposition_type': category, 'case_type': case})
        storage.add_word(self.db, {'id': 'homograph', 'type': 'adverb', 'german': 'um'})
        before = self.dump()
        with self.assertRaises(ValueError):
            storage.add_word(self.db, {'id': 'duplicate', 'type': 'praeposition', 'german': ' UM ',
                                      'preposition_type': ' FINAL ', 'case_type': ' AKKUSATIV '})
        self.assertEqual(self.dump(), before)
        self.assertEqual(len([row for row in self.rows('words') if row['german'] == 'um']), 4)

    def test_final_reuses_existing_ai_clarify_prompt_and_preset_validation(self):
        self.assertIn('final', PREPOSITION_TYPES)
        prompt = _ai_prompt('um', '')
        self.assertIn(', '.join(PREPOSITION_TYPES), prompt)
        normalized = _normalize_ai_entry({'type': 'praeposition', 'german': 'um',
            'russian': 'чтобы', 'preposition_type': 'Final', 'case_type': 'Akkusativ'}, 'first')
        self.assertEqual((normalized['preposition_type'], normalized['case_type']), ('final', 'akkusativ'))
        changed, error = _set_entry_field(normalized, 'preposition_type=final')
        self.assertIsNone(error)
        self.assertEqual(changed['preposition_type'], 'final')
        markup = _preposition_type_markup()
        rows = getattr(markup, 'rows', None) or getattr(markup, 'keyboard', ())
        self.assertIn('final', [getattr(button, 'text', str(button)).lower() for row in rows for button in row])

    def test_add_word_stores_canonical_six_plus_six_for_all_verbs_without_extending_test_whitelist(self):
        for ident, infinitive, separable in (('outside-nine', 'gehen', 0), ('inside-nine', 'haben', 0),
                                            ('separable', 'aufstehen', 1)):
            with self.subTest(verb=infinitive):
                saved = storage.add_word(self.db, self.verb(ident, infinitive=infinitive, german=infinitive,
                    is_separable=separable, conjugation_praesens='\n ' + PRESENT.replace(' ', '   ') + ' \n'))
                entry = storage.get_word_by_id(self.db, ident)
                self.assertEqual((entry['conjugation_praesens'], entry['conjugation_praeteritum']), (PRESENT, PAST))
                self.assertEqual(saved['conjugation_praesens'], PRESENT)
                self.assertEqual(entry['is_separable'], separable)
                self.assertIsNotNone(forms_for(entry, 'praesens'))
                self.assertEqual(forms_for(entry, 'praeteritum') is not None, infinitive == 'haben')

    def test_invalid_either_table_is_atomic_even_with_an_existing_id(self):
        malformed = (PAST.rsplit('\n', 1)[0], PAST.replace('du gingst', 'ich gingst'),
                     PAST.replace('ich ging', 'ich', 1))
        before = self.dump()
        for bad in malformed:
            for field in ('conjugation_praesens', 'conjugation_praeteritum'):
                with self.subTest(field=field, invalid=bad):
                    for ident in ('new-invalid', 'old'):
                        with self.assertRaises(ValueError):
                            storage.add_word(self.db, self.verb(ident, **{field: bad}))
                    self.assertEqual(self.dump(), before)

    def test_invalid_table_does_not_create_a_database_before_validation(self):
        new_path = self.folder / 'invalid.sqlite'
        with self.assertRaises(ValueError):
            storage.add_word(str(new_path), self.verb(conjugation_praeteritum='incomplete'))
        self.assertFalse(new_path.exists())

    def test_sql_failure_rolls_back_the_entire_word_and_retains_user_progress(self):
        with repository.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER synthetic_insert_failure BEFORE INSERT ON words
                WHEN NEW.id='fail' BEGIN SELECT RAISE(ABORT,'synthetic insert failure'); END""")
        before = self.dump()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic insert failure'):
            storage.add_word(self.db, self.verb('fail'))
        self.assertEqual(self.dump(), before)

    def test_copy_write_and_repeated_duplicate_leave_source_existing_tables_and_progress_unchanged(self):
        source = self.dump(self.source)
        old_words = self.rows('words')
        tables = ('progress', 'quiz_events', 'user_word_media', 'user_settings', 'conjugation_admissions')
        baseline = {table: self.rows(table) for table in tables}
        storage.add_word(self.db, self.verb('new-complete', topic='second', cefr_level='A2'))
        # Omitted tables on another new word do not clear any existing reference data.
        storage.add_word(self.db, {'id': 'new-omitted', 'type': 'verb', 'infinitive': 'bleiben', 'german': 'bleiben'})
        complete = self.dump()
        with self.assertRaises(ValueError):
            storage.add_word(self.db, {'id': 'old', 'type': 'verb', 'infinitive': 'laufen', 'german': 'laufen'})
        with self.assertRaises(ValueError):
            storage.add_word(self.db, self.verb('new-complete', topic='third'))
        self.assertEqual(self.dump(), complete)
        current = {row['id']: row for row in self.rows('words')}
        self.assertTrue(all(current[row['id']] == row for row in old_words))
        self.assertEqual({table: self.rows(table) for table in tables}, baseline)
        self.assertEqual(self.dump(self.source), source)
        self.assertEqual(storage.get_word_by_id(self.db, 'new-omitted')['conjugation_praesens'], '')
        self.assertEqual(storage.get_word_by_id(self.db, 'new-omitted')['conjugation_praeteritum'], '')


if __name__ == '__main__':
    unittest.main()
