"""Independent calendar, multi-locale and interrupted-dispatch notification checks."""

import asyncio
import ast
from contextlib import closing
from datetime import datetime
import sqlite3
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_new_logic_notifications as fixture
from bot.app_config import MENU
from bot.i18n import t
from bot.storage import (add_word, get_user_setting, register_or_update_user,
    select_due, set_user_dict_lang, set_user_interface_lang, set_user_status)

R, H, N = fixture.R, fixture.H, fixture.N


class IndependentNotificationQA(unittest.TestCase):
    setUp = fixture.NotificationTest.setUp
    tick = fixture.NotificationTest.tick
    enable_new = fixture.NotificationTest.enable_new

    def test_nine_ui_dictionary_pairs_dispatch_actual_exact_written_unfiltered_queue(self):
        words = [(f"qa-w{i}", "nomen") for i in range(6)] + [("qa-phrase", "phrase")]
        for wid, typ in words:
            add_word(self.db, {"id": wid, "type": typ, "german": wid, "article": "das",
                "russian": "ru", "ukrainian": "uk", "english": "en",
                "topic": "one" if wid == "qa-w0" else "two"})
        users, expected = [], {}
        for di, dictionary in enumerate(("ru", "uk", "en")):
            for ui, interface in enumerate(("ru", "uk", "en")):
                uid = 300 + di * 3 + ui
                users.append(uid)
                register_or_update_user(self.db, uid, "qa", "QA")
                set_user_status(self.db, uid, "approved")
                set_user_interface_lang(self.db, uid, interface)
                set_user_dict_lang(self.db, uid, dictionary)
                mode = dictionary + "_de"
                total = di + 2
                with closing(sqlite3.connect(self.db)) as conn:
                    for wid, typ in words:
                        conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES (?,?,?,0,'2020-01-01')",
                                     (uid, wid, mode))
                        due = "2020-01-01" if typ == "phrase" or int(wid[-1]) < total else "2099-01-01"
                        conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES (?,?,?,1,1,2.5,?)",
                                     (uid, wid, mode, due))
                    # A correct oral answer today must not exclude this written card.
                    conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES (?,?,?,1,'2026-10-20')",
                                 (uid, "qa-w0", "de_" + dictionary))
                    conn.commit()
                filtered = select_due(self.db, uid, mode, topics=["one"], today=datetime(2026,10,20).date(), exclude_correct_today=True)
                self.assertEqual([row["id"] for row in filtered], ["qa-w0"])
                expected[uid] = (interface, dictionary, total)
        with patch.object(R, "list_active_users", return_value=users):
            self.tick("2026-10-20T18:00:00+02:00")
            self.tick("2026-10-20T19:00:00+02:00")
        self.assertEqual(self.bot.send_message.await_count, 9)
        direction = {"ru": "Ru > De", "uk": "Ua > De", "en": "En > De"}
        for call in self.bot.send_message.call_args_list:
            data = call.kwargs
            language, dictionary, total = expected[data["chat_id"]]
            self.assertEqual(data["text"], t(language,"reminder_fsrs_due").format(total=total,dir=direction[dictionary]))
            button = data["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.text, t(language,"notifications_title"))
            self.assertEqual(button.callback_data,"notify:quiz")
            self.assertEqual(get_user_setting(self.db,data["chat_id"],"fsrs_afternoon_reminder_date"),"2026-10-20")

    def test_old_forecast_uses_own_phrase_schedule_separately_from_written_reminder(self):
        from bot.storage import fsrs_due_forecast
        for wid, typ in (("qa-fword","nomen"),("qa-fdue","phrase"),("qa-ffuture","phrase")):
            add_word(self.db,{"id":wid,"type":typ,"german":wid,"russian":"r","ukrainian":"u","english":"e"})
        for index, mode in enumerate(("ru_de","uk_de","en_de","de_ru","de_uk","de_en")):
            uid = 500 + index
            register_or_update_user(self.db,uid,"qa","QA")
            set_user_status(self.db,uid,"approved")
            dictionary = mode[:2] if mode.endswith("_de") else mode[-2:]
            set_user_dict_lang(self.db,uid,dictionary)
            with closing(sqlite3.connect(self.db)) as conn:
                for wid, exact, due in (("qa-fword",mode,"2099-01-01"),
                    ("qa-fdue","phrase_"+mode,"2020-01-01"),
                    ("qa-ffuture","phrase_"+mode,"2099-01-01"),
                    ("qa-fdue",mode,"2099-01-01"),
                    ("qa-ffuture",mode,"2020-01-01")):
                    conn.execute("INSERT INTO progress(user_id,word_id,mode,interval,repetitions,ease,due) VALUES (?,?,?,1,1,2.5,?)",(uid,wid,exact,due))
                for wid in ("qa-fword","qa-fdue","qa-ffuture"):
                    conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES (?,?,?,0,'2020-01-01')",(uid,wid,mode))
                conn.commit()
            with self.subTest(mode=mode):
                self.assertEqual(fsrs_due_forecast(self.db,uid,mode)["total"],1)
                if mode.endswith("_de"):
                    self.assertEqual(R._count_quiz_due(self.db,uid,datetime(2026,10,20).date()),0)

    def test_midnight_drops_old_day_plan_then_sends_only_current_day(self):
        before = datetime.fromisoformat("2026-10-20T23:59:59+02:00")
        after = datetime.fromisoformat("2026-10-21T00:00:00+02:00")
        with patch.object(R,"_count_quiz_due",return_value=1), \
             patch.object(R,"_now_berlin",side_effect=[before,after,after]):
            asyncio.run(R._fsrs_afternoon_reminder_job(self.ctx))
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(get_user_setting(self.db,fixture.USER,"fsrs_afternoon_reminder_date"))
        N.set_preference(self.db,fixture.USER,"quiz",time="00:00")
        with patch.object(R,"_count_quiz_due",return_value=1):
            self.tick("2026-10-21T00:00:00+02:00")
            self.tick("2026-10-21T00:01:00+02:00")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(get_user_setting(self.db,fixture.USER,"fsrs_afternoon_reminder_date"),"2026-10-21")

    def test_cancelled_dispatch_releases_overlap_guard_without_marking_success(self):
        async def scenario():
            entered = asyncio.Event()
            async def pending_send(**kwargs):
                entered.set()
                await asyncio.Event().wait()
            self.bot.send_message.side_effect = pending_send
            task = asyncio.create_task(R._fsrs_afternoon_reminder_job(self.ctx))
            await asyncio.wait_for(entered.wait(),2)
            self.assertTrue(R._dispatcher_running)
            await R._fsrs_afternoon_reminder_job(self.ctx)
            self.assertEqual(self.bot.send_message.await_count,1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(R._dispatcher_running)
            self.assertIsNone(get_user_setting(self.db,fixture.USER,"fsrs_afternoon_reminder_date"))
            self.bot.send_message.side_effect = None
            await R._fsrs_afternoon_reminder_job(self.ctx)
        now = datetime.fromisoformat("2026-10-20T18:00:00+02:00")
        with patch.object(R,"_now_berlin",return_value=now), patch.object(R,"_count_quiz_due",return_value=1):
            asyncio.run(scenario())
        self.assertEqual(self.bot.send_message.await_count,2)
        self.assertEqual(get_user_setting(self.db,fixture.USER,"fsrs_afternoon_reminder_date"),"2026-10-20")

    def test_preference_changed_during_prior_users_send_is_read_before_next_user(self):
        async def send(**kwargs):
            if kwargs["chat_id"] == fixture.USER:
                N.set_preference(self.db,fixture.OTHER,"quiz",enabled=False)
        self.bot.send_message.side_effect = send
        with patch.object(R,"list_active_users",return_value=[fixture.USER,fixture.OTHER]), \
             patch.object(R,"_count_quiz_due",return_value=1):
            self.tick("2026-10-20T18:00:00+02:00")
        self.assertEqual([call.kwargs["chat_id"] for call in self.bot.send_message.call_args_list],[fixture.USER])
        self.assertIsNone(get_user_setting(self.db,fixture.OTHER,"fsrs_afternoon_reminder_date"))

    def test_revoked_user_cannot_mutate_preferences_from_stale_time_or_toggle_state(self):
        for status in ("pending","rejected"):
            for language in ("ru","uk","en"):
                with self.subTest(status=status,language=language):
                    set_user_status(self.db,fixture.OTHER,status)
                    context = SimpleNamespace(user_data={"notification_kind":"quiz"})
                    value = fixture.update("01:23",fixture.OTHER)
                    self.assertEqual(asyncio.run(H.notification_time_input(value,context)),MENU)
                    toggle = fixture.update(t(language,"notification_disable"),fixture.OTHER)
                    self.assertEqual(asyncio.run(H.notification_menu_input(toggle,context)),MENU)
                    preference = N.get_preference(self.db,fixture.OTHER,"quiz")
                    self.assertEqual(preference,N.ReminderPreference(True,"18:00"))


    def test_real_nonempty_finalize_and_undo_keep_current_berlin_day_suppressed(self):
        from bot import learning_store as store
        from bot.study import answers,repository
        now=datetime.fromisoformat('2026-10-20T22:30:00+00:00')
        N.set_preference(self.db,fixture.USER,'quiz',enabled=False)
        self.enable_new(time='00:00')
        for wid in ('qa-start-a','qa-start-b'):
            add_word(self.db,{'id':wid,'type':'nomen','german':wid,'russian':'r'})
        with repository.transaction(self.db) as conn:
            batch=store.get_or_create_batch(conn,fixture.USER,20,now)
            store.add_items(conn,fixture.USER,['qa-start-a','qa-start-b'],batch,now)
            store.preview_transition(conn,fixture.USER,'qa-start-a','preview_next',now,86400)
        self.assertTrue(N.due_to_notify(self.db,fixture.USER,'new_words',now))
        result=answers.apply_preview_transition(self.db,fixture.USER,'qa-start-b','preview_next','qa-finalize',7000,1,now,
            flow_id='qa-learning',live_revision=1,
            affected_learning_ids=lambda conn:store.preview_affected_ids(conn,fixture.USER,'qa-start-b'),
            transition=lambda conn,before,clock:store.preview_transition(conn,fixture.USER,'qa-start-b','preview_next',clock,86400))
        self.assertEqual(set(result['after']['newly_started_ids']),{'qa-start-a','qa-start-b'})
        self.assertEqual(get_user_setting(self.db,fixture.USER,N.LEARNING_NEW_START_KEY),'2026-10-21')
        with repository.transaction(self.db) as conn:
            self.assertTrue(all(row[0].startswith('2026-10-21T22:30') for row in conn.execute('SELECT next_due FROM learning_items WHERE user_id=?',(fixture.USER,))))
        self.tick('2026-10-21T00:31:00+02:00')
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(answers.undo_steps(self.db,fixture.USER,1,7001)['restored'],1)
        self.assertEqual(get_user_setting(self.db,fixture.USER,N.LEARNING_NEW_START_KEY),'2026-10-21')
        self.tick('2026-10-21T12:00:00+02:00')
        self.bot.send_message.assert_not_awaited()
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE learning_items SET status='completed' WHERE user_id=?",(fixture.USER,))
        self.tick('2026-10-21T23:59:00+02:00')
        self.bot.send_message.assert_not_awaited()
        self.tick('2026-10-22T00:00:00+02:00')
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs['text'],t('en','reminder_new_words'))

    def test_real_skip_and_delete_all_without_first_due_leave_reminder_eligible(self):
        from bot import learning_store as store
        from bot.study import answers,repository
        from bot.storage import delete_word
        now=datetime.fromisoformat('2026-10-20T12:30:00+00:00')
        N.set_preference(self.db,fixture.USER,'quiz',enabled=False)
        self.enable_new()
        for wid in ('qa-skip-a','qa-delete-b'):
            add_word(self.db,{'id':wid,'type':'nomen','german':wid,'russian':'r'})
        with repository.transaction(self.db) as conn:
            batch=store.get_or_create_batch(conn,fixture.USER,20,now)
            store.add_items(conn,fixture.USER,['qa-skip-a','qa-delete-b'],batch,now)
        skipped=answers.apply_preview_transition(self.db,fixture.USER,'qa-skip-a','skip','qa-skip',7100,1,now,
            flow_id='qa-learning',live_revision=1,
            affected_learning_ids=lambda conn:store.preview_affected_ids(conn,fixture.USER,'qa-skip-a'),
            transition=lambda conn,before,clock:store.preview_transition(conn,fixture.USER,'qa-skip-a','skip',clock,86400))
        self.assertEqual(skipped['after']['newly_started_ids'],[])
        delete_word(self.db,'qa-delete-b')
        self.assertIsNone(get_user_setting(self.db,fixture.USER,N.LEARNING_NEW_START_KEY))
        self.assertTrue(N.due_to_notify(self.db,fixture.USER,'new_words',now))
        self.tick('2026-10-20T14:30:00+02:00')
        self.bot.send_message.assert_awaited_once()
        self.assertIsNone(get_user_setting(self.db,fixture.OTHER,N.LEARNING_NEW_START_KEY))

    def test_real_main_inline_permanent_time_and_late_replay_use_same_sql_preferences(self):
        from pathlib import Path
        raw=next(n.value for n in ast.walk(ast.parse(Path(__file__).with_name('test_new_logic_core_conversation.py').read_text()))
                 if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'class NoNetworkRequest' in n.value)
        prefix=textwrap.dedent(raw).split('class FakeService:')[0].replace('UID = 7','UID = 101')
        script=prefix+textwrap.dedent(r"""
            from tests.test_new_logic_notifications import NotificationTest
            from bot.notifications import handlers as notifications,settings as preferences
            from bot.storage import get_user_setting
            from unittest.mock import AsyncMock

            def message(bot,text,ident):
                value=Message(ident,NOW,Chat(UID,'private'),from_user=User(UID,'Learner',False),text=text)
                value.set_bot(bot);return value

            async def scenario():
                fixture=NotificationTest();fixture.setUp()
                bot=FakeBot();app=Application.builder().bot(bot).job_queue(None).build()
                builder=RecordingBuilder(app);errors=[]
                try:
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict('os.environ',{'BOT_TOKEN':'123456:synthetic'}))
                        for name in ('load_dotenv','_configure_logging','require_supported_ptb','_prepare_storage',
                                     '_configure_learning_reminders','_configure_fsrs_reports','_configure_access_reminders','_configure_owner_digest'):
                            stack.enter_context(patch.object(main,name))
                        stack.enter_context(patch.object(main,'Application',SimpleNamespace(builder=lambda:builder)))
                        stack.enter_context(patch.object(main,'AIORateLimiter',return_value=object()))
                        stack.enter_context(patch.object(main,'run_polling_safely'))
                        main.main()
                        async def on_error(update,context):errors.append(context.error)
                        app.error_handlers.clear();app.add_error_handler(on_error)
                        await app.initialize()
                        conv=app.handlers[0][0];key=(UID,UID)
                        for index,old_state in enumerate((None,main.MENU,main.LEARNING_QUIZ)):
                            if old_state is None:conv._conversations.pop(key,None)
                            else:conv._conversations[key]=old_state
                            app.user_data[UID]['learning_frame']={'flow_id':'old-learning'}
                            query=CallbackQuery('notify-'+str(index),User(UID,'Learner',False),'synthetic',
                                message=message(bot,'reminder',10+index),data='notify:new_words')
                            query.set_bot(bot)
                            opening=Update(2000+index*10,callback_query=query);opening.set_bot(bot)
                            await app.process_update(opening)
                            assert not errors,errors
                            assert conv._conversations[key]==main.NOTIFICATION_SETTINGS_MENU
                            assert 'learning_frame' not in app.user_data[UID]
                            for offset,text,expected_state in ((1,t('en','notification_change_time'),main.NOTIFICATION_SETTINGS_TIME),
                                    (2,'24:00',main.NOTIFICATION_SETTINGS_TIME),(3,'15:30',main.NOTIFICATION_SETTINGS_MENU)):
                                upd=Update(opening.update_id+offset,message=message(bot,text,20+index*10+offset));upd.set_bot(bot)
                                await app.process_update(upd)
                                assert not errors,errors
                                assert conv._conversations[key]==expected_state
                            assert preferences.get_preference(fixture.db,UID,'new_words').time=='15:30'
                            assert preferences.get_preference(fixture.db,202,'new_words').time=='14:00'
                            conv._conversations[key]=main.LEARNING_QUIZ
                            app.user_data[UID]['learning_frame']={'flow_id':'new-learning'}
                            sent=len(bot.sent)
                            await app.process_update(opening)
                            assert not errors,errors
                            assert conv._conversations[key]==main.LEARNING_QUIZ
                            assert app.user_data[UID]['learning_frame']=={'flow_id':'new-learning'}
                            assert len(bot.sent)==sent
                        conv._conversations[key]=main.SETTINGS_MENU
                        for offset,text in ((1,t('en','notifications_title')),(2,t('en','notification_new_words'))):
                            upd=Update(3000+offset,message=message(bot,text,100+offset));upd.set_bot(bot)
                            await app.process_update(upd)
                            assert not errors,errors
                            assert conv._conversations[key]==main.NOTIFICATION_SETTINGS_MENU
                        assert '15:30' in bot.sent[-1]
                        assert NoNetworkRequest.attempts==0
                        print('actual SQL preferences + PTB52/53 + permanent/inline + late replay: network=0')
                        await app.shutdown()
                finally:fixture.doCleanups()
            asyncio.run(scenario())
        """)
        result=subprocess.run([sys.executable,'-B','-c',script],cwd=Path(__file__).resolve().parents[1],
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('network=0',result.stdout)


    def test_committed_inline_open_send_failure_replay_enters_settings_without_reclosing_flow(self):
        from pathlib import Path
        raw=next(n.value for n in ast.walk(ast.parse(Path(__file__).with_name('test_new_logic_core_conversation.py').read_text()))
                 if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'class NoNetworkRequest' in n.value)
        prefix=textwrap.dedent(raw).split('class FakeService:')[0].replace('UID = 7','UID = 101')
        node=next(n for n in ast.walk(ast.parse(Path(__file__).read_text())) if isinstance(n,ast.FunctionDef)
                  and n.name=='test_real_main_inline_permanent_time_and_late_replay_use_same_sql_preferences')
        body=next(n.value for n in ast.walk(node) if isinstance(n,ast.Constant) and isinstance(n.value,str)
                  and 'fixture=NotificationTest();fixture.setUp()' in n.value)
        script=prefix+textwrap.dedent(body)
        script=script.replace('(None,main.MENU,main.LEARNING_QUIZ)','(main.LEARNING_QUIZ,)')
        needle='await app.process_update(opening)\n                assert not errors,errors'
        replacement=("from telegram.error import TimedOut\n"
            "                with patch.object(FakeBot,'send_message',side_effect=TimedOut('QA settings screen send failure')):\n"
            "                    await app.process_update(opening)\n"
            "                assert len(errors)==1 and isinstance(errors[0],TimedOut),errors\n"
            "                errors.clear()\n"
            "                await app.process_update(opening)\n"
            "                assert not errors,errors")
        self.assertIn(needle,script,'review injection if dispatch fixture changes')
        result=subprocess.run([sys.executable,'-B','-c',script.replace(needle,replacement,1)],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('network=0',result.stdout)


if __name__ == "__main__":
    unittest.main()
