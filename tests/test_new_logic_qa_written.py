"""Independent written fault boundaries and exact original/correction effects."""

import asyncio
import ast
import copy
import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_new_logic_written_answers as fixture
from bot.app_config import QUIZ_RU_DE,QUIZ_PL_SG,QUIZ_ARTICLE,I_DONT_KNOW_TEXT
from bot.common import quiz_resume
from bot.handlers import quiz as Q
from bot.study import answers
from bot.study.srs import update_fsrs
from bot.written import state as W


class IndependentWrittenQA(unittest.TestCase):
    setUp = fixture.WrittenTests.setUp
    transaction = fixture.WrittenTests.transaction
    word = fixture.WrittenTests.word
    pick = fixture.WrittenTests.pick
    update = fixture.WrittenTests.update
    run_async = fixture.WrittenTests.run_async
    start = fixture.WrittenTests.start
    answer = fixture.WrittenTests.answer
    rows = fixture.WrittenTests.rows
    baseline = fixture.WrittenTests.baseline
    srs = fixture.WrittenTests.srs

    def test_next_picker_sql_failure_replay_advances_to_b_without_another_original(self):
        self.start()
        update = self.update(I_DONT_KNOW_TEXT)
        with patch.object(Q,'_pick_due_entry',side_effect=sqlite3.OperationalError('QA next picker SQL failure')):
            try:
                self.answer('',update=update)
            except sqlite3.OperationalError as exc:
                self.assertIn('QA next picker',str(exc))
        self.assertLessEqual(len(self.rows('quiz_events')),1)
        self.answer('',update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'b')
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')
        self.assertEqual(len(self.rows('quiz_events')),1)
        self.assertEqual(self.rows('quiz_events')[0]['word_id'],'a')
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'],1)
        before = copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.answer('',update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME],before)
        self.assertEqual(len(self.rows('quiz_events')),1)

    def test_next_media_sql_failure_replay_preserves_reserved_b_without_skip(self):
        self.start()
        update = self.update('der Hund Hunde')
        with patch.object(Q,'resolve_bot_word_media',side_effect=sqlite3.OperationalError('QA next media SQL failure')):
            try:
                self.answer('',update=update)
            except sqlite3.OperationalError as exc:
                self.assertIn('QA next media',str(exc))
        self.assertLessEqual(len(self.rows('quiz_events')),1)
        self.answer('',update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'b')
        self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')
        self.assertEqual(len(self.rows('quiz_events')),1)
        self.assertEqual(self.rows('quiz_events')[0]['word_id'],'a')
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'],1)

    def test_initial_entry_picker_failure_replays_into_first_question(self):
        @Q.serialized_written_entry
        async def entry(update,context):
            return await Q._send_ru_de_prompt(update,context,None)
        update=self.update()
        with patch.object(Q,'_pick_due_entry',side_effect=sqlite3.OperationalError('QA entry lookup')):
            try:
                self.run_async(entry(update,self.ctx))
            except sqlite3.OperationalError as exc:
                self.assertIn('QA entry lookup',str(exc))
        self.run_async(entry(update,self.ctx))
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'a')
        self.assertTrue(update.message.sent)
        self.assertEqual(self.rows('quiz_events'),[])
        before=copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.run_async(entry(update,self.ctx))
        self.assertEqual(self.ctx.user_data[W.FRAME],before)

    def test_retry_picker_failure_replays_into_b_without_original_effect(self):
        self.start();self.ctx.user_data['quiz_is_retry']=True
        update=self.update(I_DONT_KNOW_TEXT)
        with patch.object(Q,'_pick_due_entry',side_effect=sqlite3.OperationalError('QA retry lookup')):
            try:
                self.answer('',update=update)
            except sqlite3.OperationalError as exc:
                self.assertIn('QA retry lookup',str(exc))
        self.answer('',update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'b')
        self.assertTrue(update.message.sent)
        for table in ('progress','quiz_events','transaction_log'):
            self.assertEqual(self.rows(table),[])
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'],0)
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'],0)

    def test_idk_and_correction_two_undos_in_all_written_scopes(self):
        scopes=[('ru_de',QUIZ_RU_DE,1.65),('uk_de',QUIZ_RU_DE,1.65),('en_de',QUIZ_RU_DE,1.65),
                ('pl_sg',QUIZ_PL_SG,2),('article',QUIZ_ARTICLE,1)]
        for index,(mode,state,divisor) in enumerate(scopes):
            with self.subTest(mode=mode):
                self.ctx.user_data.clear();self.pool=['a','b','c']
                if mode.endswith('_de'):self.pair=mode
                self.baseline(mode);self.start(state)
                self.answer(I_DONT_KNOW_TEXT,state)
                wrong=fixture.BASE.copy()
                for _ in range(3):wrong=update_fsrs(wrong,2,fixture.DAY,divisor)
                self.assertEqual(self.srs(mode),wrong)
                frame=copy.deepcopy(self.ctx.user_data[W.FRAME])
                correction=self.update('fix');self.answer('',state,update=correction)
                self.assertEqual(self.srs(mode),update_fsrs(fixture.BASE,5,fixture.DAY,divisor))
                self.assertEqual(self.ctx.user_data[W.FRAME]['action_key'],frame['action_key'])
                self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'b')
                self.assertEqual([r['is_correct'] for r in self.rows('quiz_events')],[1])
                first=answers.undo_steps('synthetic',7,1,10000+index*2)
                self.assertEqual(first['restored'],1);self.assertEqual(self.srs(mode),wrong)
                self.assertEqual([r['is_correct'] for r in self.rows('quiz_events')],[0])
                self.assertTrue(answers.undo_steps('synthetic',7,99,10000+index*2)['replayed'])
                second=answers.undo_steps('synthetic',7,1,10001+index*2)
                self.assertEqual(second['restored'],1);self.assertEqual(self.srs(mode),fixture.BASE)
                self.assertEqual(self.rows('quiz_events'),[])

    def test_two_distinct_concurrent_deliveries_grade_a_then_b_once_each(self):
        self.start()
        first=self.update('der Hund Hunde');second=self.update(I_DONT_KNOW_TEXT)
        async def run():
            return await asyncio.gather(Q.quiz_ru_de(first,self.ctx),Q.quiz_ru_de(second,self.ctx))
        self.run_async(run())
        self.assertEqual([(row['word_id'],row['is_correct']) for row in self.rows('quiz_events')],[('a',1),('b',0)])
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'c')
        self.assertEqual(self.ctx.user_data['session_correct_ru_de'],1)
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'],1)

    def test_deleting_visible_a_preserves_b_action_and_existing_counts(self):
        self.start();self.answer(I_DONT_KNOW_TEXT)
        point=quiz_resume.capture_return_point(self.ctx,QUIZ_RU_DE)
        before=copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.conn.execute("DELETE FROM words WHERE id='a'");self.conn.commit()
        with patch.object(Q,'_pick_due_entry',side_effect=AssertionError('new picker')):
            self.run_async(quiz_resume.resume_after_delete(self.update(),self.ctx,'a'))
        frame=self.ctx.user_data[W.FRAME]
        self.assertEqual((frame['question_id'],frame['action_key'],frame['revision']),
                         (before['question_id'],before['action_key'],before['revision']))
        self.assertEqual(frame['question_id'],'b')
        self.assertIsNone(frame['last_receipt'])
        self.assertEqual(self.ctx.user_data['session_wrong_ru_de'],1)

    def test_generic_control_recovery_delivery_cannot_grade_a_new_written_frame(self):
        from bot.common.study_lock import invalidate_study_frames
        self.start()
        self.conn.execute("""INSERT INTO study_deliveries
            (user_id,transport_update_id,flow_id,action_kind,state,result_json)
            VALUES(7,9000,'undo','undo','processing',?)""",
            (json.dumps({'tx_ids':[],'cursor':0,'outcomes':[]}),))
        self.conn.commit()
        old=self.update(I_DONT_KNOW_TEXT,ident=9001)
        result=answers.record_control_transition('synthetic',7,old.update_id,'old-flow','start',
            lambda conn:self.fail('recovery must precede current control resolver'))
        self.assertTrue(result['invalidate_frame'])
        invalidate_study_frames(self.ctx)
        self.start()
        before=copy.deepcopy(self.ctx.user_data)
        self.answer('',update=old)
        self.assertEqual(self.ctx.user_data,before)
        self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(self.ctx.user_data[W.FRAME]['question_id'],'b')

    def test_divisor_matrix_keeps_all_language_pairs_and_other_modes(self):
        for language in ('ru','uk','en'):
            self.pair=language+'_de'
            with patch.object(Q,'_mode_de_l1',return_value='de_'+language):
                expected={language+'_de':1.65,'de_'+language:2,'phrase_'+language+'_de':2,
                          'phrase_de_'+language:2,'pl_sg':2,'article':1,'other':1}
                for mode,divisor in expected.items():
                    with self.subTest(language=language,mode=mode):
                        self.assertEqual(Q._interval_divisor_for_mode(7,mode),divisor)


class IndependentNewDeliveryRecoveryQA(unittest.TestCase):
    def test_new_delivery_drains_unshown_pending_view_without_grading_it(self):
        for kind in ('original','retry','entry'):
            for boundary in ('picker','media'):
                with self.subTest(kind=kind,boundary=boundary):
                    h=fixture.WrittenTests();h.setUp()
                    try:
                        @Q.serialized_written_entry
                        async def entry(update,context):
                            return await Q._send_ru_de_prompt(update,context,None)
                        if kind!='entry':
                            h.start()
                            if kind=='retry':h.ctx.user_data['quiz_is_retry']=True
                        old=h.update(I_DONT_KNOW_TEXT)
                        operation=lambda update: h.run_async(entry(update,h.ctx)) if kind=='entry' else h.answer('',update=update)
                        target='_pick_due_entry' if boundary=='picker' else 'resolve_bot_word_media'
                        with patch.object(Q,target,side_effect=sqlite3.OperationalError('QA pending view failure')):
                            try:operation(old)
                            except sqlite3.OperationalError:pass
                        flow=h.ctx.user_data[W.FRAME]['flow_id']
                        fresh=h.update('der Hund Hunde')
                        self.assertNotEqual(fresh.update_id,old.update_id)
                        operation(fresh)
                        frame=h.ctx.user_data[W.FRAME]
                        self.assertEqual(frame['question_id'],'a' if kind=='entry' else 'b')
                        self.assertEqual(frame['flow_id'],flow)
                        self.assertTrue(fresh.message.sent)
                        events=h.rows('quiz_events')
                        self.assertEqual([(r['word_id'],r['is_correct']) for r in events],
                                         [('a',0)] if kind=='original' else [])
                        self.assertEqual(h.ctx.user_data['session_correct_ru_de'],0)
                        self.assertEqual(h.ctx.user_data['session_wrong_ru_de'],1 if kind=='original' else 0)
                        saved=copy.deepcopy(h.ctx.user_data)
                        operation(old)
                        self.assertEqual(h.ctx.user_data,saved)
                        self.assertEqual(h.rows('quiz_events'),events)
                    finally:h.doCleanups()


class IndependentRecoveryAPIQA(unittest.TestCase):
    def test_all_four_apis_bind_recovery_before_new_flow_and_late_replay(self):
        for api in ('original','correction','preview','control'):
            with self.subTest(api=api):
                h=fixture.WrittenTests();h.setUp()
                try:
                    h.conn.execute("""INSERT INTO study_deliveries
                        (user_id,transport_update_id,flow_id,action_kind,state,result_json)
                        VALUES(7,8000,'undo','undo','processing',?)""",
                        (json.dumps({'tx_ids':[],'cursor':0,'outcomes':[]}),))
                    h.conn.commit()
                    def forbidden(*args,**kwargs):
                        self.fail('old recovery delivery resolved a new target')
                    def call():
                        shared={'flow_id':'old','live_revision':forbidden}
                        if api=='original':
                            return answers.apply_original_answer('synthetic',7,'missing','ru_de','wrong','old',8001,99,target_resolver=forbidden,**shared)
                        if api=='correction':
                            return answers.correct_last_answer('synthetic',7,'missing-parent','old',8001,99,**shared)
                        if api=='preview':
                            return answers.apply_preview_transition('synthetic',7,'missing','preview_next','old',8001,99,transition=forbidden,**shared)
                        return answers.record_control_transition('synthetic',7,8001,'old','next',forbidden)
                    first=call()
                    self.assertTrue(first['invalidate_frame'])
                    self.assertEqual(h.rows('quiz_events'),[])
                    self.assertEqual(h.rows('transaction_log'),[])
                    self.assertEqual(h.rows('study_actions'),[])
                    self.assertEqual(h.conn.execute('SELECT state FROM study_deliveries WHERE transport_update_id=8001').fetchone()[0],'committed')
                    answers.apply_original_answer('synthetic',7,'a','ru_de','wrong','new',8002,1,flow_id='new',live_revision=1)
                    tables=('progress','quiz_events','transaction_log','study_actions','study_deliveries')
                    saved={table:h.rows(table) for table in tables}
                    with patch.object(answers,'_validate_target',side_effect=forbidden):
                        replay=call()
                    self.assertTrue(replay['replayed'])
                    for table in tables:self.assertEqual(h.rows(table),saved[table],table)
                finally:h.doCleanups()


def closed_flow_dispatch_script():
    path=Path(__file__).with_name('test_new_logic_core_conversation.py')
    raw=next(n.value for n in ast.walk(ast.parse(path.read_text()))
             if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'class NoNetworkRequest' in n.value)
    prefix=textwrap.dedent(raw).split('class FakeService:')[0]
    return prefix+textwrap.dedent(r"""
        import copy,json
        from telegram import MessageEntity
        from tests.test_new_logic_conjugation_handlers import HandlerTests,preparation,testing
        from bot.handlers import menu
        from bot.study import repository, transport_guard

        def message(bot,text,ident,command=False):
            value=Message(ident,NOW,Chat(UID,'private'),from_user=User(UID,'Learner',False),text=text,
                          entities=[MessageEntity('bot_command',0,6)] if command else None)
            value.set_bot(bot)
            return value

        async def scenario():
            h=HandlerTests();h.setUp()
            bot=FakeBot()
            app=Application.builder().bot(bot).job_queue(None).build()
            builder=RecordingBuilder(app)
            errors=[]
            try:
                h.f.add();h.f.ready()
                with ExitStack() as stack:
                    stack.enter_context(patch.dict('os.environ',{'BOT_TOKEN':'123456:synthetic'}))
                    for name in ('load_dotenv','_configure_logging','require_supported_ptb','_prepare_storage',
                                 '_configure_learning_reminders','_configure_fsrs_reports','_configure_access_reminders','_configure_owner_digest'):
                        stack.enter_context(patch.object(main,name))
                    stack.enter_context(patch.object(main,'Application',SimpleNamespace(builder=lambda:builder)))
                    stack.enter_context(patch.object(main,'AIORateLimiter',return_value=object()))
                    stack.enter_context(patch.object(main,'run_polling_safely'))
                    stack.enter_context(patch.object(repository,'transaction',h.f.connection))
                    stack.enter_context(patch.object(transport_guard,'read_delivery',
                        side_effect=lambda db_path,uid,tid: transport_guard.inspect_delivery(h.f.conn,uid,tid)))
                    stack.enter_context(patch.object(menu,'_is_owner_only',return_value=True))
                    stack.enter_context(patch.object(menu,'access_allowed',return_value=True))
                    stack.enter_context(patch.object(menu,'get_setting',return_value='en'))
                    stack.enter_context(patch.object(menu,'_user_interface_lang',return_value='en'))
                    stack.enter_context(patch.object(menu,'_main_menu_markup',return_value=None))
                    stack.enter_context(patch.object(menu,'_tests_menu_keys',return_value=[['Conjugation']]))
                    stack.enter_context(patch.object(menu,'_action_key_from_text',side_effect=lambda text,uid:{'Tests':'tests','Conjugation':'conjugation'}.get(text,'')))
                    main.main()
                    async def on_error(update,context):errors.append(context.error)
                    app.error_handlers.clear();app.add_error_handler(on_error)
                    await app.initialize()
                    conv=app.handlers[0][0];key=(UID,UID)
                    h.ctx.user_data=app.user_data[UID];h.ctx.bot=bot
                    await testing.start_conjugation_test(h.update(transport=900),h.ctx)
                    conv._conversations[key]=main.CONJUGATION_TEST
                    token=next(k for k,v in h.frame()['callbacks'].items() if v['operation']=='reset')
                    block=dict(h.f.block())
                    # A previously committed written answer is transport identity,
                    # not permission to restore its previous conversation state.
                    h.f.conn.execute("INSERT INTO study_deliveries(user_id,transport_update_id,flow_id,action_kind,state,result_json) VALUES(7,950,'old-written','answer','committed',?)",
                        (json.dumps({'flow_id':'old-written','return_state':main.QUIZ_RU_DE,'mode':'ru_de'}),))
                    h.f.conn.commit()
                    old_written=Update(950,message=message(bot,'der Hund Hunde',90));old_written.set_bot(bot)
                    frame=copy.deepcopy(h.frame());sent=len(bot.sent)
                    await app.process_update(old_written)
                    assert not errors,errors
                    assert conv._conversations[key]==main.CONJUGATION_TEST
                    assert h.frame()==frame and len(bot.sent)==sent
                    assert h.answers()==0 and h.f.block()==block

                    start=Update(1000,message=message(bot,'/start',100,True));start.set_bot(bot)
                    await app.process_update(start)
                    assert not errors,errors
                    assert conv._conversations[key]==main.MENU
                    assert preparation.FRAME not in app.user_data[UID]
                    assert h.f.block()==block and h.answers()==0
                    ledger=h.f.conn.execute('SELECT COUNT(*) FROM study_deliveries').fetchone()[0]
                    sent=len(bot.sent)
                    query=CallbackQuery('late-reset',User(UID,'Learner',False),'synthetic-chat-instance',
                        message=message(bot,'old test',101),data='conj:reset:'+token)
                    query.set_bot(bot)
                    callback=Update(1001,callback_query=query);callback.set_bot(bot)
                    await app.process_update(callback)
                    assert not errors,errors
                    assert conv._conversations[key]==main.MENU
                    assert preparation.FRAME not in app.user_data[UID]
                    assert h.f.block()==block and h.answers()==0
                    assert h.f.conn.execute('SELECT COUNT(*) FROM study_deliveries').fetchone()[0]==ledger
                    assert len(bot.sent)==sent
                    await app.process_update(old_written)
                    assert not errors,errors
                    assert conv._conversations[key]==main.MENU and h.f.block()==block
                    tests=Update(1002,message=message(bot,'Tests',102));tests.set_bot(bot)
                    await app.process_update(tests)
                    assert conv._conversations[key]==main.TESTS_MENU
                    prepare=Update(1003,message=message(bot,'Conjugation',103));prepare.set_bot(bot)
                    await app.process_update(prepare)
                    assert not errors,errors
                    assert conv._conversations[key]==main.CONJUGATION_MENU
                    frame=copy.deepcopy(h.frame())
                    await app.process_update(old_written)
                    assert not errors,errors
                    assert conv._conversations[key]==main.CONJUGATION_MENU
                    assert h.frame()==frame and h.f.block()==block
                    # Replaying the earlier /start cannot close this newer flow.
                    await app.process_update(start)
                    assert not errors,errors
                    assert conv._conversations[key]==main.CONJUGATION_MENU and h.frame()==frame
                    assert NoNetworkRequest.attempts==0
                    print('actual /start + late callback + written replay across test/MENU/preparation: network=0, SQLite=:memory:')
                    await app.shutdown()
            finally:h.doCleanups()
        asyncio.run(scenario())
    """)


class IndependentClosedFlowQA(unittest.TestCase):
    def test_real_start_closes_tokens_and_preserves_block_and_newer_flow(self):
        result=subprocess.run([sys.executable,'-B','-c',closed_flow_dispatch_script()],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('network=0',result.stdout)

    def test_real_start_send_failure_replay_returns_closed_menu_not_old_test_state(self):
        lines=closed_flow_dispatch_script().splitlines()
        index=next(i for i,line in enumerate(lines) if line.strip()=='await app.process_update(start)')
        indent=lines[index][:-len(lines[index].lstrip())]
        lines[index:index+1]=[
            indent+'from telegram.error import TimedOut',
            indent+"with patch.object(FakeBot,'send_message',side_effect=TimedOut('QA start menu send failure')):",
            indent+'    await app.process_update(start)',
            indent+'assert len(errors)<=1 and all(isinstance(e,TimedOut) for e in errors),errors',
            indent+'errors.clear()',
            indent+'await app.process_update(start)']
        result=subprocess.run([sys.executable,'-B','-c','\n'.join(lines)],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('network=0',result.stdout)

    def test_replayed_conjugation_recovery_cannot_close_new_preparation(self):
        from tests.test_new_logic_conjugation_handlers import HandlerTests,preparation,t
        h=HandlerTests();h.setUp()
        try:
            h.f.add();h.call(preparation.start_conjugation)
            h.pending_empty_undo();old=h.update(t('en','conjugation_next'))
            h.call(preparation.conjugation_input,old)
            self.assertNotIn(preparation.FRAME,h.ctx.user_data)
            h.call(preparation.start_conjugation)
            before=copy.deepcopy(h.ctx.user_data);block=h.f.block()
            self.assertEqual(h.call(preparation.conjugation_input,old),preparation.CONJUGATION_MENU)
            self.assertEqual(h.ctx.user_data,before)
            self.assertEqual(h.f.block(),block)
        finally:h.doCleanups()


if __name__ == '__main__':
    unittest.main()
