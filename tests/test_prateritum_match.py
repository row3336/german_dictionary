"""Point29: immutable launch requirements and nondestructive conjugation gating."""
import asyncio
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from tests import test_hilfsverb_match as hf
from tests import test_perfekt_quizzes as pf
from tests import test_new_logic_conjugation as cf
from tests import test_new_logic_conjugation_handlers as ch
from tests import test_new_logic_learning as lf
from bot import storage
from bot.common import match_policy as P
from bot.common.hilfsverb import MatchRules
from bot.common.conjugation import PRAETERITUM_INFINITIVES
from bot.common.verb_construction import InvalidForms
from bot.conjugation import domain as D, repository as R
from bot.handlers import quiz as Q, settings as S, learning as L
from bot.handlers import conjugation as H, conjugation_test as T
from bot.ui import keyboards as K
from bot.common.navigation import _action_key_from_text
from bot.i18n import t
from bot.written import state as W
from bot.app_config import I_DONT_KNOW_TEXT

ON = MatchRules(prateritum=True)
OFF = MatchRules()


class PrateritumPolicyTests(unittest.TestCase):
    setUp = pf.PerfektQuizzes.setUp
    update = pf.PerfektQuizzes.update
    rows = pf.PerfektQuizzes.rows
    select = pf.PerfektQuizzes.select
    launch = hf.HilfsverbMatchTests.launch

    def test_all_four_combinations_and_supplied_wrong_tokens(self):
        e = pf.entry()
        for pr in (False, True):
            for aux in (False, True):
                rules = MatchRules(prateritum=pr, hilfsverb=aux)
                for text, has_pr, has_aux in [('treffen getroffen', False, False),
                        ('treffen traf getroffen', True, False), ('treffen hat getroffen', False, True),
                        ('TREFFEN; TRAF\nHAT GETROFFEN!', True, True)]:
                    self.assertEqual(Q._check_ru_de_strict(e, text, rules)[0],
                                     (has_pr or not pr) and (has_aux or not aux), (rules,text))
                for text in ('treffen triffte hat getroffen', 'treffen traf ist getroffen',
                             'treffen traf nicht hat getroffen', 'treffen hat traf getroffen'):
                    self.assertFalse(Q._check_ru_de_strict(e, text, rules)[0], (rules,text))

    def test_separable_reflexive_forms_order_and_wechsel(self):
        e = pf.entry('sich kennenlernen', 'lernten sich kennen', 'kennengelernt',
                     case='akkusativ', is_separable_override=True)
        e['is_separable'] = 1
        rules = MatchRules(prateritum=True, hilfsverb=True)
        self.assertTrue(Q._check_ru_de_strict(e, 'sich kennenlernen; lernten sich kennen; hat sich kennengelernt', rules)[0])
        for text in ('sich kennenlernen hat sich kennengelernt',
                     'sich kennenlernen lernten kennen hat sich kennengelernt',
                     'sich kennenlernen lernten sich kennen hat kennengelernt'):
            self.assertFalse(Q._check_ru_de_strict(e,text,rules)[0])
        e = pf.entry('fahren','fuhr','gefahren',aux='wechsel')
        for aux in ('hat','ist'):
            self.assertTrue(Q._check_ru_de_strict(e,f'fahren fuhr {aux} gefahren',rules)[0])

    def test_missing_required_pr_diagnostic_not_alias_bypass(self):
        e = pf.entry(pr='', answer_aliases=[pf.entry()])
        self.assertTrue(Q._check_ru_de_strict(e,'treffen getroffen',OFF)[0])
        with self.assertRaises(InvalidForms):
            Q._check_ru_de_strict(e,'treffen traf getroffen',ON)
        with self.assertRaises(InvalidForms):
            P.validate_required(e, ON)

    def test_required_complete_forms_pure_and_off_compatibility(self):
        for field in ('infinitive', 'prateritum', 'partizip2'):
            for missing in (None, '', ' \t ', ', / ;', '...'):
                e = pf.entry(aux=None)
                e[field] = missing
                P.validate_required(e, OFF)  # Legacy OFF contract unchanged.
                for rules in (ON, MatchRules(prateritum=True, hilfsverb=True)):
                    e['hilfsverb'] = 'hat' if rules.hilfsverb else None
                    with self.subTest(field=field, missing=missing, rules=rules):
                        with self.assertRaises(InvalidForms):
                            P.validate_required(e, rules)
        P.validate_required(pf.entry(aux=None), ON)
        P.validate_required(pf.entry(), MatchRules(prateritum=True, hilfsverb=True))

    def test_two_personal_flags_ui_three_languages_and_scoped_reset(self):
        storage.set_setting(self.db, P.PR_KEY, '1')
        self.assertEqual(P.capture(self.db,1), {'prateritum':False,'hilfsverb':False})
        for lang in ('en','ru','uk'):
            storage.set_user_interface_lang(self.db,1,lang)
            for key in (P.PR_KEY,P.KEY):
                storage.set_user_setting(self.db,1,key,'0')
            for key,label in ((P.PR_KEY,'Prateritum_match'),(P.KEY,'Hilfsverb_match')):
                u=self.update(label)
                self.assertEqual(_action_key_from_text(label,1),key)
                asyncio.run(S.fsrs_settings_menu_choice(u,self.ctx))
                texts=[c.args[0] for c in u.message.reply_text.call_args_list]
                self.assertEqual(len(texts[0].splitlines()),2)
                self.assertIn(label+' = ON',texts[0])
            self.assertEqual(P.capture(self.db,1),{'prateritum':True,'hilfsverb':True})
            self.assertEqual(P.capture(self.db,2),{'prateritum':False,'hilfsverb':False})
            self.assertEqual(K._fsrs_settings_menu_keys(1)[-1],['Prateritum_match','Hilfsverb_match',t(lang,'back')])
            self.assertLess(texts[-1].index(t(lang,'prateritum_match_status')),texts[-1].index(t(lang,'hilfsverb_match_status')))
            self.assertLess(texts[-1].index(t(lang,'hilfsverb_match_status')),texts[-1].index(t(lang,'ses_rtr_gap')))
            for key in (P.PR_KEY,P.KEY): storage.set_user_setting(self.db,999,key,'1')
            self.ctx.user_data[W.FRAME]={P.FRAME_KEY:P.capture(self.db,1)}
            with patch.object(S,'_send_algo_settings_menu',new=AsyncMock()):
                asyncio.run(S.algo_settings_menu_choice(self.update(t(lang,'reset_defaults')),self.ctx))
            self.assertEqual(P.capture(self.db,1),{'prateritum':False,'hilfsverb':False})
            self.assertEqual(P.capture(self.db,999),{'prateritum':True,'hilfsverb':True})
            self.assertEqual(self.ctx.user_data[W.FRAME][P.FRAME_KEY],{'prateritum':True,'hilfsverb':True})

    def test_written_explicit_start_snapshot_on_off(self):
        self.launch()
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).prateritum)
        storage.set_user_setting(self.db,1,P.PR_KEY,'1')
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).prateritum)
        self.launch()
        self.assertTrue(P.rules(self.ctx.user_data[W.FRAME]).prateritum)
        storage.set_user_setting(self.db,1,P.PR_KEY,'0')
        self.assertTrue(P.rules(self.ctx.user_data[W.FRAME]).prateritum)
        self.launch()
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).prateritum)


class ConjugationPolicyTests(unittest.TestCase):
    setUp = cf.ConjugationTests.setUp
    connection = cf.ConjugationTests.connection
    delivery = cf.ConjugationTests.delivery
    add = cf.ConjugationTests.add
    block = cf.ConjugationTests.block
    last_pass = cf.ConjugationTests.last_pass

    def flag(self,value,user=7):
        self.conn.execute('INSERT INTO user_settings VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value',
                          (user,P.PR_KEY,'1' if value else '0'))
        self.conn.commit()

    def prep(self):
        return self.service.preparation(7,'ru_de',self.delivery(),'prep',new_session=True)

    def ready_pr(self):
        self.flag(True); self.prep()
        b=self.block(tense='praeteritum')
        return self.service.transition(7,'ru_de','a','praeteritum',b['revision'],b['cycle'],'ready',
            self.delivery(),'prep',today=cf.TODAY-timedelta(days=1),now=cf.NOW,rules=ON)

    def start(self,transport=None):
        return self.service.start_test(7,'ru_de',transport or self.delivery(),'test',today=cf.TODAY,now=cf.NOW)

    def grade(self,view,text=None,transport=None,key=None,rules=None):
        q=view['question']
        return self.service.submit_form(7,'ru_de',q['pass_id'],q['revision'],q['pronoun'],
            cf.FORMS[q['pronoun']] if text is None else text,key or 'key-'+str(self.seq+1),
            transport or self.delivery(),'test',today=cf.TODAY,now=cf.NOW,rules=rules or P.rules(view))

    def saved(self):
        return {table:[dict(r) for r in self.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('conjugation_blocks','conjugation_passes','conjugation_answers','progress')}

    def test_closed_nine_and_valid_six_gate(self):
        self.assertEqual(len(PRAETERITUM_INFINITIVES),9)
        for lemma in sorted(PRAETERITUM_INFINITIVES|{'gehen'}):
            e=dict(type='verb',infinitive=lemma,german=lemma,is_separable=0,conjugation_praeteritum=cf.TABLE)
            self.assertEqual(bool(D.forms_for(e,'praeteritum',OFF)),lemma!='gehen')
            self.assertIsNotNone(D.forms_for(e,'praeteritum',ON))
            for bad in ('ich ging',cf.TABLE+'\nich ging',cf.TABLE.replace('du gehst','ihr gehst')):
                self.assertIsNone(D.forms_for({**e,'conjugation_praeteritum':bad},'praeteritum',ON))

    def test_only_admitted_confirmed_nonseparable_valid_six_materialize(self):
        self.add('a');self.add('b',separable=1);self.add('c',separable=None)
        self.add('d',reps=0);self.add('e');self.add('f',infinitive='sich freuen')
        self.conn.execute("UPDATE words SET conjugation_praeteritum='ich ging',conjugation_praesens='' WHERE id='e'")
        self.conn.commit();self.flag(True)
        self.prep()
        self.assertEqual({r[0] for r in self.conn.execute('SELECT DISTINCT word_id FROM conjugation_blocks')},{'a'})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_blocks WHERE user_id=8').fetchone()[0],0)

    def test_on_off_on_hides_without_erasing_blocks_or_reference_forms(self):
        self.add();self.flag(True);self.prep();before=self.saved()
        self.flag(False);off=self.prep()
        self.assertEqual([b['tense'] for b in off['preparation']['blocks']],['praesens'])
        self.assertEqual(off['preparation']['entry']['conjugation_praeteritum'],'')
        self.assertEqual(self.saved(),before)
        self.flag(True);again=self.prep()
        self.assertEqual({b['tense'] for b in again['preparation']['blocks']},{'praesens','praeteritum'})
        self.assertEqual(self.saved(),before)
        self.assertEqual(self.conn.execute("SELECT conjugation_praeteritum FROM words WHERE id='a'").fetchone()[0],cf.TABLE)

    def test_partial_pass_same_frame_continues_off_new_launch_hides_on_resumes(self):
        self.add();self.ready_pr();view=self.start();pid=view['question']['pass_id']
        view=self.grade(view);self.flag(False)
        view=self.grade(view)  # frozen ON, despite current KV OFF
        before=self.saved();q=view['question'];snapshot=self.last_pass(pid)
        off=self.start()
        self.assertIsNone(off['question'])
        self.assertEqual(self.last_pass(pid),snapshot)
        self.assertEqual(self.saved()['conjugation_blocks'],before['conjugation_blocks'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM conjugation_answers').fetchone()[0],2)
        self.assertNotIn('question',self.service.inspect(7,'ru_de',q,rules=OFF))
        self.assertEqual(self.service.inspect(7,'ru_de',q,rules=ON)['question'],q)
        stale=self.grade(view,rules=OFF)
        self.assertTrue(stale['stale'])
        self.assertEqual(self.last_pass(pid),snapshot)
        self.flag(True);resumed=self.start()
        self.assertEqual(resumed['question'],q)
        self.assertEqual(self.last_pass(pid),snapshot)
        for _ in range(4):resumed=self.grade(resumed)
        self.assertIsNone(resumed['question'])
        self.assertEqual(self.last_pass(pid)['status'],'completed')
        self.assertEqual(self.block(tense='praeteritum')['repetitions'],1)

    def test_start_receipt_replay_and_legacy_snapshot_do_not_read_today_policy(self):
        self.add();self.ready_pr();ident=self.delivery();view=self.start(ident)
        self.flag(False);replay=self.start(ident)
        self.assertTrue(replay['replayed']);self.assertEqual(replay[P.FRAME_KEY],view[P.FRAME_KEY])
        self.assertEqual(replay['question'],view['question'])
        pid=view['question']['pass_id'];prompt=json.loads(self.last_pass(pid)['prompt_snapshot_json'])
        prompt.pop(P.FRAME_KEY)
        self.conn.execute('UPDATE conjugation_passes SET prompt_snapshot_json=? WHERE id=?',(json.dumps(prompt),pid));self.conn.commit()
        self.assertIsNone(self.start()['question'])
        self.flag(True);self.assertEqual(self.start()['question']['pass_id'],pid)

    def test_correction_replay_after_setting_change_keeps_pass_and_no_extra_grade(self):
        self.add();self.ready_pr();view=self.grade(self.start(),'wrong')
        self.flag(False)
        ident=self.delivery()
        args=(7,'ru_de',view['last_answer']['id'],'test',ident)
        fixed=self.service.correct_last(*args,current_question=view['question'],rules=P.rules(view),today=cf.TODAY,now=cf.NOW)
        self.assertTrue(fixed['fixed']);before=self.saved()
        replay=self.service.correct_last(*args,current_question=fixed['question'],rules=OFF,today=cf.TODAY,now=cf.NOW)
        self.assertTrue(replay['replayed']);self.assertEqual(self.saved(),before)
        self.assertEqual(fixed['question']['pronoun'],view['question']['pronoun'])

    def test_failed_start_rolls_back_materialization_and_retry_captures_once(self):
        self.add();self.flag(True);ident=self.delivery();before=self.saved()
        with patch('bot.study.deliveries.commit_delivery',side_effect=RuntimeError('synthetic receipt failure')):
            with self.assertRaises(RuntimeError):self.start(ident)
        self.assertEqual(self.saved(),before)
        self.flag(False);result=self.start(ident)
        self.assertFalse(P.rules(result).prateritum)
        self.assertEqual({r[0] for r in self.conn.execute('SELECT tense FROM conjugation_blocks')},{'praesens'})


class ConjugationHandlerPolicyTests(unittest.TestCase):
    setUp=ch.HandlerTests.setUp
    action=staticmethod(ch.HandlerTests.action)
    back_main=staticmethod(ch.HandlerTests.back_main)
    back_tests=staticmethod(ch.HandlerTests.back_tests)
    update=ch.HandlerTests.update
    call=ch.HandlerTests.call
    frame=ch.HandlerTests.frame
    answers=ch.HandlerTests.answers
    callback=ch.HandlerTests.callback
    answer=ch.HandlerTests.answer

    def flag(self,value):
        self.f.conn.execute('INSERT INTO user_settings VALUES(7,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value',
                            (P.PR_KEY,'1' if value else '0'))
        self.f.conn.commit()

    def pr_callback(self,operation):
        self.frame()['callbacks']={k:v for k,v in self.frame()['callbacks'].items() if v.get('tense')=='praeteritum'}
        return self.callback(operation)

    def test_actual_preparation_next_ready_test_answer_and_reset_keep_frozen_policy(self):
        self.f.add();self.call(H.start_conjugation)
        self.assertFalse(P.rules(self.frame()).prateritum)
        self.flag(True)
        self.call(H.conjugation_input,self.update(t('en','conjugation_next')))
        self.assertFalse(P.rules(self.frame()).prateritum)
        self.assertEqual({b['tense'] for b in self.frame()['blocks']},{'praesens'})
        self.call(H.start_conjugation)
        self.assertTrue(P.rules(self.frame()).prateritum)
        self.flag(False)
        self.call(H.conjugation_card_callback,self.pr_callback('ready'))
        self.assertTrue(P.rules(self.frame()).prateritum)
        self.assertEqual(self.f.block(tense='praeteritum')['phase'],'test')
        # Explicit Test is a new launch. OFF hides the saved Pr block.
        self.call(T.start_conjugation_test)
        self.assertFalse(P.rules(self.frame()).prateritum)
        self.assertIsNone(self.frame()['question'])
        self.flag(True)
        self.f.conn.execute("UPDATE conjugation_blocks SET due=? WHERE tense='praeteritum'",(cf.TODAY.isoformat(),));self.f.conn.commit()
        self.call(T.start_conjugation_test)
        self.flag(False)
        self.answer()
        self.assertTrue(P.rules(self.frame()).prateritum)
        self.assertEqual(self.answers(),1)
        self.call(H.conjugation_card_callback,self.pr_callback('reset'))
        self.assertEqual(self.frame()['kind'],'preparation')
        self.assertTrue(P.rules(self.frame()).prateritum)

    def test_actual_test_launch_off_hides_saved_pass_on_resumes_and_replays(self):
        self.f.add();self.flag(True)
        self.call(H.start_conjugation)
        self.call(H.conjugation_card_callback,self.pr_callback('ready'))
        self.f.conn.execute("UPDATE conjugation_blocks SET due=? WHERE tense='praeteritum'",(cf.TODAY.isoformat(),));self.f.conn.commit()
        self.call(T.start_conjugation_test);self.answer()
        q=copy.deepcopy(self.frame()['question']);pid=q['pass_id'];saved=self.f.last_pass(pid)
        self.flag(False);self.call(T.start_conjugation_test)
        self.assertIsNone(self.frame()['question']);self.assertEqual(self.f.last_pass(pid),saved)
        self.flag(True);u=self.update();self.call(T.start_conjugation_test,u)
        self.assertEqual(self.frame()['question'],q)
        self.flag(False);frame=copy.deepcopy(self.frame());self.call(T.start_conjugation_test,u)
        self.assertEqual(self.frame(),frame)
        self.answer();self.assertEqual(self.answers(),2)

    def test_failed_render_replay_preserves_accepted_start_policy(self):
        self.f.add();self.flag(True);u=self.update();u.message.fail_once=True
        with self.assertRaises(RuntimeError):self.call(H.start_conjugation,u)
        self.flag(False)
        self.call(H.start_conjugation,u)
        self.assertTrue(P.rules(self.frame()).prateritum)
        self.assertEqual({b['tense'] for b in self.frame()['blocks']},{'praesens','praeteritum'})


class WrittenPrPolicyTests(unittest.TestCase):
    setUp=hf.WrittenPolicyTransitions.setUp
    transaction=hf.WrittenPolicyTransitions.transaction
    word=hf.WrittenPolicyTransitions.word
    pick=hf.WrittenPolicyTransitions.pick
    update=hf.WrittenPolicyTransitions.update
    run_async=hf.WrittenPolicyTransitions.run_async
    start=hf.WrittenPolicyTransitions.start
    answer=hf.WrittenPolicyTransitions.answer
    rows=hf.WrittenPolicyTransitions.rows

    def test_actual_checker_on_rejects_short_and_keeps_policy_after_correction(self):
        self.ctx.user_data[W.FRAME][P.FRAME_KEY]['prateritum']=True
        self.answer('treffen getroffen')
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'],0)
        self.answer('fix')
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'],1)
        self.assertTrue(P.rules(self.ctx.user_data[W.FRAME]).prateritum)
        self.assertEqual(len(self.ctx.user_data['session_seen_ru_de']),1)

    def test_missing_pr_idk_has_no_grade_or_count(self):
        self.ctx.user_data[W.FRAME][P.FRAME_KEY]['prateritum']=True
        self.ctx.user_data['quiz_entry']['prateritum']=''
        before={table:self.rows(table) for table in ('progress','quiz_events','study_actions')}
        self.answer(I_DONT_KNOW_TEXT)
        for table,rows in before.items():self.assertEqual(self.rows(table),rows)
        self.assertNotIn(W.FRAME,self.ctx.user_data)

    def check_missing_p2(self, p2, answer):
        entry = self.ctx.user_data['quiz_entry']
        entry.update(infinitive='lesen', german='lesen', prateritum='las',
                     partizip2=p2, hilfsverb=None)
        self.ctx.user_data[W.FRAME][P.FRAME_KEY] = {'prateritum': True, 'hilfsverb': False}
        before = {table: self.rows(table) for table in ('progress', 'quiz_events', 'study_actions')}
        self.answer(answer)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        self.assertNotIn(W.FRAME, self.ctx.user_data)
        self.assertFalse(self.ctx.user_data.get('session_seen_ru_de'))

    def test_empty_p2_normal_is_diagnostic(self):
        self.check_missing_p2('', 'lesen las gelesen')

    def test_empty_p2_idk_is_diagnostic(self):
        self.check_missing_p2('', I_DONT_KNOW_TEXT)

    def test_whitespace_p2_normal_is_diagnostic(self):
        self.check_missing_p2(' \t ', 'lesen las gelesen')

    def test_whitespace_p2_idk_is_diagnostic(self):
        self.check_missing_p2(' \t ', I_DONT_KNOW_TEXT)

    def test_off_drill_and_replay_do_not_read_settings(self):
        self.ctx.user_data['quiz_is_retry']=True
        u=self.update('treffen getroffen');self.answer('',update=u)
        frame=copy.deepcopy(self.ctx.user_data[W.FRAME]);self.answer('',update=u)
        self.assertEqual(self.ctx.user_data[W.FRAME],frame)
        self.assertFalse(P.rules(frame).prateritum)
        self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(len(self.ctx.user_data['session_seen_ru_de']),0)


class LearningPrPolicyTests(unittest.TestCase):
    setUp=lf.LearningTests.setUp
    update=lf.LearningTests.update
    rows=lf.LearningTests.rows
    add=lf.LearningTests.add

    def make_verb(self,pr='traf'):
        from bot.study import repository
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET type='verb',german='treffen',infinitive='treffen',"
                         "prateritum=?,partizip2='getroffen',reflexive_case='none' WHERE id='a'",(pr,))
        self.add(ready=True)

    def test_start_replay_and_actual_checker_use_captured_pr(self):
        self.make_verb();storage.set_user_setting(self.db,1,P.PR_KEY,'1')
        u=self.update('learning_start');asyncio.run(L.learning_menu_choice(u,self.ctx))
        storage.set_user_setting(self.db,1,P.PR_KEY,'0')
        asyncio.run(L.learning_menu_choice(u,self.ctx))
        self.assertTrue(P.rules(self.ctx.user_data[L.FRAME]).prateritum)
        with patch.object(L,'_check_ru_de_strict',side_effect=Q._check_ru_de_strict):
            asyncio.run(L.learning_quiz(self.update('treffen getroffen'),self.ctx))
        actions=self.rows('study_actions')
        self.assertEqual(len(actions),1)
        self.assertEqual(json.loads(actions[0]['receipt_json'])['answer_kind'],'wrong')
        asyncio.run(L.learning_menu_choice(self.update('learning_start'),self.ctx))
        self.assertFalse(P.rules(self.ctx.user_data.get(L.FRAME)).prateritum)

    def test_missing_required_pr_idk_preserves_queue_and_progress(self):
        self.make_verb('');storage.set_user_setting(self.db,1,P.PR_KEY,'1')
        asyncio.run(L.learning_menu_choice(self.update('learning_start'),self.ctx))
        before={table:self.rows(table) for table in ('progress','quiz_events','study_actions','learning_items')}
        asyncio.run(L.learning_quiz(self.update(I_DONT_KNOW_TEXT),self.ctx))
        for table,rows in before.items():self.assertEqual(self.rows(table),rows,table)
        self.assertNotIn(L.FRAME,self.ctx.user_data)

    def check_missing_p2(self, p2, answer):
        from bot.study import repository
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET type='verb',german='lesen',infinitive='lesen',"
                         "prateritum='las',partizip2=?,hilfsverb=NULL,reflexive_case='none' WHERE id='a'", (p2,))
        self.add(ready=True)
        storage.set_user_setting(self.db, 1, P.PR_KEY, '1')
        asyncio.run(L.learning_menu_choice(self.update('learning_start'), self.ctx))
        before = {table: self.rows(table) for table in ('progress', 'quiz_events', 'study_actions', 'learning_items')}
        with patch.object(L, '_check_ru_de_strict', side_effect=Q._check_ru_de_strict):
            asyncio.run(L.learning_quiz(self.update(answer), self.ctx))
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        self.assertNotIn(L.FRAME, self.ctx.user_data)

    def test_empty_p2_normal_is_diagnostic(self):
        self.check_missing_p2('', 'lesen las gelesen')

    def test_empty_p2_idk_is_diagnostic(self):
        self.check_missing_p2('', I_DONT_KNOW_TEXT)

    def test_whitespace_p2_normal_is_diagnostic(self):
        self.check_missing_p2(' \t ', 'lesen las gelesen')

    def test_whitespace_p2_idk_is_diagnostic(self):
        self.check_missing_p2(' \t ', I_DONT_KNOW_TEXT)
