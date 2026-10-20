"""Persistent conjugation contracts on SQLite :memory:, without Telegram/cleanup."""

import json
import sqlite3
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from bot.conjugation.domain import check_form
from bot.conjugation.schema import ensure_schema, seed_admissions, admit_if_qualified
from bot.conjugation.service import ConjugationService
from bot.study.srs import update_fsrs
from bot.study.schema_v18 import ensure_schema as ensure_v18

TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 16, tzinfo=timezone.utc)
FORMS = {'ich': 'gehe', 'du': 'gehst', 'er/sie/es': 'geht', 'wir': 'gehen', 'ihr': 'geht', 'sie/Sie': 'gehen'}
TABLE = '\n'.join(p+' '+v for p, v in FORMS.items())


class ConjugationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript('''
            CREATE TABLE users(user_id INTEGER PRIMARY KEY,status TEXT);
            INSERT INTO users VALUES(7,'approved'),(8,'approved'),(9,'pending');
            CREATE TABLE words(id TEXT PRIMARY KEY,type TEXT,german TEXT,infinitive TEXT,
                is_separable INTEGER,conjugation_praesens TEXT,conjugation_praeteritum TEXT);
            CREATE TABLE progress(user_id INTEGER,word_id TEXT,mode TEXT,repetitions INTEGER,
                PRIMARY KEY(user_id,word_id,mode));
            CREATE TABLE conjugation_progress(user_id INTEGER,word_id TEXT,seen_count INTEGER,
                last_seen_at TEXT,PRIMARY KEY(user_id,word_id));
            CREATE TABLE learning_items(user_id INTEGER,word_id TEXT,status TEXT,preview_done INTEGER);
        ''')
        ensure_v18(self.conn)
        self.conn.commit()
        self.seq = 0
        self.service = ConjugationService('synthetic', connect=self.connection)
        self.addCleanup(self.conn.close)

    @contextmanager
    def connection(self, _):
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def delivery(self):
        self.seq += 1
        return self.seq

    def add(self, wid='a', infinitive='gehen', separable=0, pair='ru_de', user=7, reps=2):
        self.conn.execute('INSERT OR IGNORE INTO words VALUES (?,?,?,?,?,?,?)',
                          (wid, 'verb', infinitive, infinitive, separable, TABLE, TABLE))
        self.conn.execute('INSERT INTO progress VALUES (?,?,?,?)', (user, wid, pair, reps))
        seed_admissions(self.conn, NOW.isoformat())
        self.conn.commit()

    def prep(self, pair='ru_de', user=7):
        return self.service.preparation(user, pair, self.delivery(), 'prep')

    def block(self, wid='a', tense='praesens', pair='ru_de', user=7):
        return dict(self.conn.execute('SELECT * FROM conjugation_blocks WHERE user_id=? AND pair=? AND word_id=? AND tense=?',
                                      (user, pair, wid, tense)).fetchone())

    def ready(self, wid='a', tense='praesens', pair='ru_de', user=7, day=None):
        self.prep(pair, user)
        b = self.block(wid, tense, pair, user)
        return self.service.transition(user, pair, wid, tense, b['revision'], b['cycle'], 'ready',
                                       self.delivery(), 'prep', today=day or TODAY-timedelta(days=1), now=NOW)

    def start(self, pair='ru_de', user=7, session='live', day=TODAY):
        return self.service.start_test(user, pair, self.delivery(), session, today=day, now=NOW)

    def grade(self, view, text=None, pair='ru_de', user=7, session='live', day=TODAY, transport=None, key=None):
        q = view['question']
        text = FORMS[q['pronoun']] if text is None else text
        return self.service.submit_form(user, pair, q['pass_id'], q['revision'], q['pronoun'], text,
                                        key or 'form-'+str(self.seq+1), transport or self.delivery(), session,
                                        today=day, now=NOW)

    def last_pass(self, pid):
        return dict(self.conn.execute('SELECT * FROM conjugation_passes WHERE id=?', (pid,)).fetchone())

    def long_interval(self, wid='a'):
        self.conn.execute("UPDATE conjugation_blocks SET interval=30,repetitions=5,ease=2.5,due=? WHERE word_id=? AND tense='praesens'",
                          (TODAY.isoformat(), wid))
        self.conn.commit()

    def test_admission_latches_exact_pair_without_fabricating_view_grades(self):
        self.add()
        self.conn.execute("UPDATE progress SET repetitions=0")
        self.conn.execute("INSERT INTO conjugation_progress VALUES(7,'a',40,'2026-01-01')")
        self.conn.commit()
        self.assertIsNotNone(self.prep()['preparation'])
        self.assertIsNone(self.prep('uk_de')['preparation'])
        self.assertIsNone(self.prep(user=8)['preparation'])
        self.assertEqual(self.block()['phase'], 'preparation')
        self.assertEqual(self.block()['repetitions'], 0)

    def test_schema_and_admission_hooks_do_not_commit(self):
        self.add()
        self.conn.execute('BEGIN IMMEDIATE')
        admit_if_qualified(self.conn, 8, 'a', 'uk_de', 2, NOW.isoformat())
        ensure_schema(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_admissions WHERE user_id=8').fetchone()[0], 0)

    def test_separable_unknown_and_incomplete_tables_are_ineligible(self):
        self.add('a', separable=1)
        self.add('b', separable=None)
        self.add('c')
        self.conn.execute("UPDATE words SET conjugation_praesens='ich gehe' WHERE id='c'")
        self.conn.commit()
        self.assertIsNone(self.prep()['preparation'])

    def test_two_tenses_leave_preparation_independently_and_first_due_tomorrow(self):
        self.add(infinitive='haben')
        first = self.ready(day=TODAY)
        self.assertEqual(self.block()['due'], (TODAY+timedelta(days=1)).isoformat())
        self.assertEqual(first['preparation']['entry']['conjugation_praesens'], '')
        self.assertTrue(first['preparation']['entry']['conjugation_praeteritum'])
        self.assertIsNone(self.start()['question'])
        self.ready(tense='praeteritum', day=TODAY)
        self.assertIsNone(self.prep()['preparation'])
        self.assertEqual(len(self.start()['scheduled']), 2)

    def test_non_nine_praeteritum_is_reference_only_and_is_preserved(self):
        self.add()
        self.assertEqual(len(self.prep()['preparation']['blocks']), 1)
        self.ready()
        self.assertIsNone(self.prep()['preparation'])
        self.assertEqual(self.conn.execute("SELECT conjugation_praeteritum FROM words").fetchone()[0], TABLE)

    def test_six_distinct_groups_give_one_success_divisor_one(self):
        self.add(); self.ready(); self.long_interval()
        baseline = {k: self.block()[k] for k in ('interval', 'repetitions', 'ease', 'due')}
        view = self.start(); groups = []
        for index in range(6):
            groups.append(view['question']['pronoun'])
            view = self.grade(view)
            self.assertEqual(self.block()['repetitions'], 5 if index < 5 else 6)
        self.assertEqual(set(groups), set(FORMS))
        self.assertEqual({k:self.block()[k] for k in baseline}, update_fsrs(baseline, 5, TODAY, 1))
        self.assertIsNone(view['question'])

    def test_first_wrong_is_immediate_and_later_days_do_not_move_its_due(self):
        self.add(); self.ready(); self.long_interval()
        view = self.grade(self.start(), 'wrong')
        after = self.block(); pid = view['last_answer']['pass_id']
        self.assertEqual(after['interval'], 12)
        self.assertEqual(self.last_pass(pid)['status'], 'active')
        view = self.grade(view, 'wrong', day=TODAY+timedelta(days=1))
        self.assertEqual(self.block()['due'], after['due'])
        self.assertEqual(self.block()['repetitions'], after['repetitions'])
        for _ in range(4):
            view = self.grade(view, day=TODAY+timedelta(days=1))
        self.assertEqual(self.last_pass(pid)['status'], 'completed')
        self.assertEqual(self.block()['due'], after['due'])

    def test_correction_restores_baseline_and_clears_result_day_before_next_day_success(self):
        self.add(); self.ready(); self.long_interval()
        baseline = self.block()
        view = self.grade(self.start(), 'typo')
        last = view['last_answer']; pid = last['pass_id']
        fixed = self.service.correct_last(7, 'ru_de', last['id'], 'live', self.delivery(),
                                         current_question=view['question'], today=TODAY, now=NOW)
        self.assertTrue(fixed['fixed'])
        self.assertEqual(self.block()['due'], baseline['due'])
        self.assertIsNone(self.last_pass(pid)['result_day'])
        self.assertIsNone(self.last_pass(pid)['applied_result'])
        view = fixed
        for _ in range(5):
            view = self.grade(view, day=TODAY+timedelta(days=1))
        self.assertEqual(self.last_pass(pid)['result_day'], (TODAY+timedelta(days=1)).isoformat())
        self.assertEqual(self.block()['repetitions'], 6)

    def test_multiple_errors_keep_one_wrong_when_last_error_corrected(self):
        self.add(); self.ready(); self.long_interval()
        view = self.grade(self.start(), 'wrong')
        before = self.block()
        view = self.grade(view, 'wrong')
        fixed = self.service.correct_last(7, 'ru_de', view['last_answer']['id'], 'live', self.delivery(),
                                         current_question=view['question'], today=TODAY, now=NOW)
        self.assertTrue(fixed['fixed'])
        self.assertTrue(fixed['question']['has_error'])
        self.assertEqual(self.block()['due'], before['due'])

    def test_stop_resume_future_due_and_day_wall_close_old_correction(self):
        self.add(); self.ready(); self.long_interval()
        view = self.grade(self.start(), 'wrong'); last = view['last_answer']
        summary = self.service.stop(7, 'ru_de', 'live', self.delivery(), [last['pass_id']])['summary']
        self.assertEqual(summary, {'completed':0, 'incomplete':1})
        resumed = self.start(session='new', day=TODAY+timedelta(days=1))
        self.assertEqual(resumed['question']['pass_id'], last['pass_id'])
        self.assertEqual(resumed['question']['ordinal'], 2)
        self.assertFalse(self.service.correct_last(7, 'ru_de', last['id'], 'live', self.delivery(), today=TODAY)['fixed'])
        newer = self.grade(resumed, 'wrong', session='new', day=TODAY+timedelta(days=1))
        self.assertFalse(self.service.correct_last(7, 'ru_de', newer['last_answer']['id'], 'new', self.delivery(),
                                                  today=TODAY+timedelta(days=2))['fixed'])

    def test_sixth_form_correction_preserves_already_selected_next_block(self):
        self.add('a'); self.add('b'); self.ready('a'); self.ready('b'); self.long_interval('a')
        view = self.start(); pid = view['question']['pass_id']
        for _ in range(5): view = self.grade(view)
        view = self.grade(view, 'wrong')
        next_question = dict(view['question'])
        self.assertEqual(next_question['word_id'], 'b')
        fixed = self.service.correct_last(7, 'ru_de', view['last_answer']['id'], 'live', self.delivery(),
                                         current_question=next_question, today=TODAY, now=NOW)
        self.assertEqual(fixed['question'], next_question)
        self.assertEqual(self.last_pass(pid)['applied_result'], 'correct')
        self.assertEqual(self.block()['repetitions'], 6)

    def test_transport_and_original_action_replay_do_not_answer_next_form(self):
        self.add(); self.ready()
        original = self.start(); delivery = self.delivery(); key = 'original-form'
        view = self.grade(original, transport=delivery, key=key)
        duplicate = self.grade(view, transport=delivery, key='new-question-key')
        self.assertTrue(duplicate['replayed'])
        duplicate = self.grade(view, key=key)
        self.assertTrue(duplicate['replayed'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_answers').fetchone()[0], 1)

    def test_reset_selected_time_preserves_admission_other_time_and_history(self):
        self.add(infinitive='haben'); self.ready(); self.ready(tense='praeteritum')
        view = self.grade(self.start(), 'wrong'); pid = view['last_answer']['pass_id']
        tense = self.last_pass(pid)['tense']; other = 'praesens' if tense == 'praeteritum' else 'praeteritum'
        untouched = self.block(tense=other)
        b = self.block(tense=tense)
        self.service.transition(7, 'ru_de', 'a', tense, b['revision'], b['cycle'], 'reset',
                                self.delivery(), 'live', today=TODAY, now=NOW)
        reset = self.block(tense=tense)
        self.assertEqual((reset['phase'],reset['repetitions'],reset['due'],reset['cycle']), ('preparation',0,None,2))
        self.assertEqual(self.block(tense=other), untouched)
        self.assertEqual(self.last_pass(pid)['status'], 'reset')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_answers').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_admissions').fetchone()[0], 1)
        self.ready(tense=tense, day=TODAY)
        self.assertEqual(self.block(tense=tense)['due'], (TODAY+timedelta(days=1)).isoformat())

    def test_expected_forms_are_snapshotted_and_wrong_explicit_pronoun_rejected(self):
        self.add(); self.ready(); view = self.start()
        self.conn.execute("UPDATE words SET conjugation_praesens=?", (TABLE.replace('gehe','OTHER'),)); self.conn.commit()
        self.assertTrue(self.grade(view)['ok'])
        self.assertTrue(check_form('  ICH   gehe ', 'ich', 'gehe'))
        self.assertTrue(check_form('sie gehen', 'sie/Sie', 'gehen'))
        self.assertTrue(check_form('es geht', 'er/sie/es', 'geht'))
        self.assertFalse(check_form('sie gehen', 'wir', 'gehen'))
        self.assertFalse(check_form('random gehe', 'ich', 'gehe'))

    def test_sql_failure_rolls_back_answer_effect_and_delivery(self):
        self.add(); self.ready(); view = self.start()
        self.conn.execute("CREATE TRIGGER fail_grade BEFORE UPDATE ON conjugation_blocks BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.conn.commit()
        delivery = self.delivery()
        with self.assertRaises(sqlite3.IntegrityError): self.grade(view, 'wrong', transport=delivery)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_answers').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute('SELECT * FROM study_deliveries WHERE transport_update_id=?',(delivery,)).fetchone())
        self.assertEqual(self.last_pass(view['question']['pass_id'])['revision'], 0)

    def test_cross_user_pass_and_pending_user_are_rejected(self):
        self.add(); self.ready(); view = self.start()
        stale = self.grade(view, user=8)
        self.assertTrue(stale['stale'])
        with self.assertRaises(PermissionError): self.prep(user=9)

    def test_empty_generic_undo_closes_correction_right_without_undoing_conjugation(self):
        from bot.study.answers import undo_steps
        self.add(); self.ready(); view = self.grade(self.start(), 'wrong')
        pid, last = view['last_answer']['pass_id'], view['last_answer']['id']
        before = self.block()
        self.conn.execute('CREATE TABLE transaction_log(id INTEGER PRIMARY KEY,user_id INTEGER,payload TEXT)')
        self.conn.commit()
        with patch('bot.study.repository.transaction', self.connection):
            result = undo_steps('synthetic', 7, 1, self.delivery())
        self.assertTrue(result['invalidate_frame'])
        self.assertEqual(result['restored'], 0)
        self.assertEqual(self.block(), before)
        self.assertEqual(self.last_pass(pid)['status'], 'active')
        self.assertIsNone(self.conn.execute('SELECT correction_session_id FROM conjugation_answers WHERE id=?', (last,)).fetchone()[0])
        self.assertFalse(self.service.correct_last(7, 'ru_de', last, 'live', self.delivery(), today=TODAY)['fixed'])

    def test_reset_current_b_closes_last_error_right_in_completed_a(self):
        self.add('a'); self.add('b'); self.ready('a'); self.ready('b')
        view = self.start()
        for _ in range(5): view = self.grade(view)
        view = self.grade(view, 'typo')
        last = view['last_answer']['id']
        b = self.block('b')
        self.service.transition(7, 'ru_de', 'b', 'praesens', b['revision'], b['cycle'], 'reset',
                                self.delivery(), 'live', today=TODAY, now=NOW)
        self.assertFalse(self.service.correct_last(7, 'ru_de', last, 'live', self.delivery(), today=TODAY)['fixed'])
        self.assertEqual(self.block('a')['phase'], 'test')

    def test_language_pairs_have_independent_preparation_schedules_and_admission(self):
        self.add(pair='uk_de'); self.add(pair='en_de')
        self.prep('en_de')
        self.ready(pair='uk_de', day=TODAY)
        self.assertEqual(self.block(pair='uk_de')['phase'], 'test')
        self.assertEqual(self.block(pair='en_de')['phase'], 'preparation')
        self.assertIsNone(self.prep('ru_de')['preparation'])
        self.assertIsNone(self.start(pair='en_de')['question'])
        self.assertIsNone(self.start(pair='uk_de')['question'])
        self.assertIsNotNone(self.start(pair='uk_de', day=TODAY+timedelta(days=1))['question'])

    def test_explicit_pronoun_alternatives_and_stable_six_group_snapshot(self):
        self.assertTrue(check_form('du stündest', 'du', 'ständest / stündest'))
        self.assertTrue(check_form('gehen', 'sie/Sie', 'gehen'))
        self.assertFalse(check_form('wir geht', 'er/sie/es', 'geht'))
        self.assertFalse(check_form('ihr gehen', 'sie/Sie', 'gehen'))
        self.add(); self.ready(); view = self.start()
        pid = view['question']['pass_id']
        order = self.last_pass(pid)['pronoun_order_json']
        view = self.grade(view)
        self.service.stop(7, 'ru_de', 'live', self.delivery(), [pid])
        resumed = self.start(session='new')
        self.assertEqual(self.last_pass(pid)['pronoun_order_json'], order)
        self.assertEqual(resumed['question']['pronoun'], json.loads(order)[1])

    def test_render_inspection_refuses_reclassified_active_word(self):
        self.add(); self.ready(); view = self.start()
        self.conn.execute("UPDATE words SET is_separable=1 WHERE id='a'"); self.conn.commit()
        self.assertNotIn('question', self.service.inspect(7, 'ru_de', view['question']))


if __name__ == '__main__':
    unittest.main()
