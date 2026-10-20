"""Oral cycle and real shared receipts on synthetic in-memory SQLite only."""

import asyncio
import ast
import subprocess
import sys
import textwrap
from pathlib import Path
import copy
import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests._telegram_stub import install
install()

from bot.app_config import MENU, ORAL_QUIZ, TESTS_MENU
from bot.common import quiz_resume
from bot.common.study_lock import invalidate_study_frames
from bot.i18n import t
from bot.oral import domain, handlers as H, repository as R, view
from bot.oral.service import OralService, accepted_frame
from bot.study import answers
from bot.study.schema_v18 import ensure_schema
from bot.study.srs import update_fsrs

DAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
BASE = {'interval': 90, 'repetitions': 8, 'ease': 3.1, 'due': DAY.isoformat()}


class Message:
    def __init__(self, text=''):
        self.text, self.sent, self.fail_once = text, [], False

    async def reply_text(self, text, **kwargs):
        await asyncio.sleep(0)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('synthetic send failure')
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))


class OralTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE users(user_id INTEGER PRIMARY KEY,status TEXT);
            INSERT INTO users VALUES(7,'approved'),(8,'approved'),(9,'pending');
            CREATE TABLE words(id TEXT PRIMARY KEY,type TEXT,german TEXT,article TEXT,plural TEXT,
                infinitive TEXT DEFAULT '',prateritum TEXT DEFAULT '',partizip2 TEXT DEFAULT '',
                russian TEXT,ukrainian TEXT,english TEXT,image TEXT DEFAULT '',example TEXT DEFAULT '',
                topic TEXT DEFAULT '1',cefr_level TEXT DEFAULT 'A1',created_at TEXT DEFAULT 'before',
                is_separable INTEGER DEFAULT 0,conjugation_praesens TEXT DEFAULT '',conjugation_praeteritum TEXT DEFAULT '');
            CREATE TABLE progress(user_id INTEGER,word_id TEXT,mode TEXT,interval INTEGER,
                repetitions INTEGER,ease REAL,due TEXT,updated_at TEXT,PRIMARY KEY(user_id,word_id,mode));
            CREATE TABLE quiz_events(id INTEGER PRIMARY KEY,user_id INTEGER,word_id TEXT,mode TEXT,
                is_correct INTEGER,prev_is_correct INTEGER,created_at TEXT);
            CREATE TABLE transaction_log(id INTEGER PRIMARY KEY,user_id INTEGER,action TEXT,
                payload TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE learning_items(user_id INTEGER,word_id TEXT,status TEXT,preview_done INTEGER,
                batch_id TEXT NOT NULL DEFAULT 'batch',next_due TEXT NOT NULL DEFAULT '2026-10-20',
                updated_at TEXT DEFAULT 'before',PRIMARY KEY(user_id,word_id));
            CREATE TABLE learning_batches(id TEXT,user_id INTEGER,updated_at TEXT DEFAULT 'before',
                ready_since TEXT,status TEXT DEFAULT 'completed',PRIMARY KEY(id,user_id));
            INSERT INTO learning_batches(id,user_id) VALUES('batch',7),('batch',8);
            CREATE TABLE user_word_media(user_id INTEGER,word_id TEXT,image TEXT DEFAULT '',
                example TEXT DEFAULT '',russian TEXT DEFAULT '',ukrainian TEXT DEFAULT '',english TEXT DEFAULT '',
                PRIMARY KEY(user_id,word_id));
        ''')
        ensure_schema(self.conn)
        self.conn.commit()
        self.options = {'gap': 3, 'limit': 4, 'topics': None, 'cefr_levels': None, 'dict_lang': 'ru'}
        self.serial = 0
        self.service = OralService('synthetic')
        self.ctx = SimpleNamespace(user_data={})
        self.enterContext(patch('bot.study.repository.transaction', self.transaction))
        self.enterContext(patch.object(R.random, 'shuffle', side_effect=lambda values: None))
        self.enterContext(patch.object(R.random, 'randint', return_value=2))
        self.enterContext(patch.object(view, 'ReplyKeyboardMarkup', side_effect=lambda rows, **kw: {'keyboard': rows, **kw}))
        self.enterContext(patch.object(H, '_service', return_value=self.service))
        self.enterContext(patch.object(H, '_options', side_effect=lambda _: self.options.copy()))
        self.enterContext(patch.object(H, '_user_interface_lang', return_value='en'))
        self.enterContext(patch.object(H, '_mode_de_l1', return_value='de_ru'))
        self.enterContext(patch.object(H, '_back_to_main_menu', new=AsyncMock(return_value=MENU)))
        self.enterContext(patch.object(H, '_back_to_tests', new=AsyncMock(return_value=TESTS_MENU)))
        start, transition = self.service.start, self.service.transition
        self.enterContext(patch.object(self.service, 'start', side_effect=lambda *a, **k: start(*a, **k, today=DAY)))
        self.enterContext(patch.object(self.service, 'transition', side_effect=lambda *a, **k: transition(*a, **k, today=DAY, now=NOW)))

    @contextmanager
    def transaction(self, _):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def add(self, *ids, kind='nomen', eligible=True):
        for wid in ids:
            self.conn.execute('''INSERT INTO words(id,type,german,article,plural,russian,ukrainian,english,example)
                VALUES(?,?,?,'der','Hunde','собака','пес','dog','One example.\nSecond example.')''',
                (wid, kind, 'Guten Tag' if kind == 'phrase' else wid.upper()))
            if eligible:
                for uid in (7, 8):
                    self.conn.execute("INSERT INTO learning_items(user_id,word_id,status,preview_done) VALUES(?,?,'completed',1)", (uid, wid))
        self.conn.commit()

    def rows(self, table):
        return [dict(row) for row in self.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def frame(self):
        return self.ctx.user_data[H.FRAME]

    def update(self, key='', ident=None, user=7):
        self.serial += 1
        text = t('en', key) if key in ('oral_next', 'oral_correct', 'oral_wrong', 'stop', 'clarify_fields', 'back') else key
        return SimpleNamespace(update_id=ident or self.serial, message=Message(text),
            effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=user))

    def call(self, key='', update=None):
        return asyncio.run(H.oral_quiz(update or self.update(key), self.ctx))

    def start(self, mode='de_ru', update=None):
        return asyncio.run(H.start_oral_quiz(update or self.update(), self.ctx, mode))

    def grade(self, correct=True, update=None):
        return self.call('oral_correct' if correct else 'oral_wrong', update)

    def baseline(self, wid='a', mode='de_ru', user=7):
        self.conn.execute('INSERT INTO progress VALUES(?,?,?,?,?,?,?,?)',
            (user, wid, mode, BASE['interval'], BASE['repetitions'], BASE['ease'], BASE['due'], 'before'))
        self.conn.commit()

    def srs(self, wid='a', mode='de_ru', user=7):
        row = self.conn.execute('SELECT * FROM progress WHERE user_id=? AND word_id=? AND mode=?',
                                (user, wid, mode)).fetchone()
        return {key: row[key] for key in BASE} if row else None

    def test_cycle_reveals_a_with_reserved_b_and_last_b_requires_a_grade(self):
        self.add('a', 'b'); self.start()
        self.assertEqual(self.frame()['phase'], 'initial_hidden')
        self.assertEqual(self.frame()['hidden']['word_id'], 'a')
        self.assertIsNone(self.frame()['pending'])
        self.call('oral_next')
        self.assertEqual(self.frame()['pending']['word_id'], 'a')
        self.assertEqual(self.frame()['hidden']['word_id'], 'b')
        self.assertEqual(self.rows('quiz_events'), [])
        self.grade()
        self.assertEqual(self.frame()['pending']['word_id'], 'b')
        self.assertIsNone(self.frame()['hidden'])
        self.assertEqual(self.frame()['phase'], 'grading')
        self.grade()
        self.assertEqual(self.frame()['phase'], 'completed')
        self.assertEqual(domain.summary(self.frame()), {'count': 2, 'correct': 2, 'wrong': 0, 'accuracy': '100%'})
        self.assertEqual(len(self.rows('quiz_events')), 2)

    def test_singleton_wrong_drains_four_presentations_regardless_of_retry_results(self):
        self.add('a'); self.baseline(); self.start(); self.call('oral_next'); self.grade(False)
        expected = update_fsrs(BASE, 2, DAY, 2)
        for number in range(1, 5):
            self.assertTrue(self.frame()['pending']['retry'])
            self.assertEqual(self.frame()['pending']['presentation'], number)
            self.assertEqual(self.frame()['phase'], 'grading')
            self.grade(number % 2 == 0)
            self.assertEqual(self.srs(), expected)
            self.assertEqual(len(self.rows('quiz_events')), 1)
            self.assertEqual(len(self.rows('transaction_log')), 1)
        self.assertEqual(self.frame()['phase'], 'completed')
        self.assertEqual(domain.summary(self.frame())['count'], 1)
        self.assertEqual(domain.summary(self.frame())['wrong'], 1)

    def test_gap_is_correct_original_only_and_shown_b_is_never_replaced(self):
        self.add('a', 'b', 'c', 'd', 'e', 'f')
        self.start(); self.call('oral_next'); self.grade(False)
        for pending, hidden in (('b', 'c'), ('c', 'd'), ('d', 'e')):
            self.assertEqual(self.frame()['pending']['word_id'], pending)
            self.assertEqual(self.frame()['hidden']['word_id'], hidden)
            self.grade()
        self.assertEqual(self.frame()['pending']['word_id'], 'e')
        self.assertEqual(self.frame()['hidden']['word_id'], 'a')
        self.assertTrue(self.frame()['hidden']['retry'])
        self.assertEqual(self.frame()['retry_counts']['a'], 1)
        reserved = copy.deepcopy(self.frame()['hidden'])
        self.grade()
        self.assertEqual(self.frame()['pending'], reserved)
        self.assertEqual(self.frame()['hidden']['word_id'], 'f')
        self.assertEqual(self.frame()['since_retry'], 1)
        self.grade()
        self.assertEqual(self.frame()['since_retry'], 1)
        self.assertEqual(len(self.rows('quiz_events')), 5)

    def test_dynamic_settings_apply_per_operation_and_do_not_revoke_reserved_slot(self):
        self.add('a'); self.start(); self.call('oral_next'); self.grade(False)
        self.assertEqual(self.frame()['pending']['presentation'], 1)
        self.options['limit'] = 1
        self.grade()
        self.assertEqual(self.frame()['phase'], 'completed')
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_initial_invalid_buttons_and_text_cannot_be_replayed_as_a_grade(self):
        self.add('a', 'b'); self.start(); old = self.update('oral_correct')
        self.call(update=old); self.call('typed answer'); self.call('oral_next')
        before = copy.deepcopy(self.frame())
        self.call(update=old)
        self.assertEqual(self.frame(), before)
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(self.rows('transaction_log'), [])

    def test_original_correct_changes_only_exact_mode_with_divisor_two(self):
        self.add('a', 'p', kind='phrase')
        self.conn.execute("UPDATE words SET type='nomen' WHERE id='a'"); self.conn.commit()
        for lang in ('ru', 'uk', 'en'):
            self.options['dict_lang'] = lang
            for mode, wid in ((f'de_{lang}', 'a'), (f'phrase_de_{lang}', 'p'), (f'phrase_{lang}_de', 'p')):
                self.ctx.user_data.clear(); self.baseline(wid, mode); self.start(mode)
                self.call('oral_next'); self.grade()
                self.assertEqual(self.srs(wid, mode), update_fsrs(BASE, 5, DAY, 2))
                self.assertEqual(self.rows('quiz_events')[-1]['mode'], mode)
        self.assertEqual(len(self.rows('quiz_events')), 9)
        with self.assertRaises(ValueError): self.start('ru_de')

    def test_sql_next_media_failure_rolls_back_srs_event_receipt_and_frame(self):
        self.add('a', 'b', 'c'); self.start(); self.call('oral_next'); self.baseline()
        before = copy.deepcopy(self.ctx.user_data); ledgers = self.rows('study_deliveries')
        update = self.update('oral_correct')
        with patch.object(R, 'word', side_effect=sqlite3.OperationalError('media read')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'media read'): self.grade(update=update)
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(self.srs(), BASE)
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(self.rows('study_deliveries'), ledgers)
        self.grade(update=update)
        self.assertEqual(self.frame()['pending']['word_id'], 'b')
        self.assertEqual(self.frame()['hidden']['word_id'], 'c')
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_send_failure_replay_and_new_delivery_drain_do_not_grade_unshown_b(self):
        self.add('a', 'b', 'c'); self.start(); self.call('oral_next')
        old = self.update('oral_correct'); old.message.fail_once = True
        with self.assertRaises(RuntimeError): self.grade(update=old)
        reserved = copy.deepcopy(self.frame()['hidden'])
        newer = self.update('oral_correct'); self.grade(update=newer)
        self.assertEqual(self.frame()['pending']['word_id'], 'b')
        self.assertEqual(self.frame()['hidden'], reserved)
        self.assertEqual(len(self.rows('quiz_events')), 1)
        before = copy.deepcopy(self.ctx.user_data)
        self.grade(update=old); self.grade(update=newer)
        self.assertEqual(self.ctx.user_data, before)
        self.assertFalse(self.frame()['pending_render'])

    def test_same_delivery_concurrency_commits_once_and_distinct_grade_a_then_b(self):
        self.add('a', 'b', 'c'); self.start(); self.call('oral_next')
        first = self.update('oral_correct')
        async def duplicate():
            await asyncio.gather(H.oral_quiz(first, self.ctx), H.oral_quiz(first, self.ctx))
        asyncio.run(duplicate())
        self.assertEqual(len(self.rows('quiz_events')), 1)
        second, third = self.update('oral_correct'), self.update('oral_correct')
        async def distinct():
            await asyncio.gather(H.oral_quiz(second, self.ctx), H.oral_quiz(third, self.ctx))
        asyncio.run(distinct())
        self.assertEqual([r['word_id'] for r in self.rows('quiz_events')], ['a', 'b', 'c'])
        self.assertEqual(self.frame()['phase'], 'completed')

    def test_delivery_commit_failure_rolls_back_proposed_next_frame_and_effects(self):
        self.add('a', 'b', 'c'); self.start(); self.call('oral_next'); self.baseline()
        before = copy.deepcopy(self.ctx.user_data)
        self.conn.execute("""CREATE TRIGGER oral_fail_delivery BEFORE UPDATE ON study_deliveries
            WHEN OLD.action_kind='answer' BEGIN SELECT RAISE(ABORT,'delivery failed'); END""")
        self.conn.commit(); update = self.update('oral_correct')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'delivery failed'):
            self.grade(update=update)
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(self.srs(), BASE)
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(self.rows('transaction_log'), [])
        self.conn.execute('DROP TRIGGER oral_fail_delivery'); self.conn.commit()
        self.grade(update=update)
        self.assertEqual(self.frame()['pending']['word_id'], 'b')
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_dynamic_gap_affects_next_unshown_slot_without_replacing_shown_c(self):
        self.add('a', 'b', 'c', 'd'); self.start(); self.call('oral_next'); self.grade(False)
        reserved = copy.deepcopy(self.frame()['hidden'])
        self.assertEqual(reserved['word_id'], 'c')
        self.options['gap'] = 1
        self.grade()
        self.assertEqual(self.frame()['pending'], reserved)
        self.assertEqual(self.frame()['hidden']['word_id'], 'a')
        self.assertEqual(self.frame()['hidden']['presentation'], 1)

    def test_missing_retry_is_discarded_before_next_eligible_retry(self):
        frame = domain.new_frame('de_ru')
        frame.update(retry_queue=['missing', 'a'], retry_counts={'missing': 0, 'a': 0}, since_retry=3)
        candidate = domain.choose(frame, lambda *a: None,
            lambda wid: {'id': 'a'} if wid == 'a' else None, 3)
        self.assertEqual(candidate['word_id'], 'a')
        self.assertEqual(candidate['presentation'], 1)
        self.assertNotIn('missing', frame['retry_counts'])
        self.assertEqual(frame['retry_queue'], [])

    def test_initial_send_failure_replay_does_not_choose_another_hidden_question(self):
        self.add('a', 'b'); update = self.update(); update.message.fail_once = True
        with self.assertRaises(RuntimeError): self.start(update=update)
        reserved = copy.deepcopy(self.frame()['hidden'])
        with patch.object(R, 'pick_original', side_effect=AssertionError('second selection')):
            self.start(update=update)
        self.assertEqual(self.frame()['hidden'], reserved)
        self.assertFalse(self.frame()['pending_render'])
        self.assertEqual(self.rows('quiz_events'), [])
        # Restart has no live frame: the durable receipt cannot resurrect it.
        invalidate_study_frames(self.ctx)
        self.assertIsNone(self.start(update=update))
        self.assertNotIn(H.FRAME, self.ctx.user_data)

    def test_new_oral_entry_clears_old_transients_only_after_its_commit(self):
        self.add('a', 'b'); self.baseline(mode='ru_de')
        self.ctx.user_data.update(quiz_written_frame={'flow_id': 'old-written'},
            quiz_entry={'id': 'old-written-word'},
            retry_queue={'ru_de': ['old-written-word'], 'phrase_ru_de': ['old-phrase']},
            retry_counts={'ru_de': {'old-written-word': 2}},
            fsrs_cycle_queue={'ru_de': [{'id': 'old-written-word'}]},
            quiz_published_view={'caller_kind': 'written', 'flow_id': 'old-written'})
        before = copy.deepcopy(self.ctx.user_data); update = self.update()
        with patch.object(R, 'due', side_effect=sqlite3.OperationalError('entry plan')):
            with self.assertRaises(sqlite3.OperationalError): self.start(update=update)
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(self.srs(mode='ru_de'), BASE)
        self.start(update=update)
        self.assertNotIn('quiz_written_frame', self.ctx.user_data)
        self.assertNotIn('retry_queue', self.ctx.user_data)
        self.assertNotIn('retry_counts', self.ctx.user_data)
        self.assertNotIn('fsrs_cycle_queue', self.ctx.user_data)
        self.assertNotIn('quiz_entry', self.ctx.user_data)
        self.assertEqual(self.frame()['retry_queue'], [])
        self.assertEqual(self.frame()['correct'], 0)
        self.assertEqual(self.srs(mode='ru_de'), BASE)
        self.assertEqual(self.ctx.user_data['quiz_mode'], 'de_ru')

    def test_initial_sql_failure_creates_no_frame_or_delivery_and_retries_once(self):
        self.add('a'); update = self.update()
        with patch.object(R, 'due', side_effect=sqlite3.OperationalError('entry read')):
            with self.assertRaises(sqlite3.OperationalError): self.start(update=update)
        self.assertNotIn(H.FRAME, self.ctx.user_data)
        self.assertEqual(self.rows('study_deliveries'), [])
        self.start(update=update)
        self.assertEqual(self.frame()['hidden']['word_id'], 'a')
        self.assertEqual(len(self.rows('study_deliveries')), 1)

    def test_old_entry_and_grade_delivery_never_reset_new_flow_or_read_settings(self):
        self.add('a', 'b'); old_entry = self.update(); self.start(update=old_entry)
        self.call('oral_next'); old_grade = self.update('oral_correct'); self.grade(update=old_grade)
        self.start(); before = copy.deepcopy(self.ctx.user_data)
        with patch.object(H, '_options', side_effect=AssertionError('late settings read')):
            self.assertIsNone(self.start(update=old_entry))
            self.assertIsNone(self.grade(update=old_grade))
        self.assertEqual(self.ctx.user_data, before)

    def test_fresh_recovery_closes_flow_and_late_recovery_does_not_close_new_flow(self):
        self.add('a', 'b'); self.start()
        self.conn.execute("""INSERT INTO study_deliveries(user_id,transport_update_id,flow_id,
            action_kind,state,result_json) VALUES(7,9000,'undo','undo','processing',?)""",
            (json.dumps({'tx_ids': [], 'cursor': 0, 'outcomes': []}),))
        self.conn.commit(); old = self.update('oral_next')
        self.assertEqual(self.call(update=old), MENU)
        self.assertNotIn(H.FRAME, self.ctx.user_data)
        self.start(); before = copy.deepcopy(self.ctx.user_data)
        self.assertIsNone(self.call(update=old))
        self.assertEqual(self.ctx.user_data, before)

    def test_serialized_entry_preflights_before_menu_mutates_mode_or_frame(self):
        self.add('a', 'b'); calls = []
        @H.serialized_oral_entry
        async def menu_entry(update, context):
            calls.append(True)
            context.user_data['quiz_mode'] = 'de_ru'
            return await H.start_oral_quiz_locked(update, context, 'de_ru')
        update = self.update(); update.message.fail_once = True
        with self.assertRaises(RuntimeError): asyncio.run(menu_entry(update, self.ctx))
        reserved = copy.deepcopy(self.frame()['hidden'])
        asyncio.run(menu_entry(update, self.ctx))
        self.assertEqual(calls, [True])
        self.assertEqual(self.frame()['hidden'], reserved)
        self.assertEqual(self.rows('quiz_events'), [])

    def test_deleted_only_visible_card_completes_without_grade_or_resurrection(self):
        self.add('a'); self.start(); self.call('oral_next')
        quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        self.conn.execute("DELETE FROM words WHERE id='a'"); self.conn.commit()
        asyncio.run(quiz_resume.resume_after_delete(self.update(), self.ctx, 'a'))
        self.assertEqual(self.frame()['phase'], 'completed')
        self.assertIsNone(self.frame()['pending'])
        self.assertIsNone(self.frame()['hidden'])
        self.assertIsNone(self.frame()['last_visible'])
        self.assertEqual(domain.summary(self.frame())['count'], 0)
        self.assertEqual(self.rows('quiz_events'), [])
        quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        asyncio.run(quiz_resume.render_existing_frame(self.update(), self.ctx))
        self.assertEqual(self.frame()['phase'], 'completed')

    def test_stop_and_empty_summary_count_only_accepted_originals(self):
        self.start(); self.assertIn('Accuracy: —', self.frame()['view'][0])
        self.add('a', 'b')
        for reveal in (False, True):
            self.start()
            if reveal: self.call('oral_next')
            self.call('stop')
            self.assertEqual(domain.summary(self.frame())['count'], 0)
        self.start(); self.call('oral_next'); self.grade(); self.call('stop')
        self.assertEqual(domain.summary(self.frame())['count'], 1)
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_generic_undo_restores_original_and_old_grade_never_resurrects(self):
        self.add('a', 'b'); self.start(); self.call('oral_next'); self.baseline(); self.baseline(user=8)
        old = self.update('oral_wrong'); self.grade(update=old)
        result = answers.undo_steps('synthetic', 7, 1, 9000)
        self.assertEqual(result['restored'], 1)
        self.assertEqual(self.srs(), BASE); self.assertEqual(self.srs(user=8), BASE)
        self.assertEqual(self.rows('quiz_events'), [])
        invalidate_study_frames(self.ctx)
        self.assertIsNone(self.grade(update=old))
        self.assertNotIn(H.FRAME, self.ctx.user_data)
        self.assertEqual(self.rows('quiz_events'), [])

    def test_clarify_return_targets_visible_a_and_never_picks_counts_or_spends_retry(self):
        self.add('a', 'b'); self.start(); self.call('oral_next')
        point = quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        self.assertEqual(point.visible_answer_id, 'a')
        self.assertEqual(point.current_question_id, 'b')
        before = domain.copy_frame(self.frame())
        with patch.object(R, 'pick_original', side_effect=AssertionError('return picker')):
            asyncio.run(quiz_resume.render_existing_frame(self.update(), self.ctx))
        self.assertEqual(domain.copy_frame(self.frame()), before)
        self.assertEqual(self.rows('quiz_events'), [])
        self.grade(); self.grade()
        quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        with patch.object(R, 'pick_original', side_effect=AssertionError('completed picker')):
            asyncio.run(quiz_resume.render_existing_frame(self.update(), self.ctx))
        self.assertEqual(self.frame()['phase'], 'completed')
        self.assertIsNone(self.frame()['pending'])

    def test_failed_clarify_presentation_preserves_refresh_prefix_and_late_replay(self):
        self.add('a', 'b')
        for boundary in ('refresh', 'format', 'send'):
            for same_delivery in (True, False):
                with self.subTest(boundary=boundary, same_delivery=same_delivery):
                    self.ctx.user_data.clear(); self.start(); self.call('oral_next')
                    quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
                    before = domain.copy_frame(self.frame())
                    translation = 'FRESH ' + boundary
                    self.conn.execute('UPDATE words SET russian=? WHERE id=?', (translation, 'a'))
                    self.conn.commit(); old = self.update('back')
                    if boundary == 'send':
                        old.message.fail_once = True
                        with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'):
                            asyncio.run(quiz_resume.render_existing_frame(old, self.ctx, 'Saved prefix'))
                    else:
                        owner, name = (self.service, 'refresh') if boundary == 'refresh' else (view, 'message')
                        with patch.object(owner, name, side_effect=sqlite3.OperationalError('presentation read')):
                            with self.assertRaisesRegex(sqlite3.OperationalError, 'presentation read'):
                                asyncio.run(quiz_resume.render_existing_frame(old, self.ctx, 'Saved prefix'))
                    self.assertTrue(self.frame()['pending_render'])
                    drain = old if same_delivery else self.update('oral_correct')
                    with patch.object(R, 'pick_original', side_effect=AssertionError('refresh picker')):
                        self.call(update=drain)
                    shown = drain.message.sent[-1][0]
                    self.assertIn(translation, shown)
                    self.assertTrue(shown.startswith('Saved prefix'))
                    self.assertFalse(self.frame()['pending_render'])
                    self.assertEqual(domain.copy_frame(self.frame()), before)
                    self.assertEqual(self.rows('quiz_events'), [])
                    self.assertEqual(self.rows('transaction_log'), [])
                    self.start(); newer = copy.deepcopy(self.ctx.user_data)
                    self.assertIsNone(self.call(update=drain))
                    self.assertEqual(self.ctx.user_data, newer)

    def test_stale_oral_return_delete_and_failed_delivery_leave_new_foreign_flow(self):
        self.add('a', 'b'); self.start(); self.call('oral_next')
        point = quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        old = self.update('oral_correct'); old.message.fail_once = True
        with self.assertRaises(RuntimeError): self.grade(update=old)
        # Keep the old live data deliberately: published caller is authoritative.
        quiz_resume.publish_view(self.ctx, caller_kind='written', mode='ru_de',
            phase='question', flow_id='new-written', current_question_id='b', view_revision=10)
        before = copy.deepcopy(self.ctx.user_data)
        self.assertIsNone(asyncio.run(H.render_existing_oral(self.update(), self.ctx, point)))
        self.assertIsNone(asyncio.run(H.delete_from_oral(self.update(), self.ctx, point, 'a')))
        self.assertIsNone(self.grade(update=old))
        self.assertIsNone(self.grade())
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_explicit_delete_visible_or_hidden_preserves_unanswered_remainder(self):
        self.add('a', 'b', 'c'); self.start(); self.call('oral_next')
        old_b = copy.deepcopy(self.frame()['hidden'])
        quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        self.conn.execute("DELETE FROM words WHERE id='a'"); self.conn.commit()
        asyncio.run(quiz_resume.resume_after_delete(self.update(), self.ctx, 'a'))
        self.assertEqual(self.frame()['pending'], old_b)
        self.assertEqual(self.frame()['hidden']['word_id'], 'c')
        pending = copy.deepcopy(self.frame()['pending'])
        quiz_resume.capture_return_point(self.ctx, ORAL_QUIZ)
        self.conn.execute("DELETE FROM words WHERE id='c'"); self.conn.commit()
        asyncio.run(quiz_resume.resume_after_delete(self.update(), self.ctx, 'c'))
        self.assertEqual(self.frame()['pending'], pending)
        self.assertIsNone(self.frame()['hidden'])
        self.assertEqual(self.rows('quiz_events'), [])

    def test_repository_filters_and_active_learning_exclusion(self):
        self.add('a', 'b', 'c', 'p', eligible=False)
        self.conn.execute("UPDATE words SET type='phrase' WHERE id='p'")
        self.conn.execute("UPDATE words SET topic='2',cefr_level='B1' WHERE id='b'")
        self.conn.execute("INSERT INTO learning_items(user_id,word_id,status,preview_done) VALUES(7,'a','completed',1),(7,'b','skipped',1),(7,'c','active',1)")
        self.conn.execute("INSERT INTO progress VALUES(7,'p','phrase_de_ru',1,1,2.5,'2026-10-20','before')")
        self.conn.commit()
        self.assertEqual([r['id'] for r in R.due(self.conn, 7, 'de_ru', self.options, DAY)], ['a', 'b'])
        self.assertEqual([r['id'] for r in R.due(self.conn, 7, 'de_ru', {**self.options, 'topics': ['2']}, DAY)], ['b'])
        self.assertEqual([r['id'] for r in R.due(self.conn, 7, 'de_ru', {**self.options, 'cefr_levels': ['A1']}, DAY)], ['a'])
        self.assertEqual([r['id'] for r in R.due(self.conn, 7, 'phrase_de_ru', self.options, DAY)], ['p'])

    def test_localized_question_retains_gender_example_and_phrase_direction(self):
        self.add('a')
        for article, emoji in (('der', '🔵'), ('die', '🔴'), ('das', '🟢')):
            self.conn.execute('UPDATE words SET article=?', (article,)); self.conn.commit()
            entry = R.word(self.conn, 7, 'a', 'ru')
            self.assertIn(emoji, view.question(entry, 'de_ru'))
            self.assertIn('One example.', view.question(entry, 'de_ru'))
            self.assertNotIn('Second example.', view.question(entry, 'de_ru'))
        self.add('p', kind='phrase')
        entry = R.word(self.conn, 7, 'p', 'en')
        self.assertIn('dog', view.question(entry, 'phrase_en_de'))
        self.assertIn('Guten Tag', view.answer(entry, 'phrase_en_de'))
        for lang in ('ru', 'uk', 'en'):
            initial = domain.new_frame('de_'+lang)
            labels = str(view.markup(initial, lang))
            self.assertNotIn(t(lang, 'clarify_fields'), labels)
            self.assertIn(t(lang, 'oral_next'), labels)

    def test_guest_guard_and_user_isolation_before_slot_resolution(self):
        self.add('a'); self.start(); self.call('oral_next')
        before = copy.deepcopy(self.ctx.user_data)
        with self.assertRaises(PermissionError): self.grade(update=self.update('oral_correct', user=9))
        self.assertEqual(self.ctx.user_data, before)
        other = self.service.start(8, 'de_ru', 500, self.options)
        other = self.service.transition(8, accepted_frame(other), 'next', 501, self.options)
        self.service.transition(8, accepted_frame(other), 'correct', 502, self.options)
        self.assertIsNone(self.srs(user=7))
        self.assertIsNotNone(self.srs(user=8))


class OralDispatcherTests(unittest.TestCase):
    def test_real_ptb_entry_and_unshown_grade_recovery_without_network(self):
        source = Path(__file__).with_name('test_new_logic_core_conversation.py').read_text()
        raw = next(node.value for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and 'class NoNetworkRequest' in node.value)
        prefix = textwrap.dedent(raw).split('class FakeService:')[0]
        script = prefix + textwrap.dedent(r'''
            from telegram import ReplyKeyboardMarkup
            from telegram.ext import filters
            from tests.test_new_logic_oral import OralTests
            from bot.oral import handlers as oral, view

            def message(bot, text, ident):
                result = Message(ident, NOW, Chat(UID, 'private'), from_user=User(UID, 'Learner', False), text=text)
                result.set_bot(bot)
                update = Update(1000+ident, message=result)
                update.set_bot(bot)
                return update

            async def scenario():
                h = OralTests(); h.setUp()
                bot = FakeBot()
                app = Application.builder().bot(bot).job_queue(None).build()
                errors = []
                try:
                    h.add('a', 'b')
                    async def enter(update, context):
                        return await oral.start_oral_quiz(update, context, 'de_ru')
                    conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex('^Oral$'), enter)],
                        states={oral.ORAL_QUIZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, oral.oral_quiz)]},
                        fallbacks=[])
                    app.add_handler(conv)
                    async def error(update, context): errors.append(context.error)
                    app.add_error_handler(error)
                    with patch.object(view, 'ReplyKeyboardMarkup', ReplyKeyboardMarkup):
                        await app.initialize()
                        key = (UID, UID)
                        entry = message(bot, 'Oral', 1)
                        with patch.object(FakeBot, 'send_message', side_effect=RuntimeError('entry send')):
                            await app.process_update(entry)
                        assert len(errors) == 1 and str(errors.pop()) == 'entry send', errors
                        data = app.user_data[UID]
                        hidden_key = data[oral.FRAME]['hidden']['action_key']
                        assert data[oral.FRAME]['pending_render']
                        await app.process_update(entry)
                        assert not errors, errors
                        assert conv._conversations[key] == oral.ORAL_QUIZ
                        assert data[oral.FRAME]['hidden']['action_key'] == hidden_key
                        await app.process_update(message(bot, t('en', 'oral_next'), 2))
                        assert not errors, errors
                        assert data[oral.FRAME]['pending']['word_id'] == 'a'
                        grade = message(bot, t('en', 'oral_correct'), 3)
                        with patch.object(FakeBot, 'send_message', side_effect=RuntimeError('grade send')):
                            await app.process_update(grade)
                        assert len(errors) == 1 and str(errors.pop()) == 'grade send', errors
                        assert len(h.rows('quiz_events')) == 1
                        assert data[oral.FRAME]['pending']['word_id'] == 'b'
                        # A new reply first delivers B; it cannot grade unseen B.
                        await app.process_update(message(bot, t('en', 'oral_correct'), 4))
                        assert not errors, errors
                        assert len(h.rows('quiz_events')) == 1
                        await app.process_update(grade)
                        assert len(h.rows('quiz_events')) == 1
                        await app.process_update(message(bot, t('en', 'oral_correct'), 5))
                        assert not errors, errors
                        assert data[oral.FRAME]['phase'] == 'completed'
                        assert len(h.rows('quiz_events')) == 2
                        assert NoNetworkRequest.attempts == 0
                        print('oral real PTB isolated handlers: network=0, SQLite=:memory:')
                        await app.shutdown()
                finally:
                    h.doCleanups()
            asyncio.run(scenario())
        ''')
        result = subprocess.run([sys.executable, '-B', '-c', script],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('network=0', result.stdout)


class OralMainIntegrationTests(unittest.TestCase):
    def test_actual_main_routes_failure_recovery_clarify_undo_notify_and_written(self):
        source = Path(__file__).with_name('test_new_logic_core_conversation.py').read_text()
        raw = next(node.value for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and 'class NoNetworkRequest' in node.value)
        prefix = textwrap.dedent(raw).split('class FakeService:')[0]
        script = prefix + textwrap.dedent(r'''
            import copy
            from telegram import MessageEntity, ReplyKeyboardMarkup
            from tests.test_new_logic_oral import OralTests, DAY
            from bot.oral import handlers as oral, repository as oral_repo, view
            from bot.handlers import menu, quiz, clarify_fields as clarify
            from bot.common import lang, navigation, session
            from bot.study import repository as study_repo
            from bot.study.transport_guard import inspect_delivery
            from bot.notifications import handlers as notify, settings as preferences

            async def scenario():
                h = OralTests(); h.setUp()
                bot = FakeBot()
                app = Application.builder().bot(bot).job_queue(None).build()
                builder = RecordingBuilder(app)
                errors, serial = [], 1000
                def update(text, command=False):
                    nonlocal serial
                    serial += 1
                    value = Message(serial, NOW, Chat(UID, 'private'), from_user=User(UID, 'Learner', False),
                        text=text, entities=[MessageEntity('bot_command', 0, len(text.split()[0]))] if command else None)
                    value.set_bot(bot)
                    result = Update(serial, message=value); result.set_bot(bot)
                    return result
                async def process(value):
                    await asyncio.wait_for(app.process_update(value), 3)
                    assert not errors, errors
                    return value
                async def send(text, command=False):
                    return await process(update(text, command))
                def rows(table): return h.rows(table)
                def reset():
                    for table in ('progress', 'quiz_events', 'transaction_log', 'study_actions', 'study_deliveries'):
                        h.conn.execute('DELETE FROM '+table)
                    h.conn.commit()
                    app.user_data[UID].clear()
                    conv._conversations[key] = main.TESTS_MENU
                async def enter(direction='DE', kind='Words'):
                    await send(direction)
                    assert conv._conversations[key] == main.QUIZ_RU_DE_MENU
                    return await send(kind)
                def delete_word(_db, wid):
                    result = h.conn.execute('DELETE FROM words WHERE id=?', (wid,)).rowcount
                    h.conn.commit(); return bool(result)
                def set_progress(_db, uid, wid, mode, srs):
                    study_repo.set_progress(h.conn, uid, wid, mode, srs, NOW); h.conn.commit()
                def log(uid, action, snapshot):
                    study_repo.append_transaction(h.conn, uid, action, {'snapshot': snapshot}); h.conn.commit()
                try:
                    h.add('a', 'b'); h.add('p', kind='phrase')
                    h.conn.execute('CREATE TABLE user_settings(user_id INTEGER,key TEXT,value TEXT,PRIMARY KEY(user_id,key))')
                    h.conn.commit()
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict('os.environ', {'BOT_TOKEN': '123456:synthetic', 'OWNER_ID': str(UID)}))
                        for name in ('load_dotenv', '_configure_logging', 'require_supported_ptb', '_prepare_storage',
                            '_configure_learning_reminders', '_configure_fsrs_reports', '_configure_access_reminders', '_configure_owner_digest'):
                            stack.enter_context(patch.object(main, name))
                        stack.enter_context(patch.object(main, 'Application', SimpleNamespace(builder=lambda: builder)))
                        stack.enter_context(patch.object(main, 'AIORateLimiter', return_value=object()))
                        stack.enter_context(patch.object(main, 'run_polling_safely'))
                        stack.enter_context(patch('bot.study.transport_guard.read_delivery',
                            side_effect=lambda _db, uid, tid: inspect_delivery(h.conn, uid, tid)))
                        stack.enter_context(patch.object(lang, 'get_setting', side_effect=lambda _db, key, default='': 'ru' if key == main.OWNER_DICT_LANG_KEY else 'en'))
                        stack.enter_context(patch.object(menu, '_topic_filter_banner', return_value='Synthetic filter'))
                        actions = {'DE': 'quiz_de_l1', 'L1': 'quiz_l1_de', 'Words': 'quiz_words', 'Phrases': 'quiz_phrases', 'Tests': 'tests'}
                        stack.enter_context(patch.object(menu, '_action_key_from_text', side_effect=lambda text, uid: actions.get(text, 'back' if text == t('en', 'back') else '')))
                        stack.enter_context(patch.object(oral, '_back_to_tests', navigation._back_to_tests))
                        stack.enter_context(patch.object(oral, '_back_to_main_menu', navigation._back_to_main_menu))
                        stack.enter_context(patch.object(view, 'ReplyKeyboardMarkup', ReplyKeyboardMarkup))
                        stack.enter_context(patch.object(clarify, 'get_word_by_id', side_effect=lambda _db, wid: oral_repo.word(h.conn, UID, wid, 'ru')))
                        stack.enter_context(patch.object(clarify, '_format_word_card', side_effect=lambda entry, *a, **k: entry['german']))
                        stack.enter_context(patch.object(clarify, '_clarify_screen', return_value=None))
                        stack.enter_context(patch.object(clarify, 'get_progress', side_effect=lambda _db, uid, wid, mode: study_repo.get_progress(h.conn, uid, wid, mode, DAY)))
                        stack.enter_context(patch.object(clarify, 'set_progress', side_effect=set_progress))
                        stack.enter_context(patch.object(clarify, '_capture_transaction_snapshot', side_effect=lambda uid, wid, modes: study_repo.capture_snapshot(h.conn, uid, wid, modes)))
                        stack.enter_context(patch.object(clarify, '_log_transaction', side_effect=log))
                        stack.enter_context(patch.object(clarify, 'delete_word', side_effect=delete_word))
                        stack.enter_context(patch.object(notify, 'get_preference', side_effect=lambda _db, uid, kind: preferences.read_preference(h.conn, uid, kind)))
                        stack.enter_context(patch.object(quiz, '_pick_due_entry', side_effect=lambda context, uid, mode, **kw: oral_repo.pick_original(h.conn, uid, mode, h.options, DAY,
                            context.user_data.get('session_seen_'+mode, set()), {'original_pool': []})))
                        stack.enter_context(patch.object(quiz, 'resolve_bot_word_media', side_effect=lambda _db, uid, entry: entry))
                        stack.enter_context(patch.object(quiz, '_quiz_markup', return_value=None))
                        main.main()
                        conv = app.handlers[0][0]; key = (UID, UID)
                        async def on_error(update, context): errors.append(context.error)
                        app.error_handlers.clear(); app.add_error_handler(on_error)
                        await app.initialize()
                        h.ctx.user_data = app.user_data[UID]
                        routes = [('DE', 'Words', 'de_ru'), ('DE', 'Phrases', 'phrase_de_ru'), ('L1', 'Phrases', 'phrase_ru_de')]
                        # All real menu routes, final revealed grade, original-only count.
                        for direction, kind, mode in routes:
                            reset(); await enter(direction, kind)
                            assert conv._conversations[key] == main.ORAL_QUIZ
                            assert h.frame()['mode'] == mode and h.ctx.user_data['quiz_mode'] == mode
                            assert h.frame()['phase'] == 'initial_hidden' and h.frame()['pending'] is None
                            await send(t('en', 'oral_next'))
                            while h.frame()['phase'] == 'grading': await send(t('en', 'oral_correct'))
                            count = 2 if kind == 'Words' else 1
                            assert len(rows('quiz_events')) == count
                            assert {r['mode'] for r in rows('quiz_events')} == {mode}
                            assert h.frame()['correct'] == count
                            await send(t('en', 'back'))
                            assert conv._conversations[key] == main.TESTS_MENU
                        # Initial oral and written send failures must recover before
                        # the mixed entry router resets any context or picks again.
                        for direction, kind, mode in routes + [('L1', 'Words', 'ru_de')]:
                            for fresh in (False, True):
                                reset(); await send(direction)
                                entry = update(kind)
                                with patch.object(FakeBot, 'send_message', side_effect=RuntimeError('initial send')):
                                    await app.process_update(entry)
                                assert len(errors) == 1 and str(errors.pop()) == 'initial send', errors
                                assert conv._conversations[key] == main.QUIZ_RU_DE_MENU
                                frame_key = oral.FRAME if mode != 'ru_de' else 'quiz_written_frame'
                                saved = copy.deepcopy(app.user_data[UID][frame_key])
                                replay = update(kind) if fresh else entry
                                await process(replay)
                                frame = app.user_data[UID][frame_key]
                                assert frame['flow_id'] == saved['flow_id']
                                assert frame['revision'] == saved['revision']
                                assert not frame['pending_render'] and rows('quiz_events') == []
                                assert conv._conversations[key] == (main.ORAL_QUIZ if mode != 'ru_de' else main.QUIZ_RU_DE)
                                if mode != 'ru_de': assert frame['hidden'] == saved['hidden']
                                sent = len(bot.sent); await process(entry)
                                assert len(bot.sent) == sent and rows('quiz_events') == []
                        # Stop does not grade either slot; undo exits through the
                        # actual main fallback and blocks old receipts in new flows.
                        reset(); await enter(); await send(t('en', 'stop'))
                        assert h.frame()['phase'] == 'completed' and h.frame()['correct'] == 0
                        assert h.frame()['wrong'] == 0 and rows('quiz_events') == []
                        assert '—' in bot.sent[-1]
                        reset(); await enter(); await send(t('en', 'oral_next'))
                        old_grade = await send(t('en', 'oral_correct'))
                        assert len(rows('quiz_events')) == 1
                        await send('/undo', True)
                        assert conv._conversations[key] == main.MENU
                        assert oral.FRAME not in app.user_data[UID] and rows('quiz_events') == []
                        await process(old_grade)
                        assert conv._conversations[key] == main.MENU and rows('quiz_events') == []
                        await send('Tests'); await enter()
                        before = copy.deepcopy(h.frame()); await process(old_grade)
                        assert h.frame() == before and conv._conversations[key] == main.ORAL_QUIZ
                        # An actual inline reminder opens settings from active oral.
                        reset(); await enter(); await send(t('en', 'oral_next'))
                        old_grade = await send(t('en', 'oral_correct'))
                        original_events = copy.deepcopy(rows('quiz_events'))
                        base = update('notification')
                        query = CallbackQuery('oral-notify', User(UID, 'Learner', False), 'synthetic-chat-instance',
                            message=base.message, data='notify:quiz'); query.set_bot(bot)
                        callback = Update(base.update_id, callback_query=query); callback.set_bot(bot)
                        await process(callback)
                        assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU
                        assert oral.FRAME not in app.user_data[UID]
                        await process(old_grade)
                        assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU
                        assert rows('quiz_events') == original_events
                        # Phrase add_int keeps its exact oral mode in both
                        # directions, targeting visible P rather than hidden Q.
                        h.add('q', kind='phrase')
                        for direction, mode in (('DE', 'phrase_de_ru'), ('L1', 'phrase_ru_de')):
                            reset(); await enter(direction, 'Phrases'); await send(t('en', 'oral_next'))
                            before = copy.deepcopy(h.frame())
                            visible, hidden = before['pending']['word_id'], before['hidden']['word_id']
                            assert visible != hidden
                            await send(t('en', 'clarify_fields')); await send('add_int')
                            assert app.user_data[UID]['quiz_edit_entry']['id'] == visible
                            assert app.user_data[UID]['add_int_mode'] == mode
                            await send('3'); await send(clarify.SYN_CONFIRM_TEXT)
                            assert {(r['word_id'], r['mode']) for r in rows('progress')} == {(visible, mode)}
                            assert rows('quiz_events') == []
                            await send(t('en', 'back'))
                            assert conv._conversations[key] == main.ORAL_QUIZ
                            assert h.frame()['pending'] == before['pending'] and h.frame()['hidden'] == before['hidden']
                        # A failed actual clarify return leaves the dispatcher in
                        # ADD_EDIT. Same/fresh input must drain the oral view there.
                        for same_delivery in (True, False):
                            reset(); await enter(); await send(t('en', 'oral_next'))
                            await send(t('en', 'clarify_fields'))
                            h.conn.execute("UPDATE words SET russian='SAVED translation' WHERE id='a'"); h.conn.commit()
                            back = update(t('en', 'back'))
                            with patch.object(h.service, 'refresh', side_effect=__import__('sqlite3').OperationalError('clarify refresh boundary')):
                                await app.process_update(back)
                            assert len(errors) == 1 and str(errors.pop()) == 'clarify refresh boundary', errors
                            assert conv._conversations[key] == main.ADD_EDIT
                            assert h.frame()['pending_render']
                            retry = back if same_delivery else update(t('en', 'oral_correct'))
                            await process(retry)
                            assert conv._conversations[key] == main.ORAL_QUIZ
                            assert not h.frame()['pending_render'] and rows('quiz_events') == []
                            assert 'SAVED translation' in bot.sent[-1]
                            assert 'quiz_return_point' not in app.user_data[UID]
                            await send(t('en', 'oral_correct'))
                            assert len(rows('quiz_events')) == 1 and h.frame()['pending']['word_id'] == 'b'
                            before = copy.deepcopy(h.frame()); sent = len(bot.sent)
                            await process(retry)
                            assert h.frame() == before and len(bot.sent) == sent
                        # Clarify and add_int use visible A and its exact oral mode;
                        # returning or deleting never grades/resurrects hidden B.
                        reset(); await enter(); await send(t('en', 'oral_next'))
                        before = copy.deepcopy(h.frame())
                        await send(t('en', 'clarify_fields'))
                        assert conv._conversations[key] == main.ADD_EDIT
                        assert app.user_data[UID]['quiz_edit_entry']['id'] == 'a'
                        await send('add_int')
                        assert app.user_data[UID]['add_int_mode'] == 'de_ru'
                        await send('3'); await send(clarify.SYN_CONFIRM_TEXT)
                        assert len(rows('transaction_log')) == 1 and rows('quiz_events') == []
                        assert {(r['word_id'], r['mode']) for r in rows('progress')} == {('a', 'de_ru')}
                        await send(t('en', 'back'))
                        assert conv._conversations[key] == main.ORAL_QUIZ
                        assert h.frame()['pending'] == before['pending'] and h.frame()['hidden'] == before['hidden']
                        await send(t('en', 'clarify_fields')); await send('delete'); await send('yes')
                        assert conv._conversations[key] == main.ORAL_QUIZ
                        assert h.frame()['pending']['word_id'] == 'b' and h.frame()['hidden'] is None
                        assert rows('quiz_events') == []
                        await send(t('en', 'oral_correct'))
                        assert h.frame()['phase'] == 'completed' and len(rows('quiz_events')) == 1
                        await send(t('en', 'clarify_fields')); await send('delete'); await send('yes')
                        assert h.frame()['phase'] == 'completed' and h.frame()['pending'] is None
                        assert h.frame()['last_visible'] is None and h.frame()['correct'] == 1
                        assert NoNetworkRequest.attempts == 0
                        print('actual main oral routes/mixed replay/clarify/add_int/delete/undo/notify: network=0, SQLite=:memory:')
                        await app.shutdown()
                finally: h.doCleanups()
            asyncio.run(scenario())
        ''')
        result = subprocess.run([sys.executable, '-B', '-c', script],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('network=0', result.stdout)


if __name__ == '__main__':
    unittest.main()
