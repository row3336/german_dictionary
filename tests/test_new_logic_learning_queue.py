"""K15 L01..L09: retained synthetic SQLite and real no-network adapters."""

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from _telegram_stub import install
install()

from bot import learning_requests as requests, learning_store as store, storage
from bot.app_config import LEARNING_QUIZ, QUIZ_RU_DE
from bot.common import db_safety
from bot.common.quiz_resume import capture_return_point, render_existing_frame
from bot.handlers import learning as handler, quiz, clarify_fields
from bot.i18n import t
from bot.study import answers, deliveries, generations, repository as repo, schema_v20
from bot.written import state as written

STAND = Path('/Users/wf/py/_agent_work/888_german_dictionary/k15_impl_20261023')
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


class LearningQueueTests(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.runtime = Path(tempfile.mkdtemp(prefix='l01-l09-', dir=STAND))
        self.db = str(self.runtime / 'synthetic.sqlite')
        env = patch.dict(os.environ, {'DB_PATH': self.db, 'TMPDIR': str(self.runtime),
                                     'OWNER_ID': '0', 'BOT_TOKEN': '12345:FAKE'})
        env.start(); self.addCleanup(env.stop)
        storage.init_db(self.db)
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES (1,'approved','en','ru'),(2,'approved','uk','en')")
            conn.executemany("INSERT INTO words(id,type,german,russian,english,ukrainian,topic,cefr_level) VALUES (?,'adverb',?,?,?,?,?,?)",
                [(c, c, 'ru-'+c, 'en-'+c, 'uk-'+c, 'one' if c < 'd' else 'two', 'A1' if c < 'c' else 'A2')
                 for c in 'abcdef'])
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO topics(name,level) VALUES ('one','A1'),('two','A2')")
        self.ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(set_message_reaction=AsyncMock()))
        self.seq = 100

    def update(self, text='send_learn', uid=1, ident=None):
        self.seq += 1
        msg = SimpleNamespace(text=text, message_id=self.seq, reply_text=AsyncMock())
        return SimpleNamespace(update_id=self.seq if ident is None else ident, message=msg,
            effective_message=msg, effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=uid))

    def rows(self, table):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def state(self):
        return {name: self.rows(name) for name in ('learning_items','learning_batches','progress',
            'quiz_events','transaction_log','study_actions','study_deliveries','learning_requests','user_settings')}

    def select(self, wid, participants=()):
        with repo.transaction(self.db) as conn:
            return generations.selected_entry(conn, 1, storage._row_to_entry(conn.execute('SELECT * FROM words WHERE id=?',(wid,)).fetchone()), participants)

    def enqueue(self, wid='a', uid=1, now=NOW, expected=0):
        with repo.transaction(self.db) as conn:
            return requests.enqueue(conn, uid, wid, expected, now)

    def add(self, qty=1, **kwargs):
        with repo.transaction(self.db) as conn:
            return requests.add_candidates(conn, 1, qty, now=NOW, **kwargs)

    def legacy(self, ids=('a',), status='completed', ready=True):
        with repo.transaction(self.db) as conn:
            batch = store.get_or_create_batch(conn, 1, 20, NOW)
            store.add_items(conn, 1, ids, batch, NOW)
            conn.execute("UPDATE learning_items SET status=?,preview_done=?,first_due_pending=?,step_index=4,recovery_remaining=2,next_due='2000-01-01T00:00:00Z' WHERE user_id=1",
                         (status, int(ready), int(not ready)))
        return batch

    def grade(self, wid='a', mode='learning', key=None, entry=None):
        self.seq += 1
        entry = entry or self.select(wid)
        return answers.apply_original_answer(self.db, 1, wid, mode, 'wrong', key or str(self.seq), self.seq, 1, NOW,
            flow_id='test', live_revision=1, expected_generation=entry['study_generation'],
            expected_lifecycles=requests.frame_lifecycles(entry),
            learning_transition=(lambda c,s,k,n: store.apply_answer(c,s,k,n)) if mode == 'learning' else None)

    def test_l01_fresh_and_19_upgrade_preserve_old_rows_repeat_future_rollback(self):
        self.legacy()
        before = self.state()
        with repo.transaction(self.db) as conn:
            conn.execute('DROP TABLE learning_requests'); conn.execute('PRAGMA user_version=19')
        fingerprint = db_safety.database_fingerprint(self.db)
        with self.assertRaises(db_safety.DestructiveMigrationRequired): storage.init_db(self.db)
        def fail_after_ddl(conn):
            schema_v20.ensure_schema(conn)
            raise RuntimeError('fault after DDL')
        with patch.object(schema_v20, 'migrate', side_effect=fail_after_ddl):
            with self.assertRaisesRegex(RuntimeError, 'fault after DDL'):
                db_safety._apply_migration_dispatcher(self.db)
        self.assertEqual(db_safety.database_fingerprint(self.db), fingerprint)
        report = db_safety._apply_migration_dispatcher(self.db)
        self.assertEqual(report['to_version'], 22)
        self.assertEqual(self.rows('learning_requests'), [])
        for name in before:
            if name != 'learning_requests': self.assertEqual(self.rows(name), before[name])
        self.assertFalse(db_safety._apply_migration_dispatcher(self.db)['migrated'])
        self.assertTrue(db_safety.run_healthcheck(self.db)['ok'])
        with repo.transaction(self.db) as conn: conn.execute('PRAGMA user_version=23')
        with self.assertRaises(RuntimeError): storage.init_db(self.db)
        with self.assertRaises(RuntimeError): db_safety._apply_migration_dispatcher(self.db)

    def test_l01_17_18_19_20_chain_and_online_backup(self):
        from test_reflexive_db19 import old_schema
        with repo.transaction(self.db) as conn: conn.execute('DROP TABLE learning_requests')
        old_schema(self.db, 17)
        backup = self.runtime / 'online-backup.sqlite'
        db_safety.backup_database(self.db, backup)
        before = db_safety.database_fingerprint(self.db)
        rehearsal = db_safety._apply_migration_dispatcher(str(backup))
        self.assertEqual(set(rehearsal['steps']), {'study_v18','reflexive_v19','learning_requests_v20','perfekt_v21','hilfsverb_v22'})
        self.assertEqual(db_safety.database_fingerprint(self.db), before)
        self.assertEqual(db_safety.get_user_version(backup), 22)
        self.assertEqual(db_safety.get_user_version(self.db), 17)

    def test_l01_health_and_startup_require_complete_twenty(self):
        with repo.transaction(self.db) as conn: conn.execute('DROP TABLE learning_requests')
        self.assertFalse(db_safety.run_healthcheck(self.db)['ok'])
        with self.assertRaisesRegex(RuntimeError, 'incomplete schema22'): storage.init_db(self.db)

    def test_l02_enqueue_statuses_fifo_users_generation_and_deletion(self):
        for uid in (1,2): self.assertEqual(self.enqueue('a',uid), requests.EnqueueResult.QUEUED)
        before = self.rows('learning_requests')
        self.assertEqual(self.enqueue('a',now=NOW+timedelta(days=1)), requests.EnqueueResult.ALREADY_PENDING)
        self.assertEqual(self.rows('learning_requests'), before)
        with repo.transaction(self.db) as conn: generations.advance_generation(conn,1,'a',0)
        self.assertEqual(self.enqueue('a'), requests.EnqueueResult.STALE)
        self.assertEqual(self.enqueue('a',expected=1), requests.EnqueueResult.QUEUED)
        after = self.rows('learning_requests')
        self.assertNotEqual(after[0]['request_id'], before[0]['request_id'])
        self.assertEqual(after[1],before[1])
        self.legacy(('b',), 'active')
        self.assertEqual(self.enqueue('b'), requests.EnqueueResult.ALREADY_ACTIVE)
        with repo.transaction(self.db) as conn: conn.execute("DELETE FROM words WHERE id='a'")
        self.assertEqual(self.rows('learning_requests'), [])
        self.assertEqual(self.enqueue('a'), requests.EnqueueResult.MISSING)

    def test_l02_history_completed_skipped_can_enqueue_and_nonce_after_cancel(self):
        self.legacy(('a','b'))
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE learning_items SET status='skipped' WHERE word_id='b'")
            repo.append_event(conn,1,'a','ru_de',False,NOW)
        before = self.state()
        for wid in 'abc': self.assertEqual(self.enqueue(wid), requests.EnqueueResult.QUEUED)
        for name in before:
            if name != 'learning_requests': self.assertEqual(self.rows(name),before[name])
        old = self.rows('learning_requests')[0]
        with repo.transaction(self.db) as conn: conn.execute("UPDATE learning_requests SET status='cancelled' WHERE word_id='a'")
        self.enqueue('a',now=NOW+timedelta(seconds=1))
        self.assertNotEqual(self.rows('learning_requests')[0]['request_id'],old['request_id'])

    def test_l03_ready_group_untouched_by_two_requests(self):
        with repo.transaction(self.db) as conn:
            for i in range(16): conn.execute("INSERT INTO words(id,type,german) VALUES (?,'adverb',?)",(str(i),str(i)))
        self.legacy(tuple(str(i) for i in range(16)), 'active')
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE learning_batches SET ready_since='2000-01-01T00:00:00Z'")
        before = self.state(); stats = storage.learning_status_snapshot(self.db,1)
        self.enqueue('a'); self.enqueue('b')
        self.assertEqual(storage.learning_status_snapshot(self.db,1),stats)
        for name in before:
            if name != 'learning_requests': self.assertEqual(self.rows(name), before[name])

    def test_l04_one_pool_filters_fifo_capacity_mix_and_hidden_requests(self):
        self.legacy(('b','d'))
        # Equal queue time deliberately opposes insertion order.
        for wid in ('d','b','a'): self.enqueue(wid)
        for topics,cefr in ((None,None),([],None),(['one'],None),(None,[]),(['two'],['A2']),(['one'],['A1'])):
            with self.subTest(topics=topics,cefr=cefr),repo.transaction(self.db) as conn:
                pool=requests.eligible_pool(conn,1,topics,cefr)
                ids=[e['id'] for e in pool]
                self.assertEqual(requests.count_candidates(conn,1,topics,cefr),len(ids))
                self.assertEqual([e['id'] for e in requests.pick(conn,1,100,topics,cefr)],ids)
                self.assertEqual([e['id'] for e in storage.list_learning_queue_words(self.db,1,topics,cefr)],ids)
                mixed=requests.pick(conn,1,100,topics,cefr,True)
                self.assertEqual({e['id'] for e in mixed},set(ids))
                self.assertEqual([e['id'] for e in mixed if e['learning_explicit_pending']], [i for i in ids if i in 'abd'])
                if topics==[] or cefr==[]: self.assertEqual(ids,[])
        self.assertEqual(self.add(1,topics=['one'])['selected_ids'],['a'])
        pending=[r['word_id'] for r in self.rows('learning_requests') if r['status']=='pending']
        self.assertEqual(pending,['d','b'])
        self.assertEqual(self.add(1)['added'],0)

    def test_l04_actual_queue_list_uses_effective_filter_and_empty_stays_empty(self):
        from bot.handlers import db_search
        self.enqueue('d')
        with patch.object(db_search,'_send_lines_or_file',new=AsyncMock()) as send:
            asyncio.run(db_search.db_search_input(self.update(t('en','db_search_topics_lern')),self.ctx))
            body=send.call_args.args[1]
            self.assertIn('3 words',body)
            self.assertNotIn('ru-d',body)
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE topics SET level='A2'")
        with patch.object(db_search,'_send_db_search_prompt',new=AsyncMock()) as empty:
            asyncio.run(db_search.db_search_input(self.update(t('en','db_search_topics_lern')),self.ctx))
            self.assertIn(t('en','no_queued_learning'),empty.call_args.args)
        self.assertEqual(self.rows('learning_requests')[0]['status'],'pending')

    def test_l05_restart_exact_target_and_lifecycle_every_actual_add(self):
        self.legacy(('a','b'))
        with repo.transaction(self.db) as conn:
            repo.set_progress(conn,1,'a','ru_de',{'interval':8,'repetitions':4,'ease':2.5,'due':'2030-01-01'})
            repo.append_event(conn,1,'a','ru_de',False,NOW)
        self.enqueue('a'); baseline=self.state()
        result=self.add(2)
        self.assertEqual(result['selected_ids'],['a','c'])
        items={r['word_id']:r for r in self.rows('learning_items')}
        self.assertEqual(items['b'],baseline['learning_items'][1])
        for wid in ('a','c'):
            row=items[wid]
            self.assertEqual((row['step_index'],row['preview_done'],row['recovery_remaining'],row['first_due_pending'],row['status']), (0,0,0,1,'active'))
        for table in ('progress','quiz_events','transaction_log','study_actions'):
            self.assertEqual(self.rows(table),baseline[table])
        self.assertEqual({r['word_id']:r['learning_lifecycle'] for r in self.rows('learning_requests')},{'a':1,'c':1})
        with repo.transaction(self.db) as conn:
            self.assertEqual(generations.generation_for(conn,1,'a'),0)
            self.assertEqual(requests.lifecycle_for(conn,1,'b'),0)
            self.assertEqual(store.finalize_preview_if_ready(conn,1,items['a']['batch_id'],NOW),[])

    def test_l05_failure_after_actual_item_write_rolls_back_entire_control(self):
        self.enqueue('a');before=self.state()
        with repo.transaction(self.db) as conn:
            conn.execute("""CREATE TRIGGER fail_consume BEFORE UPDATE ON learning_requests WHEN NEW.status='consumed'
                BEGIN SELECT RAISE(ABORT,'consume fault'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'consume fault'):
            answers.record_control_transition(self.db,1,301,'add','learning_add',lambda conn: requests.add_candidates(conn,1,1,now=NOW))
        self.assertEqual(self.state(),before)
        with repo.transaction(self.db) as conn: conn.execute('DROP TRIGGER fail_consume')
        accepted=answers.record_control_transition(self.db,1,301,'add','learning_add',lambda conn: requests.add_candidates(conn,1,1,now=NOW))
        self.assertEqual(accepted['selected_ids'],['a'])
        self.enqueue('b')
        with patch.object(requests,'add_candidates',side_effect=AssertionError('replayed picker')):
            replay=answers.record_control_transition(self.db,1,301,'add','learning_add',lambda c: requests.add_candidates(c,1,2))
        self.assertTrue(replay['replayed'])
        self.assertEqual([r['word_id'] for r in self.rows('learning_requests') if r['status']=='pending'],['b'])
        self.assertEqual(self.add(2)['selected_ids'],['b'])

    def test_l06_actual_written_queue_advance_once_retry_budget_retained(self):
        entry=self.select('a'); written.establish_view(self.ctx,'ru_de',QUIZ_RU_DE,entry)
        self.ctx.user_data.update(retry_queue={'ru_de':['a']},retry_counts={'ru_de':{'a':3}},quiz_is_retry=True)
        update=self.update(t('en','send_learn'))
        with patch.object(quiz,'_pick_due_entry',return_value=self.select('b')) as picker:
            asyncio.run(quiz.quiz_ru_de(update,self.ctx))
            self.assertEqual(picker.call_count,1)
            asyncio.run(quiz.quiz_ru_de(update,self.ctx))
            self.assertEqual(picker.call_count,1)
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')
        self.assertEqual(self.ctx.user_data['retry_queue']['ru_de'],[])
        self.assertEqual(self.ctx.user_data['retry_counts']['ru_de']['a'],3)
        self.assertEqual(self.rows('quiz_events'),[]);self.assertEqual(self.rows('progress'),[])
        self.assertEqual(self.rows('transaction_log'),[])

    def test_l06_queued_media_render_send_failures_drain_same_or_fresh_once(self):
        for stage in ('media','render','send'):
            for fresh in (False,True):
                with self.subTest(stage=stage,fresh=fresh):
                    probe=LearningQueueTests(methodName='runTest');probe.setUp();self.addCleanup(probe.doCleanups)
                    written.establish_view(probe.ctx,'ru_de',QUIZ_RU_DE,probe.select('a'))
                    probe.ctx.user_data.update(retry_queue={'ru_de':['a']},retry_counts={'ru_de':{'a':3}})
                    update=probe.update(t('en','send_learn'))
                    target={'media':'resolve_bot_word_media','render':'_written_question','send':'_reply_text_with_retry'}[stage]
                    with patch.object(quiz,'_pick_due_entry',return_value=probe.select('b')) as picker:
                        with patch.object(quiz,target,side_effect=RuntimeError('display fault')):
                            with self.assertRaisesRegex(RuntimeError,'display fault'):
                                asyncio.run(quiz.quiz_ru_de(update,probe.ctx))
                        asyncio.run(quiz.quiz_ru_de(probe.update('not an answer') if fresh else update,probe.ctx))
                        self.assertEqual(picker.call_count,1)
                    self.assertEqual(probe.ctx.user_data['quiz_entry']['id'],'b')
                    self.assertEqual(probe.ctx.user_data['retry_counts']['ru_de']['a'],3)
                    self.assertEqual(probe.rows('quiz_events'),[])
                    self.assertEqual(probe.rows('progress'),[])
                    self.assertEqual(len(probe.rows('learning_requests')),1)

    def test_l06_noops_do_not_pick_or_change_action_or_last_answer(self):
        self.enqueue('a')
        written.establish_view(self.ctx,'ru_de',QUIZ_RU_DE,self.select('a'))
        self.ctx.user_data[written.FRAME]['last_receipt']={'word_id':'old','answer_kind':'wrong'}
        frame=deepcopy(self.ctx.user_data[written.FRAME])
        with patch.object(quiz,'_pick_due_entry',side_effect=AssertionError('noop picker')):
            asyncio.run(quiz.quiz_ru_de(self.update(t('en','send_learn')),self.ctx))
        for key in ('revision','question_id','action_key','last_receipt'):
            self.assertEqual(self.ctx.user_data[written.FRAME][key],frame[key])
        self.assertEqual(self.rows('quiz_events'),[])

    def test_l06_clarify_other_b_and_active_learning_frame_preserved(self):
        self.legacy(('a',),'active')
        asyncio.run(handler.learning_menu_choice(self.update(t('en','learning_start')),self.ctx))
        capture_return_point(self.ctx,LEARNING_QUIZ)
        self.ctx.user_data['quiz_edit_entry']=self.select('a')
        frame=deepcopy(self.ctx.user_data[handler.FRAME]);state=self.state()
        self.assertEqual(handler._send_entry_to_learning(self.update(),self.ctx,'quiz_edit_entry'), requests.EnqueueResult.ALREADY_ACTIVE)
        with patch.object(handler,'_pick_learning_due_entry',side_effect=AssertionError('active picker')):
            asyncio.run(render_existing_frame(self.update('return'),self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME],frame)
        for name in state:
            if name!='study_deliveries': self.assertEqual(self.rows(name),state[name])
        # A has no effect on the already displayed, separately selected B.
        self.ctx.user_data.clear();written.establish_view(self.ctx,'ru_de',QUIZ_RU_DE,self.select('b'))
        self.ctx.user_data['quiz_edit_entry']=self.select('c')
        before=deepcopy(self.ctx.user_data[written.FRAME])
        handler._send_entry_to_learning(self.update(),self.ctx,'quiz_edit_entry')
        asyncio.run(quiz._learning_send_outcome(self.update(ident=self.seq),self.ctx,QUIZ_RU_DE))
        self.assertEqual(self.ctx.user_data[written.FRAME]['action_key'],before['action_key'])
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')

    def test_l07_old_fsrs_undo_preserves_pending_then_new_cycle(self):
        first=self.grade(mode='ru_de')
        self.enqueue('a')
        pending=deepcopy(self.rows('learning_requests'))
        answers.undo_steps(self.db,1,1,401)
        self.assertEqual(self.rows('learning_requests'),pending)
        self.assertEqual(self.rows('progress'),[])
        second=self.grade(mode='ru_de')
        self.add(1); cycle=self.rows('learning_items')
        answers.undo_steps(self.db,1,1,402)
        self.assertEqual(self.rows('learning_items'),cycle)
        self.assertEqual(self.rows('progress'),[]);self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(self.rows('learning_requests')[0]['learning_lifecycle'],1)

    def test_l07_composite_old_learning_undo_skips_new_a_restores_b_and_fsrs(self):
        self.legacy(('a','b'),'active')
        with repo.transaction(self.db) as conn:
            snapshot=repo.capture_snapshot(conn,1,'a',['ru_de','learning'],affected_learning_ids=['b'])
            conn.execute("UPDATE learning_items SET step_index=5 WHERE user_id=1")
            repo.set_progress(conn,1,'a','ru_de',{'interval':3,'repetitions':2,'ease':2.5,'due':'2026-10-24'})
            fsrs=repo.append_event(conn,1,'a','ru_de',False,NOW)
            learning=repo.append_event(conn,1,'a','learning',False,NOW)
            repo.record_created_events(conn,snapshot,[fsrs,learning])
            conn.execute("UPDATE learning_items SET status='completed' WHERE word_id='a'")
        self.enqueue('a');self.add(2)
        cycle=next(r for r in self.rows('learning_items') if r['word_id']=='a')
        with repo.transaction(self.db) as conn: repo.restore_snapshot(conn,snapshot)
        rows={r['word_id']:r for r in self.rows('learning_items')}
        self.assertEqual(rows['a'],cycle)
        self.assertEqual(rows['b']['step_index'],4)
        self.assertEqual(self.rows('progress'),[])
        self.assertEqual([r['mode'] for r in self.rows('quiz_events')],['learning'])

    def test_l07_new_cycle_undo_and_fsrs_correction_are_legal(self):
        original=self.grade(mode='ru_de');self.enqueue('a');self.add(1)
        fixed=answers.correct_last_answer(self.db,1,original['action_key'],'fix-fsrs',501,1,NOW,flow_id='test',live_revision=1)
        self.assertEqual(fixed['after']['repetitions'],1)
        with repo.transaction(self.db) as conn:
            store.preview_transition(conn,1,'a','preview_next',NOW,0)
        cycle=self.rows('learning_items')
        self.grade();answers.undo_steps(self.db,1,1,502)
        self.assertEqual(self.rows('learning_items'),cycle)
        self.assertEqual([r['mode'] for r in self.rows('quiz_events')],['ru_de'])

    def test_l08_word_generation_is_stronger_than_lifecycle(self):
        old=self.grade(mode='ru_de');self.enqueue('a')
        with repo.transaction(self.db) as conn: generations.advance_generation(conn,1,'a',0)
        with repo.transaction(self.db) as conn:
            self.assertNotIn('a',[e['id'] for e in requests.eligible_pool(conn,1) if e['learning_explicit_pending']])
            repo.restore_snapshot(conn,old['before'])
        self.assertEqual(len(self.rows('progress')),1)
        self.assertEqual(self.rows('learning_requests')[0]['status'],'pending')
        self.assertEqual(self.enqueue('a',expected=1),requests.EnqueueResult.QUEUED)
        self.add(1)
        self.assertEqual(self.rows('learning_requests')[0]['word_generation'],1)

    def test_l08_new_add_after_generation_change_uses_new_marker_identity(self):
        self.enqueue('a',now=NOW-timedelta(days=1)); old=self.rows('learning_requests')[0]
        with repo.transaction(self.db) as conn:
            generations.advance_generation(conn,1,'a',0)
            pool=requests.eligible_pool(conn,1)
            current=next(e for e in pool if e['id']=='a')
            self.assertFalse(current['learning_explicit_pending'])
        self.add(1); consumed=self.rows('learning_requests')[0]
        self.assertEqual(consumed['word_generation'],1)
        self.assertEqual(consumed['learning_lifecycle'],1)
        self.assertNotEqual(consumed['request_id'],old['request_id'])
        self.assertNotEqual(consumed['queued_at'],old['queued_at'])

    def test_l05_explicit_add_retains_current_request_identity_fifo(self):
        self.enqueue('a',now=NOW-timedelta(days=1)); old=self.rows('learning_requests')[0]
        self.add(1); consumed=self.rows('learning_requests')[0]
        self.assertEqual(consumed['request_id'],old['request_id'])
        self.assertEqual(consumed['queued_at'],old['queued_at'])

    def test_l08_delivery_first_authorization_recovery_and_old_receipt(self):
        self.ctx.user_data['quiz_entry']=self.select('a');update=self.update()
        self.assertEqual(handler._send_entry_to_learning(update,self.ctx),requests.EnqueueResult.QUEUED)
        class Poison:
            def get(self,*args): raise AssertionError('replay target lookup')
        self.ctx.user_data['quiz_entry']=Poison()
        self.assertEqual(handler._send_entry_to_learning(update,self.ctx),requests.EnqueueResult.QUEUED)
        self.assertTrue(self.ctx.user_data['learning_send_receipt']['replayed'])
        with repo.transaction(self.db) as conn:
            deliveries.begin_delivery(conn,1,602,'old','send_learn')
            deliveries.commit_delivery(conn,1,602,{'added':1,'word_id':'old-active','batch_id':'historical'})
            deliveries.begin_delivery(conn,1,603,'undo','undo',result={'tx_ids':[],'cursor':0,'outcomes':[]})
        handler._send_entry_to_learning(self.update(ident=602),self.ctx)
        self.assertEqual(self.ctx.user_data['learning_send_receipt']['word_id'],'old-active')
        self.assertNotIn('enqueue_result',self.ctx.user_data['learning_send_receipt'])
        handler._send_entry_to_learning(self.update(ident=604),self.ctx)
        self.assertTrue(self.ctx.user_data['learning_send_receipt']['recovered_undo'])
        with self.assertRaises(PermissionError): handler._send_entry_to_learning(self.update(uid=99),self.ctx)

    def test_l08_actual_menu_add_receipt_legacy_replay_and_failed_send(self):
        with repo.transaction(self.db) as conn:
            conn.execute('INSERT INTO user_settings(user_id,key,value) VALUES (1,?,?)',(handler.LEARNING_DAILY_QTY_KEY,'1'))
        self.enqueue('a')
        with repo.transaction(self.db) as conn:
            deliveries.begin_delivery(conn,1,651,'learning-menu','learning_add')
            deliveries.commit_delivery(conn,1,651,{'added':3,'flow_id':'learning-menu'})
        baseline=self.state()
        with patch.object(requests,'add_candidates',side_effect=AssertionError('old Add picker')):
            asyncio.run(handler._learning_add_words(self.update(ident=651),self.ctx))
        self.assertEqual(self.state(),baseline)
        update=self.update()
        update.message.reply_text=AsyncMock(side_effect=RuntimeError('send failed'))
        with self.assertRaisesRegex(RuntimeError,'send failed'):
            asyncio.run(handler._learning_add_words(update,self.ctx))
        state=self.state()
        self.enqueue('b')
        update.message.reply_text=AsyncMock()
        with patch.object(requests,'add_candidates',side_effect=AssertionError('failed Add picker')):
            asyncio.run(handler._learning_add_words(update,self.ctx))
        self.assertEqual(self.rows('learning_items'),state['learning_items'])
        self.assertEqual([r['word_id'] for r in self.rows('learning_requests') if r['status']=='pending'],['b'])

    def test_l07_actual_oral_original_after_completed_learning_cycle_one(self):
        from bot.oral.service import OralService
        self.enqueue('a'); self.add(1)
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE learning_items SET status='completed' WHERE word_id='a'")
        options={'gap':3,'limit':4,'topics':None,'cefr_levels':None}
        service=OralService(self.db)
        start=service.start(1,'de_ru',661,options,today=NOW.date())['oral_frame']
        reveal=start  # the start plan already performs the former 'next' reveal
        self.assertEqual(reveal['pending']['word_id'],'a')
        self.assertEqual(reveal['pending']['entry']['learning_lifecycle'],1)
        receipt=service.transition(1,reveal,'wrong',663,options,today=NOW.date(),now=NOW)
        self.assertEqual(receipt['mode'],'de_ru')
        self.assertEqual(len(self.rows('quiz_events')),1)
        # A generic FSRS callback can also implement correction; it has no
        # Learning effects and must retain its legal correction right at LC1.
        def fsrs_correction(conn,before,kind,now):
            from bot.study.srs import apply_answer,default_srs
            after=apply_answer(before['progress'].get('de_ru') or default_srs(NOW.date()),kind,NOW.date(),2.0)
            repo.set_progress(conn,1,'a','de_ru',after,now)
            return after
        fixed=answers.correct_last_answer(self.db,1,receipt['action_key'],'oral-fix',664,reveal['revision'],NOW,
            flow_id=reveal['flow_id'],live_revision=reveal['revision'],learning_transition=fsrs_correction)
        self.assertEqual(fixed['answer_kind'],'correct')
        self.assertEqual(self.rows('learning_requests')[0]['learning_lifecycle'],1)

    def test_l09_old_grade_control_preview_stale_before_snapshot_and_mutation(self):
        self.legacy(('a','b'),'active',ready=False)
        selected=self.select('a',('b',))
        with repo.transaction(self.db) as conn: conn.execute("UPDATE learning_items SET status='completed' WHERE word_id='b'")
        self.enqueue('b');self.add(2)
        baseline=self.state()
        with patch.object(repo,'capture_snapshot',side_effect=AssertionError('stale capture')):
            with self.assertRaises(generations.StaleGeneration):
                answers.apply_preview_transition(self.db,1,'a','preview_next','preview',701,1,NOW,flow_id='test',live_revision=1,
                    affected_learning_ids=lambda c: store.preview_affected_ids(c,1,'a'),
                    expected_generations=generations.frame_generations(selected),expected_lifecycles=requests.frame_lifecycles(selected),
                    transition=lambda c,s,n: store.preview_transition(c,1,'a','preview_next',n))
        self.assertEqual(self.state(),baseline)
        with patch.object(repo,'capture_snapshot',side_effect=AssertionError('stale capture')):
            for command in (
                lambda: self.grade('b',entry={'id':'b','study_generation':0,'learning_lifecycle':0}),
                lambda: answers.record_control_transition(self.db,1,702,'test','bound-control',lambda c: self.fail('stale resolver'),expected_lifecycles={'b':0})):
                with self.assertRaises(generations.StaleGeneration): command()
        self.assertEqual(self.state(),baseline)

    def test_l09_actual_old_frames_deferred_resume_and_correction_block_new_cycle(self):
        self.legacy(('a',),'active')
        asyncio.run(handler.learning_menu_choice(self.update(t('en','learning_start')),self.ctx))
        old=deepcopy(self.ctx.user_data)
        receipt=self.grade()
        with repo.transaction(self.db) as conn: conn.execute("UPDATE learning_items SET status='completed' WHERE word_id='a'")
        self.enqueue('a');self.add(1);cycle=self.rows('learning_items')
        with patch.object(repo,'capture_snapshot',side_effect=AssertionError('old frame capture')):
            asyncio.run(handler.learning_quiz(self.update('wrong'),self.ctx))
        self.assertEqual(self.rows('learning_items'),cycle)
        self.assertEqual(len(self.rows('quiz_events')),1)
        self.ctx.user_data=deepcopy(old)
        self.ctx.user_data[handler.FRAME]['pending_render']=True
        with patch.object(handler,'_next_learning_view',side_effect=AssertionError('old plan picker')):
            asyncio.run(handler.learning_quiz(self.update('next'),self.ctx))
        self.assertNotIn(handler.FRAME,self.ctx.user_data)
        with self.assertRaises(generations.StaleGeneration):
            answers.correct_last_answer(self.db,1,receipt['action_key'],'fix',801,1,NOW,flow_id='test',live_revision=1,
                learning_transition=lambda c,s,k,n: store.apply_answer(c,s,k,n))
        self.assertEqual(self.rows('learning_items'),cycle)

    def test_l09_legacy_snapshots_missing_lifecycle_mean_zero(self):
        self.legacy(('a',),'completed')
        with repo.transaction(self.db) as conn: baseline=repo.capture_snapshot(conn,1,'a',['ru_de'])
        self.enqueue('a');self.add(1);cycle=self.rows('learning_items')
        for version in (1,2,3):
            snapshot=deepcopy(baseline);snapshot['version']=version;snapshot.pop('learning_lifecycles')
            with repo.transaction(self.db) as conn: repo.restore_snapshot(conn,snapshot)
            self.assertEqual(self.rows('learning_items'),cycle)

    def test_l09_clarify_reload_preserves_selected_pins_and_new_search_captures(self):
        from bot.search import read_search_entry
        selected=self.select('a'); self.ctx.user_data['quiz_entry']=selected
        self.enqueue('a');self.add(1)
        fresh=read_search_entry(self.db,'a',user_id=1)
        self.assertEqual(fresh['learning_lifecycle'],1)
        restored=clarify_fields._preserve_selection(fresh,selected)
        self.assertEqual(restored['learning_lifecycle'],0)
        self.assertEqual(restored['learning_lifecycles'],{'a':0})


if __name__=='__main__': unittest.main()
