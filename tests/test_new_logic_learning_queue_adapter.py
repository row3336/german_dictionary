"""K15 actual PTB ConversationHandler acceptance, with an offline ExtBot."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


def actual_suite():
    import asyncio
    from contextlib import ExitStack, closing
    from copy import deepcopy
    from datetime import datetime, timezone
    import sqlite3
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch

    from telegram import Chat, Message, Update, User
    from telegram.ext import Application, ConversationHandler, ExtBot
    from telegram.request import BaseRequest
    from bot import main, storage, learning_requests, learning_store
    from bot.app_config import LEARNING_MENU, LEARNING_QUIZ, QUIZ_RU_DE, ADD_EDIT
    from bot.handlers import learning as L, quiz
    from bot.i18n import t
    from bot.study import generations, repository
    from bot.written.state import establish_view, FRAME

    NOW=datetime(2026,10,23,12,tzinfo=timezone.utc)
    STAND=Path('/Users/wf/py/_agent_work/888_german_dictionary/k15_impl_20261023')

    class OfflineRequest(BaseRequest):
        attempts=0
        @property
        def read_timeout(self): return None
        async def initialize(self): pass
        async def shutdown(self): pass
        async def do_request(self,*a,**kw):
            type(self).attempts+=1
            raise AssertionError('network forbidden')

    class OfflineBot(ExtBot):
        __slots__=('sent','fail_once')
        def __init__(self):
            super().__init__('12345:FAKE',request=OfflineRequest(),get_updates_request=OfflineRequest())
            object.__setattr__(self,'sent',[]);object.__setattr__(self,'fail_once',False)
        async def initialize(self): self._bot_user=User(55,'Offline',True,username='offline_bot')
        async def shutdown(self): pass
        async def send_message(self,chat_id,text,**kw):
            if self.fail_once:
                object.__setattr__(self,'fail_once',False)
                raise RuntimeError('definite send failure')
            msg=Message(10000+len(self.sent),NOW,Chat(chat_id,'private'),from_user=self._bot_user,text=text,reply_markup=kw.get('reply_markup'))
            msg.set_bot(self);self.sent.append(msg);return msg
        async def set_message_reaction(self,*a,**kw): return True

    class Builder:
        def __init__(self,app): self.app=app
        def __getattr__(self,n): return lambda *a,**kw:self
        def build(self): return self.app

    class AdapterTests(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            asyncio.get_running_loop().set_debug(False)
            STAND.mkdir(parents=True,exist_ok=True)
            self.runtime=Path(tempfile.mkdtemp(prefix='actual-ptb-',dir=STAND))
            self.db=str(self.runtime/'synthetic.sqlite');self.stack=ExitStack()
            self.stack.enter_context(patch.dict(os.environ,{'DB_PATH':self.db,'TMPDIR':str(self.runtime),'OWNER_ID':'999','BOT_TOKEN':'12345:FAKE'}))
            storage.init_db(self.db)
            with repository.transaction(self.db) as c:
                c.execute("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES (7,'approved','en','ru'),(999,'approved','ru','ru')")
                c.execute("INSERT INTO topics(name,level) VALUES ('one','A1')")
                c.executemany("INSERT INTO words(id,type,german,russian,ukrainian,english,topic) VALUES (?,'adverb',?,?,?,?, 'one')",[(x,x,'ru-'+x,'uk-'+x,'en-'+x) for x in 'abc'])
                c.execute("INSERT INTO user_settings(user_id,key,value) VALUES (7,?, '1')",(L.LEARNING_DAILY_QTY_KEY,))
            self.bot=OfflineBot();self.app=Application.builder().bot(self.bot).job_queue(None).build()
            for n in ('load_dotenv','_configure_logging','require_supported_ptb','_prepare_storage','_configure_learning_reminders','_configure_fsrs_reports','_configure_access_reminders','_configure_owner_digest'):
                self.stack.enter_context(patch.object(main,n))
            self.stack.enter_context(patch.object(main,'Application',SimpleNamespace(builder=lambda:Builder(self.app))))
            self.stack.enter_context(patch.object(main,'AIORateLimiter',return_value=object()))
            self.stack.enter_context(patch.object(main,'run_polling_safely'))
            main.main();self.conv=next(h for h in self.app.handlers[0] if isinstance(h,ConversationHandler))
            self.errors=[]
            async def error(update,context): self.errors.append(context.error)
            self.app.error_handlers.clear();self.app.add_error_handler(error)
            await self.app.initialize();self.ident=1000
            self.ctx=SimpleNamespace(user_data=self.app.user_data[7])

        async def asyncTearDown(self):
            self.assertEqual(self.errors,[]);self.assertEqual(OfflineRequest.attempts,0)
            await self.app.shutdown();self.stack.close()

        def rows(self,table):
            with closing(sqlite3.connect(self.db)) as c:
                c.row_factory=sqlite3.Row
                return [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]

        def selected(self,wid):
            with repository.transaction(self.db) as c:
                return generations.selected_entry(c,7,storage._row_to_entry(c.execute('SELECT * FROM words WHERE id=?',(wid,)).fetchone()))

        def state(self,value): self.conv._conversations[(7,7)]=value

        async def text(self,text,update=None):
            self.ident+=1
            if update is None:
                message=Message(self.ident,NOW,Chat(7,'private'),from_user=User(7,'Learner',False),text=text)
                message.set_bot(self.bot);update=Update(self.ident,message=message);update.set_bot(self.bot)
            await self.app.process_update(update)
            return update

        async def test_l06_queued_advances_once_noops_keep_frame_and_correction(self):
            self.state(QUIZ_RU_DE)
            establish_view(self.ctx,'ru_de',QUIZ_RU_DE,self.selected('a'))
            self.ctx.user_data.update(retry_queue={'ru_de':['a']},retry_counts={'ru_de':{'a':3}})
            with patch.object(quiz,'_pick_due_entry',return_value=self.selected('b')) as picker:
                update=await self.text(t('en','send_learn'))
                self.assertEqual(picker.call_count,1)
                await self.text('',update)
                self.assertEqual(picker.call_count,1)
            self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')
            self.assertEqual(self.ctx.user_data['retry_counts']['ru_de']['a'],3)
            self.assertEqual(self.rows('quiz_events'),[]);self.assertEqual(self.rows('transaction_log'),[])
            with repository.transaction(self.db) as c: learning_requests.enqueue(c,7,'b',0,NOW)
            before=deepcopy(self.ctx.user_data[FRAME])
            with patch.object(quiz,'_pick_due_entry',side_effect=AssertionError('noop picker')):
                await self.text(t('en','send_learn'))
            self.assertEqual(self.ctx.user_data[FRAME]['action_key'],before['action_key'])
            self.assertEqual(self.ctx.user_data[FRAME]['revision'],before['revision'])

        async def test_l06_written_failed_send_same_and_fresh_transport_do_not_grade_b(self):
            self.state(QUIZ_RU_DE);establish_view(self.ctx,'ru_de',QUIZ_RU_DE,self.selected('a'))
            object.__setattr__(self.bot,'fail_once',True)
            with patch.object(quiz,'_pick_due_entry',return_value=self.selected('b')) as picker:
                old=await self.text(t('en','send_learn'))
                self.assertEqual(len(self.errors),1);self.assertIn('definite send failure',str(self.errors.pop()))
                await self.text('fresh wrong answer')
                await self.text('',old)
                self.assertEqual(picker.call_count,1)
            self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')
            self.assertEqual(self.rows('quiz_events'),[])
            self.assertEqual(len(self.rows('learning_requests')),1)

        async def test_l05_l08_add_is_explicit_atomic_replay_and_new_add_uses_capacity(self):
            with repository.transaction(self.db) as c: learning_requests.enqueue(c,7,'a',0,NOW)
            self.state(LEARNING_MENU)
            old=await self.text(t('en','add_for_learning'))
            self.assertEqual([r['word_id'] for r in self.rows('learning_items')],['a'])
            self.assertEqual(self.rows('learning_requests')[0]['status'],'consumed')
            with repository.transaction(self.db) as c:
                learning_requests.enqueue(c,7,'b',0,NOW)
                c.execute("UPDATE user_settings SET value='2' WHERE user_id=7 AND key=?",(L.LEARNING_DAILY_QTY_KEY,))
            await self.text('',old)
            self.assertEqual(len(self.rows('learning_items')),1)
            await self.text(t('en','add_for_learning'))
            self.assertEqual([r['word_id'] for r in self.rows('learning_items')],['a','b'])
            self.assertEqual(self.rows('transaction_log'),[])

        def clarify(self,target='a',current='a'):
            from bot.common.quiz_resume import capture_return_point
            establish_view(self.ctx,'ru_de',QUIZ_RU_DE,self.selected(current))
            capture_return_point(self.ctx,QUIZ_RU_DE)
            self.ctx.user_data.update(edit_source='quiz',edit_return_state=QUIZ_RU_DE,
                edit_stage='pick_field',quiz_edit_entry=self.selected(target),
                retry_queue={'ru_de':[target]},retry_counts={'ru_de':{target:3}})
            self.state(ADD_EDIT)

        async def test_l06_clarify_other_b_retains_action_and_wrong_answer_right(self):
            self.clarify(target='a',current='b')
            original={'word_id':'a','answer_kind':'wrong','action_key':'old-answer'}
            self.ctx.user_data[FRAME]['last_receipt']=deepcopy(original)
            action=self.ctx.user_data[FRAME]['action_key']
            with patch.object(quiz,'_pick_due_entry',side_effect=AssertionError('clarify changed B')):
                await self.text(t('en','send_learn'))
            self.assertEqual(self.ctx.user_data[FRAME]['action_key'],action)
            self.assertEqual(self.ctx.user_data[FRAME]['last_receipt'],original)
            self.assertEqual(self.ctx.user_data['quiz_entry']['id'],'b')
            self.assertEqual(self.ctx.user_data['retry_queue']['ru_de'],[])
            self.assertEqual(self.ctx.user_data['retry_counts']['ru_de']['a'],3)
            self.assertEqual(self.rows('quiz_events'),[])

        async def test_l06_l08_clarify_failed_media_render_send_same_fresh_bound_return(self):
            for stage in ('media','render','send'):
                for fresh in (False,True):
                    with self.subTest(stage=stage,fresh=fresh):
                        probe=AdapterTests('runTest');await probe.asyncSetUp()
                        try:
                            probe.clarify()
                            target={'media':'resolve_bot_word_media','render':'_written_question','send':'_reply_text_with_retry'}[stage]
                            with patch.object(quiz,'_pick_due_entry',return_value=probe.selected('b')) as picker:
                                with patch.object(quiz,target,side_effect=RuntimeError('clarify return fault')):
                                    old=await probe.text(t('en','send_learn'))
                                self.assertEqual(len(probe.errors),1)
                                self.assertIn('clarify return fault',str(probe.errors.pop()))
                                self.assertEqual(probe.conv._conversations[(7,7)],ADD_EDIT)
                                self.assertTrue(probe.ctx.user_data[FRAME]['learning_send_return_pending'])
                                # The old edit target must not be reloaded on recovery.
                                from bot.handlers import clarify_fields as C
                                with patch.object(C,'get_word_by_id',side_effect=AssertionError('reloaded old edit A')):
                                    await probe.text('fresh input' if fresh else '',None if fresh else old)
                                self.assertEqual(picker.call_count,1)
                            self.assertEqual(probe.conv._conversations[(7,7)],QUIZ_RU_DE)
                            self.assertNotIn('learning_send_return_pending',probe.ctx.user_data[FRAME])
                            self.assertEqual(probe.ctx.user_data['quiz_entry']['id'],'b')
                            self.assertEqual(probe.ctx.user_data['retry_counts']['ru_de']['a'],3)
                            self.assertEqual(probe.rows('quiz_events'),[])
                            # Late receipt cannot replace a subsequently opened flow.
                            establish_view(probe.ctx,'ru_de',QUIZ_RU_DE,probe.selected('c'))
                            current=deepcopy(probe.ctx.user_data[FRAME])
                            await probe.text('',old)
                            self.assertEqual(probe.ctx.user_data[FRAME],current)
                        finally:
                            await probe.asyncTearDown()

        async def test_l05_new_cycle_preview_finalize_grade_and_correction(self):
            with repository.transaction(self.db) as c: learning_requests.enqueue(c,7,'a',0,NOW)
            self.state(LEARNING_MENU)
            await self.text(t('en','add_for_learning'))
            await self.text(t('en','learning_start'))
            self.assertEqual(self.ctx.user_data[L.FRAME]['phase'],'preview')
            self.assertEqual(self.ctx.user_data[L.FRAME]['current']['entry']['learning_lifecycle'],1)
            await self.text(t('en','learning_next'))
            self.assertEqual(self.rows('learning_items')[0]['first_due_pending'],0)
            await self.text('wrong')
            self.assertEqual(self.rows('learning_items')[0]['recovery_remaining'],3)
            await self.text(t('en','input_err'))
            self.assertEqual(self.rows('learning_items')[0]['step_index'],1)
            self.assertEqual(len(self.rows('quiz_events')),1)
            self.assertEqual(self.rows('quiz_events')[0]['is_correct'],1)

        async def test_l09_old_learning_bound_controls_and_grade_stale_before_capture(self):
            with repository.transaction(self.db) as c:
                batch=learning_store.get_or_create_batch(c,7,1,NOW)
                learning_store.add_items(c,7,['a'],batch,NOW)
                learning_store.preview_transition(c,7,'a','preview_next',NOW,0)
                c.execute("UPDATE learning_items SET next_due='2000-01-01T00:00:00Z' WHERE user_id=7")
            self.state(LEARNING_MENU);await self.text(t('en','learning_start'))
            self.assertEqual(self.conv._conversations[(7,7)],LEARNING_QUIZ)
            with repository.transaction(self.db) as c:
                c.execute("UPDATE learning_items SET status='completed' WHERE user_id=7")
                learning_requests.enqueue(c,7,'a',0,NOW)
                learning_requests.add_candidates(c,7,1,topics=['one'],now=NOW)
            before=self.rows('learning_items')
            with patch.object(repository,'capture_snapshot',side_effect=AssertionError('old lifecycle capture')):
                await self.text(t('en','learning_skip'))
            self.assertEqual(self.rows('learning_items'),before)
            self.assertEqual(self.rows('quiz_events'),[])
            self.assertEqual(self.conv._conversations[(7,7)],LEARNING_MENU)

    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AdapterTests))
    raise SystemExit(0 if result.wasSuccessful() else 1)


class RealLearningQueueDispatch(unittest.TestCase):
    def test_l05_l06_l08_l09_actual_ptb(self):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--actual'],capture_output=True,text=True,env=os.environ.copy(),timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        print(result.stderr)


if __name__=='__main__':
    if '--actual' in sys.argv: actual_suite()
    else: unittest.main()
