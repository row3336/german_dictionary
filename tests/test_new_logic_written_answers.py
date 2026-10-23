"""Written handlers, real atomic SQLite effects, and render-only returns."""

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
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests._telegram_stub import install
install()

from bot.app_config import QUIZ_RU_DE, QUIZ_PL_SG, QUIZ_ARTICLE, TESTS_MENU, MENU, I_DONT_KNOW_TEXT, LEARNING_QUIZ, ORAL_QUIZ
from bot.common import session as sessions, quiz_resume
from bot.handlers import quiz as Q
from bot.study import answers, repository
from bot.study.schema_v18 import ensure_schema
from bot.study.schema_v19 import ensure_schema as ensure_schema19
from bot.study.srs import update_fsrs
from bot.written import state as W

DAY = date(2026, 10, 20)
BASE = {'interval': 90, 'repetitions': 8, 'ease': 3.1, 'due': DAY.isoformat()}


class Message:
    def __init__(self, text=''):
        self.text = text
        self.message_id = 100
        self.sent = []
        self.fail_once = False

    async def reply_text(self, text, **kwargs):
        await asyncio.sleep(0)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('synthetic send failure')
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=100+len(self.sent))


class WrittenTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE users(user_id INTEGER PRIMARY KEY,status TEXT);
            INSERT INTO users VALUES(7,'approved'),(8,'approved'),(9,'pending');
            CREATE TABLE words(id TEXT PRIMARY KEY,type TEXT,german TEXT,article TEXT,plural TEXT,
                russian TEXT,ukrainian TEXT,english TEXT,infinitive TEXT,is_separable INTEGER,
                conjugation_praesens TEXT,conjugation_praeteritum TEXT);
            CREATE TABLE progress(user_id INTEGER,word_id TEXT,mode TEXT,interval INTEGER,
                repetitions INTEGER,ease REAL,due TEXT,updated_at TEXT,
                PRIMARY KEY(user_id,word_id,mode));
            CREATE TABLE quiz_events(id INTEGER PRIMARY KEY,user_id INTEGER,word_id TEXT,mode TEXT,
                is_correct INTEGER,prev_is_correct INTEGER,created_at TEXT);
            CREATE TABLE transaction_log(id INTEGER PRIMARY KEY,user_id INTEGER,action TEXT,
                payload TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE learning_items(user_id INTEGER,word_id TEXT,status TEXT,preview_done INTEGER);
        ''')
        ensure_schema(self.conn)
        ensure_schema19(self.conn)
        for wid in ('a', 'b', 'c'):
            self.conn.execute("INSERT INTO words(id,type,german,article,plural,russian,ukrainian,english,infinitive,is_separable,conjugation_praesens,conjugation_praeteritum) VALUES(?,'nomen','Hund','der','Hunde','собака','собака','dog','',0,'','')", (wid,))
        self.conn.commit()
        self.pool = ['a', 'b', 'c']
        self.pair = 'ru_de'
        self.ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(set_message_reaction=AsyncMock()))
        self.serial = 0
        self.enterContext(patch('bot.study.repository.transaction', self.transaction))
        self.enterContext(patch.object(Q, 'get_db_path', return_value='synthetic'))
        self.enterContext(patch.object(Q, '_mode_l1_de', side_effect=lambda _: self.pair))
        self.enterContext(patch.object(Q, '_mode_de_l1', return_value='de_ru'))
        self.enterContext(patch.object(Q, '_mode_pl_sg', return_value='pl_sg'))
        self.enterContext(patch.object(Q, '_user_dict_lang', return_value='ru'))
        self.enterContext(patch.object(Q, '_user_interface_lang', return_value='en'))
        self.enterContext(patch.object(Q, '_action_key_from_text', side_effect=lambda text, _: {
            'fix': 'input_err', 'stop': 'stop'}.get(text, '')))
        self.enterContext(patch.object(Q, '_back_to_tests', new=AsyncMock(return_value=TESTS_MENU)))
        self.enterContext(patch.object(Q, '_back_to_main_menu', new=AsyncMock(return_value=MENU)))
        self.enterContext(patch.object(Q, '_pick_due_entry', side_effect=self.pick))
        self.enterContext(patch.object(Q, '_force_take_retry_entry', return_value=None))
        self.enterContext(patch.object(Q, 'resolve_bot_word_media', side_effect=lambda _, uid, e: e))
        self.enterContext(patch.object(Q, 'get_word_by_id', side_effect=lambda _, wid: self.word(wid)))
        self.enterContext(patch.object(Q, 'get_synonym_group', return_value=[]))
        self.enterContext(patch.object(Q, 'count_words_for_filter', return_value=3))
        self.enterContext(patch.object(Q, '_quiz_markup', return_value=None))
        self.enterContext(patch.object(Q, '_quiz_idle_markup', return_value=None))
        self.enterContext(patch.object(Q, '_tests_menu_keys', return_value=[['back']]))
        self.enterContext(patch.object(sessions, '_session_retry_gap', return_value=3))
        self.enterContext(patch.object(sessions, '_session_retry_iterations', return_value=4))
        self.enterContext(patch.object(sessions, 'get_word_by_id', side_effect=lambda _, wid: self.word(wid)))
        original, correction = answers.apply_original_answer, answers.correct_last_answer
        self.enterContext(patch.object(answers, 'apply_original_answer',
            side_effect=lambda *a, **kw: original(*a, **kw, srs_day=DAY)))
        self.enterContext(patch.object(answers, 'correct_last_answer',
            side_effect=lambda *a, **kw: correction(*a, **kw, srs_day=DAY)))

    @contextmanager
    def transaction(self, _):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def word(self, wid):
        row = self.conn.execute('SELECT * FROM words WHERE id=?', (wid,)).fetchone()
        return dict(row) if row else None

    def pick(self, *_, **kwargs):
        while self.pool:
            entry = self.word(self.pool.pop(0))
            if entry:
                return entry
        return None

    def update(self, text='', ident=None, user=7):
        self.serial += 1
        return SimpleNamespace(update_id=ident or self.serial, message=Message(text),
                               effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=user))

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def start(self, state=QUIZ_RU_DE):
        return self.run_async(Q._send_written_prompt(self.update(), self.ctx, None, state))

    def answer(self, text, state=QUIZ_RU_DE, update=None):
        handler = {QUIZ_RU_DE: Q.quiz_ru_de, QUIZ_PL_SG: Q.quiz_pl_sg, QUIZ_ARTICLE: Q.quiz_article}[state]
        return self.run_async(handler(update or self.update(text), self.ctx))

    def rows(self, table):
        return [dict(row) for row in self.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def baseline(self, mode=None, wid='a'):
        self.conn.execute('INSERT INTO progress VALUES(7,?,?,?,?,?,?,?)',
            (wid, mode or self.pair, BASE['interval'], BASE['repetitions'], BASE['ease'], BASE['due'], 'before'))
        self.conn.commit()

    def srs(self, mode=None, wid='a'):
        row = self.conn.execute('SELECT * FROM progress WHERE user_id=7 AND word_id=? AND mode=?',
                                (wid, mode or self.pair)).fetchone()
        return {k: row[k] for k in BASE}

    def test_idk_all_languages_three_penalties_but_one_original_event_and_drill(self):
        for pair in ('ru_de', 'uk_de', 'en_de'):
            with self.subTest(pair=pair):
                self.pair = pair; self.pool = ['a', 'b']; self.ctx.user_data.clear()
                self.baseline(pair); self.start(); self.answer(I_DONT_KNOW_TEXT)
                expected = dict(BASE)
                for _ in range(3): expected = update_fsrs(expected, 2, DAY, 1.65)
                self.assertEqual(self.srs(pair), expected)
                self.assertEqual(self.ctx.user_data['session_wrong_'+pair], 1)
                self.assertEqual(self.ctx.user_data['retry_queue'][pair], ['a'])
                self.assertEqual(self.ctx.user_data['retry_counts'][pair]['a'], 0)
                self.assertEqual(self.rows('quiz_events')[-1]['mode'], pair)
        self.assertEqual(len(self.rows('quiz_events')), 3)
        self.assertEqual(len(self.rows('transaction_log')), 3)

    def test_normal_wrong_is_one_penalty_and_correct_uses_divisor_165(self):
        self.baseline(); self.start(); self.answer('wrong')
        self.assertEqual(self.srs(), update_fsrs(BASE, 2, DAY, 1.65))
        self.baseline(wid='b'); self.answer('der Hund Hunde')
        self.assertEqual(self.srs(wid='b'), update_fsrs(BASE, 5, DAY, 1.65))

    def test_idk_correction_preserves_b_and_two_undos_restore_wrong_then_before(self):
        self.baseline(); self.start(); self.answer(I_DONT_KNOW_TEXT)
        wrong = self.srs(); frame = self.ctx.user_data[W.FRAME]
        key, current = frame['action_key'], self.ctx.user_data['quiz_entry']
        self.answer('fix')
        self.assertEqual(self.srs(), update_fsrs(BASE, 5, DAY, 1.65))
        self.assertEqual(self.ctx.user_data['quiz_entry'], current)
        self.assertEqual(self.ctx.user_data[W.FRAME]['action_key'], key)
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'], 1)
        self.assertEqual(len(self.rows('quiz_events')), 1)
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'], 1)
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'], 0)
        answers.undo_steps('synthetic', 7, 1, 900)
        self.assertEqual(self.srs(), wrong)
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'], 0)
        answers.undo_steps('synthetic', 7, 1, 901)
        self.assertEqual(self.srs(), BASE)
        self.assertEqual(self.rows('quiz_events'), [])

    def test_delivery_replay_precedes_current_target_and_does_not_grade_b(self):
        self.start(); update = self.update('der Hund Hunde'); self.answer('', update=update)
        frame = copy.deepcopy(self.ctx.user_data[W.FRAME])
        with patch.object(Q, '_check_ru_de_strict', side_effect=AssertionError('resolved B')), \
             patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
            self.answer('', update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME], frame)
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_send_failure_retries_only_current_committed_view(self):
        self.start(); update = self.update('der Hund Hunde'); update.message.fail_once = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'): self.answer('', update=update)
        frame = self.ctx.user_data[W.FRAME]
        self.assertEqual(frame['question_id'], 'b')
        with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
            self.answer('', update=update)
        self.assertFalse(frame['pending_render'])
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_accepted_next_read_failures_recover_without_regrading_or_skipping(self):
        for boundary in ('picker', 'media', 'format'):
            with self.subTest(boundary=boundary):
                self.ctx.user_data.clear(); self.pool = ['a', 'b', 'c']; self.start()
                update = self.update('der Hund Hunde')
                target = {'picker': '_pick_due_entry', 'media': 'resolve_bot_word_media',
                          'format': '_written_question'}[boundary]
                with patch.object(Q, target, side_effect=sqlite3.OperationalError('next read')):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'next read'):
                        self.answer('', update=update)
                pending = self.ctx.user_data[W.FRAME]['next_view']
                if boundary != 'picker':
                    self.assertEqual(pending['entry']['id'], 'b')
                before = len(self.rows('quiz_events'))
                self.answer('', update=update)
                self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'], 'b')
                self.assertEqual(self.ctx.user_data['session_correct_ru_de'], 1)
                self.assertEqual(len(self.rows('quiz_events')), before)
                frame = copy.deepcopy(self.ctx.user_data[W.FRAME])
                self.answer('', update=update)
                self.assertEqual(self.ctx.user_data[W.FRAME], frame)
                self.assertEqual(self.pool, ['c'])

    def test_new_delivery_drains_pending_original_retry_and_entry_before_text(self):
        @Q.serialized_written_entry
        async def enter(update, context):
            return await Q._send_ru_de_prompt(update, context, None)
        for kind in ('original', 'retry', 'entry'):
            for boundary in ('picker', 'media'):
                with self.subTest(kind=kind, boundary=boundary):
                    self.ctx.user_data.clear(); self.pool = ['a', 'b', 'c']
                    events = len(self.rows('quiz_events'))
                    if kind != 'entry': self.start()
                    if kind == 'retry': self.ctx.user_data['quiz_is_retry'] = True
                    old = self.update('der Hund Hunde')
                    call = enter if kind == 'entry' else Q.quiz_ru_de
                    target = '_pick_due_entry' if boundary == 'picker' else 'resolve_bot_word_media'
                    with patch.object(Q, target, side_effect=sqlite3.OperationalError('unshown read')):
                        with self.assertRaises(sqlite3.OperationalError):
                            self.run_async(call(old, self.ctx))
                    fresh = self.update('der Hund Hunde')
                    self.run_async(call(fresh, self.ctx))
                    expected = 'a' if kind == 'entry' else 'b'
                    self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'], expected)
                    self.assertEqual(self.ctx.user_data['quiz_entry']['id'], expected)
                    self.assertEqual(len(self.rows('quiz_events')), events + (kind == 'original'))
                    self.assertTrue(fresh.message.sent)
                    self.assertEqual(self.rows('study_deliveries')[-1]['action_kind'], 'written_drain_view')
                    before = copy.deepcopy(self.ctx.user_data)
                    self.run_async(call(fresh, self.ctx))
                    self.run_async(call(old, self.ctx))
                    self.assertEqual(self.ctx.user_data, before)
                    self.assertEqual(self.pool, ['b', 'c'] if kind == 'entry' else ['c'])

    def test_new_delivery_send_failure_drains_once_without_grading_unshown_b(self):
        self.start(); old = self.update('der Hund Hunde'); old.message.fail_once = True
        with self.assertRaises(RuntimeError): self.answer('', update=old)
        fresh = self.update('der Hund Hunde'); fresh.message.fail_once = True
        with self.assertRaises(RuntimeError): self.answer('', update=fresh)
        newer = self.update('der Hund Hunde')
        self.answer('', update=newer)
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'], 'b')
        self.assertFalse(self.ctx.user_data[W.FRAME]['pending_render'])
        self.assertEqual(len(self.rows('quiz_events')), 1)
        before = copy.deepcopy(self.ctx.user_data)
        for update in (old, fresh, newer): self.answer('', update=update)
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(self.pool, ['c'])

    def test_initial_and_retry_control_read_failure_resume_their_accepted_advance(self):
        @Q.serialized_written_entry
        async def enter(update, context):
            return await Q._send_ru_de_prompt(update, context, None)
        update = self.update()
        with patch.object(Q, '_pick_due_entry', side_effect=sqlite3.OperationalError('entry read')):
            with self.assertRaises(sqlite3.OperationalError): self.run_async(enter(update, self.ctx))
        self.run_async(enter(update, self.ctx))
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'], 'a')
        self.ctx.user_data['quiz_is_retry'] = True
        update = self.update(I_DONT_KNOW_TEXT)
        with patch.object(Q, '_pick_due_entry', side_effect=sqlite3.OperationalError('retry read')):
            with self.assertRaises(sqlite3.OperationalError): self.answer('', update=update)
        self.answer('', update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'], 'b')
        for table in ('progress', 'quiz_events', 'transaction_log'):
            self.assertEqual(self.rows(table), [])

    def test_start_closes_live_permissions_once_preserving_persisted_conjugation(self):
        from bot.handlers import menu
        self.conn.execute("INSERT INTO conjugation_admissions(user_id,pair,word_id,admitted_at) VALUES(7,'ru_de','a','before')")
        self.conn.execute("INSERT INTO conjugation_blocks(user_id,pair,word_id,tense,phase,due) VALUES(7,'ru_de','a','praesens','test','2026-10-21')")
        self.conn.execute("""INSERT INTO conjugation_passes(id,user_id,pair,word_id,tense,cycle,status,
            pronoun_order_json,expected_forms_json,baseline_srs_json,started_at)
            VALUES('pass',7,'ru_de','a','praesens',1,'active','[]','{}','{}','before')""")
        self.conn.execute("""INSERT INTO conjugation_answers(user_id,pass_id,pronoun_group,ordinal,
            answer_text,is_correct,original_text,original_correct,answered_at,answer_day,
            correction_session_id,action_key) VALUES(7,'pass','ich',1,'x',0,'x',0,'before',
            '2026-10-20','live','form-action')""")
        self.conn.commit()
        persisted = {table: self.rows(table) for table in (
            'conjugation_admissions', 'conjugation_blocks', 'conjugation_passes')}
        self.ctx.user_data.update(conjugation_frame={'token': 'live'},
            conjugation_callback_receipts={1: {}}, quiz_return_point={'flow_id': 'old'})
        update = self.update('/start')
        with patch.object(menu, 'get_db_path', return_value='synthetic'),              patch.object(menu, '_is_owner_only', return_value=False),              patch.object(menu, 'get_user_status', return_value='approved'),              patch.object(menu, '_guest_start', new=AsyncMock(return_value=MENU)) as guest:
            self.assertEqual(self.run_async(menu.start(update, self.ctx)), MENU)
            self.assertNotIn('conjugation_frame', self.ctx.user_data)
            self.assertNotIn('conjugation_callback_receipts', self.ctx.user_data)
            self.assertIsNone(self.rows('conjugation_answers')[0]['correction_session_id'])
            self.assertEqual({table: self.rows(table) for table in persisted}, persisted)
            self.ctx.user_data['conjugation_frame'] = {'token': 'new'}
            self.conn.execute("UPDATE conjugation_answers SET correction_session_id='new'"); self.conn.commit()
            before = copy.deepcopy(self.ctx.user_data)
            self.assertIsNone(self.run_async(menu.start(update, self.ctx)))
            self.assertEqual(self.ctx.user_data, before)
            self.assertEqual(self.rows('conjugation_answers')[0]['correction_session_id'], 'new')
            self.assertEqual(guest.await_count, 1)

    def test_start_replays_only_its_unfinished_accepted_menu_display(self):
        from bot.handlers import menu
        update = self.update('/start')
        self.ctx.user_data['conjugation_frame'] = {'token': 'old'}
        with patch.object(menu, 'get_db_path', return_value='synthetic'),              patch.object(menu, '_is_owner_only', return_value=False),              patch.object(menu, 'get_user_status', return_value='approved'),              patch.object(menu, '_guest_start', new=AsyncMock(side_effect=[RuntimeError('send failed'), MENU])) as guest:
            with self.assertRaisesRegex(RuntimeError, 'send failed'):
                self.run_async(menu.start(update, self.ctx))
            self.assertNotIn('conjugation_frame', self.ctx.user_data)
            ledgers = copy.deepcopy(self.rows('study_deliveries'))
            with patch.object(menu, 'invalidate_study_frames', side_effect=AssertionError('second close')):
                self.assertEqual(self.run_async(menu.start(update, self.ctx)), MENU)
            self.assertEqual(guest.await_count, 2)
            self.assertNotIn('quiz_start_pending', self.ctx.user_data)
            self.assertEqual(self.rows('study_deliveries'), ledgers)
            self.ctx.user_data['oral_frame'] = {'flow_id': 'new'}
            before = copy.deepcopy(self.ctx.user_data)
            self.assertIsNone(self.run_async(menu.start(update, self.ctx)))
            self.assertEqual(self.ctx.user_data, before)
            self.assertEqual(guest.await_count, 2)

    def test_start_send_failure_and_pending_onboarding_do_not_reapply_exit(self):
        from bot.handlers import menu
        update = self.update('/start')
        self.ctx.user_data['conjugation_frame'] = {'token': 'old'}
        with patch.object(menu, 'get_db_path', return_value='synthetic'),              patch.object(menu, '_is_owner_only', return_value=False),              patch.object(menu, 'get_user_status', return_value='approved'),              patch.object(menu, '_guest_start', new=AsyncMock(side_effect=RuntimeError('send failed'))) as guest:
            with self.assertRaisesRegex(RuntimeError, 'send failed'):
                self.run_async(menu.start(update, self.ctx))
            self.assertNotIn('conjugation_frame', self.ctx.user_data)
            self.ctx.user_data['conjugation_frame'] = {'token': 'new'}
            self.assertIsNone(self.run_async(menu.start(update, self.ctx)))
            self.assertEqual(guest.await_count, 1)
            self.assertEqual(self.ctx.user_data['conjugation_frame']['token'], 'new')
        for status in (None, 'pending', 'rejected'):
            with patch.object(menu, 'get_db_path', return_value='synthetic'),                  patch.object(menu, '_is_owner_only', return_value=False),                  patch.object(menu, 'get_user_status', return_value=status),                  patch.object(menu, 'close_study_flow', side_effect=AssertionError('approval guard')),                  patch.object(menu, '_guest_start', new=AsyncMock(return_value=-1)):
                self.assertEqual(self.run_async(menu.start(self.update('/start'), self.ctx)), -1)

    def test_learning_send_recovery_replay_preserves_new_flow_without_sending(self):
        from bot.handlers import learning
        self.start(); update = self.update('send')
        def send(*_, **kwargs):
            self.ctx.user_data['learning_send_receipt'] = {'replayed': True,
                'recovered_undo': True, 'transport_update_id': update.update_id}
            return False
        before = copy.deepcopy(self.ctx.user_data)
        with patch.object(Q, '_action_key_from_text', return_value='send_learn'),              patch.object(learning, '_send_entry_to_learning', side_effect=send),              patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
            self.assertIsNone(self.answer('', update=update))
        before['learning_send_receipt'] = self.ctx.user_data['learning_send_receipt']
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(update.message.sent, [])

    def test_late_delivery_never_renders_another_pending_view(self):
        self.start(); old = self.update('der Hund Hunde'); self.answer('', update=old)
        newer = self.update('der Hund Hunde'); newer.message.fail_once = True
        with self.assertRaises(RuntimeError): self.answer('', update=newer)
        frame = copy.deepcopy(self.ctx.user_data[W.FRAME]); old.message.sent.clear()
        self.answer('', update=old)
        self.assertEqual(self.ctx.user_data[W.FRAME], frame)
        self.assertEqual(old.message.sent, [])
        self.assertEqual(len(self.rows('quiz_events')), 2)

    def test_sql_failure_preserves_frame_and_rolls_back_whole_original(self):
        self.start(); before = copy.deepcopy(self.ctx.user_data)
        initial_deliveries = self.rows('study_deliveries')
        self.conn.execute("CREATE TRIGGER fail_event BEFORE INSERT ON quiz_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError): self.answer(I_DONT_KNOW_TEXT)
        self.assertEqual(self.ctx.user_data, before)
        for table in ('progress', 'quiz_events', 'transaction_log'):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(self.rows('study_deliveries'), initial_deliveries)

    def test_plural_and_article_idk_keep_their_existing_divisors(self):
        for state, mode, divisor in ((QUIZ_PL_SG, 'pl_sg', 2), (QUIZ_ARTICLE, Q.MODE_ARTICLE, 1)):
            with self.subTest(mode=mode):
                self.ctx.user_data.clear(); self.pool = ['a', 'b']; self.baseline(mode)
                self.start(state); self.answer(I_DONT_KNOW_TEXT, state)
                expected = dict(BASE)
                for _ in range(3): expected = update_fsrs(expected, 2, DAY, divisor)
                self.assertEqual(self.srs(mode), expected)

    def test_completed_return_never_picks_or_resurrects_last_answer(self):
        self.pool = ['a']; self.start(); self.answer('der Hund Hunde')
        self.assertNotIn('quiz_entry', self.ctx.user_data)
        quiz_resume.capture_return_point(self.ctx, QUIZ_RU_DE)
        before = copy.deepcopy(self.ctx.user_data)
        with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
            self.run_async(quiz_resume.render_existing_frame(self.update(), self.ctx))
        self.assertEqual(self.ctx.user_data[W.FRAME]['phase'], 'completed')
        self.assertNotIn('quiz_entry', self.ctx.user_data)
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'], before['session_correct_ru_de'])

    def test_question_return_refreshes_word_without_picker_or_new_action(self):
        self.start(); self.answer('der Hund Hunde')
        quiz_resume.capture_return_point(self.ctx, QUIZ_RU_DE)
        frame = copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.conn.execute("UPDATE words SET german='Katze' WHERE id='b'"); self.conn.commit()
        with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
            self.run_async(quiz_resume.render_existing_frame(self.update(), self.ctx))
        self.assertEqual(self.ctx.user_data['quiz_entry']['german'], 'Katze')
        self.assertEqual(self.ctx.user_data[W.FRAME]['action_key'], frame['action_key'])
        self.assertEqual(self.ctx.user_data[W.FRAME]['revision'], frame['revision'])
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_delete_current_continues_residual_without_grading_deleted_word(self):
        self.start(); quiz_resume.capture_return_point(self.ctx, QUIZ_RU_DE)
        self.conn.execute("DELETE FROM words WHERE id='a'"); self.conn.commit()
        self.run_async(quiz_resume.resume_after_delete(self.update(), self.ctx, 'a'))
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
        self.assertEqual(self.rows('quiz_events'), [])

    def test_retry_answer_has_delivery_but_no_srs_event_or_summary_result(self):
        self.start(); self.ctx.user_data['quiz_is_retry'] = True
        self.answer(I_DONT_KNOW_TEXT)
        self.assertEqual(self.rows('progress'), [])
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(self.rows('transaction_log'), [])
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'], 0)
        self.assertEqual([r['action_kind'] for r in self.rows('study_deliveries')], ['written_enter', 'written_retry'])

    def test_last_original_correction_cancels_its_displayed_tail_retry(self):
        self.pool = ['a']; self.baseline()
        with patch.object(Q, '_force_take_retry_entry', sessions._force_take_retry_entry):
            self.start(); self.answer('wrong')
            self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'a')
            self.assertTrue(self.ctx.user_data['quiz_is_retry'])
            self.assertEqual(self.ctx.user_data['retry_counts']['ru_de']['a'], 1)
            self.answer('fix')
        self.assertNotIn('quiz_entry', self.ctx.user_data)
        self.assertFalse(self.ctx.user_data.get('quiz_is_retry', False))
        self.assertEqual(self.ctx.user_data[W.FRAME]['phase'], 'completed')
        self.assertEqual(self.srs(), update_fsrs(BASE, 5, DAY, 1.65))
        self.assertEqual(len(self.rows('quiz_events')), 1)
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'], 1)
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'], 1)
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'], 0)
        self.assertEqual(self.ctx.user_data['retry_counts']['ru_de']['a'], 1)

    def test_real_tail_replies_and_correction_have_one_original_in_all_l1_modes(self):
        for pair, lang in (('ru_de', 'ru'), ('uk_de', 'uk'), ('en_de', 'en')):
            with self.subTest(pair=pair), patch.object(Q, '_user_dict_lang', return_value=lang), \
                 patch.object(Q, '_force_take_retry_entry', sessions._force_take_retry_entry):
                self.pair = pair; self.pool = ['a']; self.ctx.user_data.clear()
                self.baseline(pair); self.start(); self.answer('wrong')
                wrong = self.srs(pair)
                self.answer('der Hund Hunde'); self.answer('wrong')
                self.assertTrue(self.ctx.user_data['quiz_is_retry'])
                self.assertEqual(self.srs(pair), wrong)
                self.assertEqual(len([r for r in self.rows('quiz_events') if r['mode'] == pair]), 1)
                self.assertEqual(self.ctx.user_data['session_wrong_'+pair], 1)
                self.assertEqual(self.ctx.user_data['retry_counts'][pair]['a'], 3)
                correction = self.update('fix'); self.answer('', update=correction)
                self.assertNotIn('quiz_entry', self.ctx.user_data)
                self.assertEqual(self.ctx.user_data[W.FRAME]['phase'], 'completed')
                self.assertEqual(self.srs(pair), update_fsrs(BASE, 5, DAY, 1.65))
                self.assertEqual(self.ctx.user_data['retry_counts'][pair]['a'], 3)
                self.assertEqual(self.ctx.user_data['session_correct_'+pair], 1)
                self.assertEqual(self.ctx.user_data['session_wrong_'+pair], 0)
                before = copy.deepcopy(self.ctx.user_data)
                with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('replay picker')):
                    self.answer('', update=correction)
                self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(len(self.rows('quiz_events')), 3)
        self.assertEqual(len(self.rows('transaction_log')), 6)

    def test_last_word_correction_advances_to_another_original_or_retry(self):
        for next_retry in (False, True):
            with self.subTest(next_retry=next_retry), \
                 patch.object(Q, '_force_take_retry_entry', sessions._force_take_retry_entry):
                self.ctx.user_data.clear(); self.pool = ['a']; self.start(); self.answer('wrong')
                if next_retry:
                    sessions._register_retry_result(self.ctx, self.pair, 'b', False, False, 7)
                else:
                    self.pool = ['b']
                before_events = len(self.rows('quiz_events'))
                self.answer('fix')
                self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
                self.assertEqual(bool(self.ctx.user_data.get('quiz_is_retry')), next_retry)
                self.assertEqual(self.ctx.user_data['retry_counts'][self.pair]['a'], 1)
                self.assertEqual(len(self.rows('quiz_events')), before_events)
                self.assertEqual(self.ctx.user_data['session_correct_'+self.pair], 1)
                self.assertEqual(self.ctx.user_data['session_wrong_'+self.pair], 0)

    def test_correction_preserves_a_distinct_displayed_retry_and_its_action(self):
        self.pool = ['a']; self.start()
        sessions._register_retry_result(self.ctx, self.pair, 'b', False, False, 7)
        with patch.object(Q, '_force_take_retry_entry', sessions._force_take_retry_entry):
            self.answer('wrong')
            self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
            self.assertTrue(self.ctx.user_data['quiz_is_retry'])
            action = self.ctx.user_data[W.FRAME]['action_key']
            with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('preserve B')):
                self.answer('fix')
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
        self.assertTrue(self.ctx.user_data['quiz_is_retry'])
        self.assertEqual(self.ctx.user_data[W.FRAME]['action_key'], action)
        self.assertEqual(self.ctx.user_data['retry_counts'][self.pair]['b'], 1)
        self.assertEqual(self.ctx.user_data['retry_queue'][self.pair], [])
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_last_word_correction_recovery_never_grades_or_skips_the_next_question(self):
        self.baseline()
        for boundary in ('picker', 'media', 'format', 'send'):
            for fresh in (False, True):
                with self.subTest(boundary=boundary, fresh=fresh), \
                     patch.object(Q, '_force_take_retry_entry', sessions._force_take_retry_entry):
                    self.ctx.user_data.clear(); self.pool = ['a']; before_srs = self.srs()
                    self.start(); self.answer('wrong'); self.pool = ['b']
                    before_events = len(self.rows('quiz_events'))
                    correction = self.update('fix')
                    if boundary == 'send':
                        correction.message.fail_once = True
                        with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'):
                            self.answer('', update=correction)
                    else:
                        target = {'picker': '_pick_due_entry', 'media': 'resolve_bot_word_media',
                                  'format': '_written_question'}[boundary]
                        with patch.object(Q, target, side_effect=sqlite3.OperationalError('fix next read')):
                            with self.assertRaisesRegex(sqlite3.OperationalError, 'fix next read'):
                                self.answer('', update=correction)
                    self.assertEqual(self.srs(), update_fsrs(before_srs, 5, DAY, 1.65))
                    self.assertNotEqual(self.ctx.user_data[W.FRAME]['question_id'], 'a')
                    self.answer('', update=self.update('der Hund Hunde') if fresh else correction)
                    self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
                    self.assertFalse(self.ctx.user_data.get('quiz_is_retry', False))
                    self.assertEqual(self.ctx.user_data['retry_counts'][self.pair]['a'], 1)
                    self.assertEqual(len(self.rows('quiz_events')), before_events)
                    self.assertEqual(self.ctx.user_data['session_correct_'+self.pair], 1)
                    self.assertEqual(self.ctx.user_data['session_wrong_'+self.pair], 0)
                    self.assertNotIn('next_view', self.ctx.user_data[W.FRAME])
                    self.assertFalse(self.ctx.user_data[W.FRAME]['pending_render'])

    def test_completed_last_word_correction_send_replay_and_undo(self):
        self.pool = ['a']; self.baseline()
        with patch.object(Q, '_force_take_retry_entry', sessions._force_take_retry_entry):
            self.start(); self.answer('wrong'); wrong = self.srs()
            correction = self.update('fix'); correction.message.fail_once = True
            with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'):
                self.answer('', update=correction)
            self.assertEqual(self.ctx.user_data[W.FRAME]['phase'], 'completed')
            self.assertNotIn('quiz_entry', self.ctx.user_data)
            with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('render only')):
                self.answer('', update=correction)
            self.assertFalse(self.ctx.user_data[W.FRAME]['pending_render'])
            self.assertEqual(len(self.rows('quiz_events')), 1)
            answers.undo_steps('synthetic', 7, 1, 900)
            self.assertEqual(self.srs(), wrong)
            self.assertEqual(self.rows('quiz_events')[0]['is_correct'], 0)
            self.answer('', update=correction)
            self.assertEqual(self.srs(), wrong)
            self.assertNotIn('quiz_entry', self.ctx.user_data)
            answers.undo_steps('synthetic', 7, 1, 901)
            self.assertEqual(self.srs(), BASE)
            self.assertEqual(self.rows('quiz_events'), [])
            self.answer('', update=correction)
            self.assertEqual(self.srs(), BASE)
            self.assertEqual(self.rows('quiz_events'), [])

    def test_pending_user_cannot_write_answer(self):
        self.start()
        with self.assertRaises(PermissionError): self.answer('', update=self.update(I_DONT_KNOW_TEXT, user=9))
        self.assertEqual(self.rows('quiz_events'), [])

    def test_phrase_divisor_and_clarify_add_int_import_use_oral_two(self):
        from bot.handlers.clarify_fields import _interval_divisor_for_mode
        for pair in ('ru_de', 'uk_de', 'en_de'):
            self.pair = pair
            self.assertEqual(Q._interval_divisor_for_mode(7, pair), 1.65)
            self.assertEqual(_interval_divisor_for_mode(7, 'phrase_'+pair), 2)

    def test_concurrent_same_delivery_commits_once_and_keeps_b(self):
        self.start(); update = self.update('der Hund Hunde')
        async def scenario():
            return await asyncio.gather(Q.quiz_ru_de(update, self.ctx), Q.quiz_ru_de(update, self.ctx))
        self.run_async(scenario())
        self.assertEqual(len(self.rows('quiz_events')), 1)
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'], 1)

    def test_replayed_correction_after_new_wrong_does_not_correct_new_answer(self):
        self.start(); self.answer(I_DONT_KNOW_TEXT)
        correction = self.update('fix'); self.answer('', update=correction)
        self.answer(I_DONT_KNOW_TEXT)
        before = copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.answer('', update=correction)
        self.assertEqual([r['is_correct'] for r in self.rows('quiz_events')], [1, 0])
        self.assertEqual(self.ctx.user_data[W.FRAME], before)

    def test_rejected_delivery_is_bound_before_another_question_is_selected(self):
        self.start(); rejected = self.update('der Hund Hunde')
        with patch.object(Q.written_service, 'original', side_effect=answers.StaleStudyAction('stale')):
            self.answer('', update=rejected)
        self.run_async(Q._send_ru_de_prompt(self.update(), self.ctx, None))
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
        self.answer('', update=rejected)
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')

    def test_deleted_question_advances_without_grade_and_old_delivery_stays_inert(self):
        self.start(); update = self.update('der Hund Hunde')
        self.conn.execute("DELETE FROM words WHERE id='a'"); self.conn.commit()
        self.answer('', update=update)
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')
        self.assertEqual(self.rows('quiz_events'), [])
        self.answer('', update=update)
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'b')

    def test_pending_undo_recovery_closes_frame_and_returns_main_without_grade(self):
        self.start()
        self.conn.execute("""INSERT INTO study_deliveries
            (user_id,transport_update_id,flow_id,action_kind,state,result_json)
            VALUES(7,99999,'undo','undo','processing',?)""",
            (json.dumps({'tx_ids': [], 'cursor': 0, 'outcomes': []}),))
        self.conn.commit()
        self.assertEqual(self.answer(I_DONT_KNOW_TEXT), MENU)
        self.assertNotIn(W.FRAME, self.ctx.user_data)
        self.assertEqual(self.rows('quiz_events'), [])

    def test_stop_receipt_cannot_reopen_a_closed_written_flow(self):
        self.start(); self.answer('der Hund Hunde')
        update = self.update('stop')
        self.assertEqual(self.answer('', update=update), TESTS_MENU)
        self.assertNotIn(W.FRAME, self.ctx.user_data)
        self.assertNotIn('quiz_published_view', self.ctx.user_data)
        self.assertEqual(self.answer('', update=update), TESTS_MENU)
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_correction_preserves_consumed_retry_budget_and_dynamic_limit(self):
        self.start(); self.answer(I_DONT_KNOW_TEXT)
        self.ctx.user_data['retry_counts']['ru_de']['a'] = 2
        self.answer('fix')
        self.assertEqual(self.ctx.user_data['retry_counts']['ru_de']['a'], 2)
        self.assertEqual(self.ctx.user_data['retry_queue']['ru_de'], [])
        with patch.object(sessions, '_session_retry_iterations', return_value=3):
            sessions._register_retry_result(self.ctx, 'ru_de', 'a', False, True, 7)
            sessions._force_take_retry_entry(self.ctx, 'ru_de')
            sessions._register_retry_result(self.ctx, 'ru_de', 'a', False, True, 7)
        self.assertEqual(self.ctx.user_data['retry_queue']['ru_de'], [])
        self.assertNotIn('a', self.ctx.user_data['retry_counts']['ru_de'])
        self.assertEqual(len(self.rows('quiz_events')), 1)

    def test_learning_send_replay_does_not_advance_and_recovery_exits_main(self):
        self.start()
        from bot.handlers import learning
        for key, expected in (('replayed', None), ('recovered_undo', MENU)):
            update = self.update('send')
            def send(*_, **kwargs):
                self.ctx.user_data['learning_send_receipt'] = {key: True,
                    'transport_update_id': update.update_id}
                return False
            with patch.object(Q, '_action_key_from_text', return_value='send_learn'), \
                 patch.object(learning, '_send_entry_to_learning', side_effect=send), \
                 patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
                self.assertEqual(self.answer('', update=update), expected)
            if key == 'replayed':
                self.assertEqual(self.ctx.user_data['quiz_entry']['id'], 'a')
        self.assertNotIn(W.FRAME, self.ctx.user_data)


    def test_stale_written_return_and_delivery_leave_foreign_flow_untouched(self):
        self.start(); accepted = self.update('der Hund Hunde'); self.answer('', update=accepted)
        point = quiz_resume.capture_return_point(self.ctx, QUIZ_RU_DE)
        for kind, expected in (('learning', LEARNING_QUIZ), ('oral', ORAL_QUIZ)):
            quiz_resume.publish_view(self.ctx, caller_kind=kind, mode='other', phase='question',
                flow_id='other-flow', current_question_id='c', view_revision=10)
            before = copy.deepcopy(self.ctx.user_data)
            self.assertEqual(self.run_async(Q._render_existing_written(self.update(), self.ctx, point, None)), expected)
            self.assertEqual(self.answer('', update=accepted), expected)
            self.assertEqual(self.ctx.user_data, before)

    def test_stale_return_point_does_not_close_newer_written_flow(self):
        self.start(); point = quiz_resume.capture_return_point(self.ctx, QUIZ_RU_DE)
        self.answer('der Hund Hunde')
        before = copy.deepcopy(self.ctx.user_data)
        update = self.update()
        self.assertEqual(self.run_async(Q._render_existing_written(update, self.ctx, point, None)), QUIZ_RU_DE)
        self.assertEqual(self.ctx.user_data, before)
        self.assertEqual(update.message.sent, [])

    def test_entry_wrapper_replays_failed_first_send_before_menu_resets_frame(self):
        calls = []
        @Q.serialized_written_entry
        async def entry(update, context):
            calls.append(True)
            sessions._reset_session(context, self.pair)
            return await Q._send_ru_de_prompt(update, context, None)
        update = self.update(); update.message.fail_once = True
        with self.assertRaises(RuntimeError): self.run_async(entry(update, self.ctx))
        frame = self.ctx.user_data[W.FRAME]
        key, flow = frame['action_key'], frame['flow_id']
        with patch.object(Q, '_pick_due_entry', side_effect=AssertionError('picker')):
            self.assertEqual(self.run_async(entry(update, self.ctx)), QUIZ_RU_DE)
        self.assertEqual(calls, [True])
        self.assertEqual((frame['action_key'], frame['flow_id']), (key, flow))
        self.assertEqual(frame['question_id'], 'a')
        self.assertEqual(self.rows('quiz_events'), [])

    def test_initial_menu_delivery_cannot_be_reinterpreted_as_a_written_answer(self):
        update = self.update('Words')
        self.run_async(Q._send_ru_de_prompt(update, self.ctx, None))
        before = copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.answer('', update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME], before)
        self.assertEqual(self.rows('quiz_events'), [])


class WrittenConversationTest(unittest.TestCase):
    def test_real_dispatch_initial_send_failure_replays_before_menu_frame_reset(self):
        # The existing harness provides only real-PTB/no-network boilerplate.
        source = Path(__file__).with_name('test_new_logic_core_conversation.py').read_text()
        tree = ast.parse(source)
        raw = next(node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and 'class NoNetworkRequest' in node.value)
        prefix = textwrap.dedent(raw).split('class FakeService:')[0]
        script = prefix + textwrap.dedent(r'''
            from tests.test_new_logic_written_answers import WrittenTests
            from bot.handlers import menu, quiz
            from bot.written.state import FRAME
            from bot.study.transport_guard import inspect_delivery
            from unittest.mock import AsyncMock

            def message(bot, text, ident):
                value = Message(ident, NOW, Chat(UID, 'private'), from_user=User(UID, 'Learner', False), text=text)
                value.set_bot(bot)
                return value

            async def scenario():
                fixture = WrittenTests()
                fixture.setUp()
                bot = FakeBot()
                app = Application.builder().bot(bot).job_queue(None).build()
                builder = RecordingBuilder(app)
                errors = []
                try:
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict('os.environ', {'BOT_TOKEN': '123456:synthetic'}))
                        for name in ('load_dotenv', '_configure_logging', 'require_supported_ptb',
                                     '_prepare_storage', '_configure_learning_reminders',
                                     '_configure_fsrs_reports', '_configure_access_reminders', '_configure_owner_digest'):
                            stack.enter_context(patch.object(main, name))
                        stack.enter_context(patch.object(main, 'Application', SimpleNamespace(builder=lambda: builder)))
                        stack.enter_context(patch.object(main, 'AIORateLimiter', return_value=object()))
                        stack.enter_context(patch.object(main, 'run_polling_safely'))
                        stack.enter_context(patch('bot.study.transport_guard.read_delivery',
                            side_effect=lambda _path, uid, transport: inspect_delivery(fixture.conn, uid, transport)))
                        stack.enter_context(patch.object(FakeBot, 'set_message_reaction', new=AsyncMock()))
                        stack.enter_context(patch.object(menu, '_mode_l1_de', return_value='ru_de'))
                        stack.enter_context(patch.object(menu, '_mode_pl_sg', return_value='pl_sg'))
                        stack.enter_context(patch.object(menu, '_user_interface_lang', return_value='en'))
                        stack.enter_context(patch.object(menu, '_topic_filter_banner', return_value='synthetic filter'))
                        stack.enter_context(patch.object(menu, '_action_key_from_text',
                            side_effect=lambda text, uid: {'Words': 'quiz_words', 'Plural': 'quiz_pl_sg'}.get(text, '')))
                        main.main()
                        conv = app.handlers[0][0]
                        async def on_error(update, context):
                            errors.append(context.error)
                        app.error_handlers.clear()
                        app.add_error_handler(on_error)
                        await app.initialize()
                        key = (UID, UID)
                        conv._conversations[key] = main.QUIZ_RU_DE_MENU
                        app.user_data[UID]['quiz_direction_base'] = 'ru_de'
                        entry = Update(1000, message=message(bot, 'Words', 1))
                        entry.set_bot(bot)
                        with patch.object(FakeBot, 'send_message', side_effect=RuntimeError('synthetic first send')):
                            await app.process_update(entry)
                        assert len(errors) == 1 and str(errors[0]) == 'synthetic first send', errors
                        errors.clear()
                        frame = app.user_data[UID][FRAME]
                        saved = (frame['action_key'], frame['flow_id'])
                        assert frame['question_id'] == 'a' and frame['pending_render']
                        assert conv._conversations[key] == main.QUIZ_RU_DE_MENU
                        with patch.object(quiz, '_pick_due_entry', side_effect=AssertionError('replayed entry picker')):
                            await app.process_update(entry)
                        assert not errors, errors
                        assert conv._conversations[key] == main.QUIZ_RU_DE
                        assert (frame['action_key'], frame['flow_id']) == saved
                        assert not frame['pending_render']
                        answer = Update(1001, message=message(bot, 'der Hund Hunde', 2))
                        answer.set_bot(bot)
                        await app.process_update(answer)
                        assert not errors, errors
                        assert app.user_data[UID]['quiz_entry']['id'] == 'b'
                        assert len(fixture.rows('quiz_events')) == 1
                        await app.process_update(entry)
                        assert len(fixture.rows('quiz_events')) == 1
                        assert app.user_data[UID]['quiz_entry']['id'] == 'b'
                        conv._conversations[key] = main.TESTS_MENU
                        plural = Update(1002, message=message(bot, 'Plural', 3))
                        plural.set_bot(bot)
                        with patch.object(FakeBot, 'send_message', side_effect=RuntimeError('synthetic plural send')):
                            await app.process_update(plural)
                        assert len(errors) == 1 and str(errors[0]) == 'synthetic plural send', errors
                        errors.clear()
                        saved = app.user_data[UID][FRAME]['action_key']
                        await asyncio.wait_for(app.process_update(plural), 3)
                        assert not errors, errors
                        assert conv._conversations[key] == main.QUIZ_PL_SG
                        assert app.user_data[UID][FRAME]['action_key'] == saved
                        assert len(fixture.rows('quiz_events')) == 1
                        assert NoNetworkRequest.attempts == 0
                        print('written and plural entry replay: network=0, SQLite=:memory:')
                        await app.shutdown()
                finally:
                    fixture.doCleanups()

            asyncio.run(scenario())
        ''')
        result = subprocess.run([sys.executable, '-B', '-c', script],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('network=0', result.stdout)


if __name__ == '__main__':
    unittest.main()
