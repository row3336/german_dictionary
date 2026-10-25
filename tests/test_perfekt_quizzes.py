"""Linked Perfekt checking and real reservation/answer transaction guards."""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from tests._telegram_stub import install
install()
from bot import storage, learning_store
from bot.app_config import QUIZ_RU_DE, I_DONT_KNOW_TEXT
from bot.common import session
from bot.common.perfekt import scalar_projection
from bot.common.verb_construction import InvalidForms
from bot.handlers import quiz, learning, clarify_fields
from bot.oral import domain as D, view as V, repository as oral_repo
from bot.oral.service import OralService
from bot.study import repository as R, generations as G, answers
from bot.ui import formatting as F
from bot.written import state as W, service as S

STAND=Path('/Users/wf/py/_agent_work/888_german_dictionary/data18_7_20261025/core')
NOW=datetime(2026,10,25,12,tzinfo=timezone.utc)

def variant(lemma,pr,p2,aux='haben',case='none',plural=False,**extra):
    finite=('haben' if plural else 'hat') if aux=='haben' else ('sind' if plural else 'ist')
    return dict(variant_id=lemma+pr+aux,sense_id=lemma+pr,sense_ru='значение',
        lemma_for_answer=lemma,reflexive_case=case,auxiliary=aux,finite_auxiliary=finite,
        person_scope='third_plural_reciprocal_reference' if plural else 'third_singular_reference',
        prateritum=pr,partizip2=p2,perfekt_fragment=finite+(' sich ' if case!='none' else ' ')+p2,
        examples=[{'text_de':'Natürliches Beispiel. Ich habe es gesehen.'}],
        source_pointers=['synthetic:accepted'],**extra)

def entry(lemma='treffen',pr='traf',p2='getroffen',wid=None,variants=None,**extra):
    vs=variants or [variant(lemma,pr,p2)]
    return dict(id=wid,type='verb',german=lemma,infinitive=lemma,reflexive_case=vs[0]['reflexive_case'],
        russian='встречать',ukrainian='зустрічати',english='meet',is_separable=0,topic='1',cefr_level='A1',
        perfekt_variants_json=json.dumps(dict(version=1,status='verified',variants=vs)),
        **scalar_projection(vs),**extra)

class PerfektQuizzes(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True,exist_ok=True)
        self.db=str(Path(tempfile.mkdtemp(prefix='perfekt-',dir=STAND))/'synthetic.sqlite')
        self.enterContext(patch.dict(os.environ,DB_PATH=self.db,OWNER_ID='999'))
        storage.init_db(self.db)
        with R.transaction(self.db) as c:
            c.execute("INSERT INTO topics(name,level) VALUES('1','A1')")
            c.execute("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES(1,'approved','en','ru')")
        self.ctx=NS(user_data={},bot=NS(set_message_reaction=AsyncMock()))
        self.seq=100
    def update(self,text=''):
        self.seq+=1
        return NS(update_id=self.seq,effective_user=NS(id=1),effective_chat=NS(id=1),
                  message=NS(text=text,message_id=self.seq,reply_text=AsyncMock()))
    def rows(self,table):
        with R.transaction(self.db) as c:
            return [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
    def select(self,wid='a'):
        with R.transaction(self.db) as c:
            return G.selected_entry(c,1,storage._row_to_entry(c.execute('SELECT * FROM words WHERE id=?',(wid,)).fetchone()))
    def aliases(self):
        storage.add_word(self.db,entry(wid='a'))
        storage.add_word(self.db,entry('begegnen','begegnete','begegnet',wid='b'))
        self.assertTrue(storage.add_synonym_link(self.db,'a','b')[0])
    def reserve(self):
        with R.transaction(self.db) as c:
            R.append_event(c,1,'a','de_ru',False,NOW)
            R.set_progress(c,1,'a','ru_de',dict(interval=5,repetitions=2,ease=2.5,due='2000-01-01'))
        with patch.object(session,'_mode_l1_de',return_value='ru_de'),patch.object(session,'_mode_de_l1',return_value='de_ru'),patch.object(session,'_mode_pl_sg',return_value='pl_sg'):
            e=session._pick_due_entry(self.ctx,1,'ru_de')
        self.assertEqual(e['id'],'a')
        W.establish_view(self.ctx,'ru_de',QUIZ_RU_DE,e)
        return e
    def test_required_aux_short_full_and_legacy(self):
        e=entry()
        for text in ['treffen hat getroffen','treffen, traf. hat getroffen','TREFFEN HAT GETROFFEN']:
            self.assertTrue(quiz._check_ru_de_strict(e,text)[0],text)
        for text in ['treffen getroffen','treffen ist getroffen','treffen traf getroffen']:
            self.assertFalse(quiz._check_ru_de_strict(e,text)[0],text)
        e.pop('perfekt_variants_json')
        self.assertTrue(quiz._check_ru_de_strict(e,'treffen getroffen')[0])
        e['perfekt_variants_json']=None
        self.assertTrue(quiz._check_ru_de_strict(e,'treffen getroffen')[0])
    def test_linked_hangen_and_motion(self):
        e=entry('hängen',variants=[variant('hängen','hing','gehangen','sein'),variant('hängen','hängte','gehängt')])
        for text in ['hängen ist gehangen','hängen hat gehängt','hängen hing ist gehangen','hängen hängte hat gehängt']:
            self.assertTrue(quiz._check_ru_de_strict(e,text)[0],text)
        for text in ['hängen hing hat gehängt','hängen hängte ist gehangen','hängen hat gehangen','hängen ist gehängt']:
            self.assertFalse(quiz._check_ru_de_strict(e,text)[0],text)
        e=entry('fahren',variants=[variant('fahren','fuhr','gefahren','sein'),variant('fahren','fuhr','gefahren')])
        for text in ['fahren ist gefahren','fahren hat gefahren']:
            self.assertTrue(quiz._check_ru_de_strict(e,text)[0])
    def test_reflexive_and_reciprocal_plural(self):
        for case in ['akkusativ','dativ']:
            e=entry('sich waschen',variants=[variant('sich waschen','wusch sich','gewaschen',case=case)])
            for text in ['sich waschen hat sich gewaschen','sich waschen wusch sich hat sich gewaschen']:
                self.assertTrue(quiz._check_ru_de_strict(e,text)[0])
            for text in ['waschen hat sich gewaschen','sich waschen hat gewaschen','sich waschen hat mir gewaschen','sich waschen wusch hat sich gewaschen']:
                self.assertFalse(quiz._check_ru_de_strict(e,text)[0],text)
        for lemma,pr,p2,aux,finite in [('sich kennenlernen','lernten sich kennen','kennengelernt','haben','haben'),('sich begegnen','begegneten sich','begegnet','sein','sind')]:
            e=entry(lemma,variants=[variant(lemma,pr,p2,aux,'akkusativ',True)])
            self.assertTrue(quiz._check_ru_de_strict(e,lemma+' '+finite+' sich '+p2)[0])
            self.assertFalse(quiz._check_ru_de_strict(e,lemma+' hat sich '+p2)[0])
    def test_special_forms(self):
        for lemma,pr,p2,aux,kind in [('geboren werden','wurde geboren','geboren worden','sein','passive_perfekt_complement'),('lassen','ließ','machen lassen','haben','ersatzinfinitiv_construction_complement'),('möchten','möchtete','gemöchtet','haben','participle')]:
            v=variant(lemma,pr,p2,aux,partizip2_kind=kind)
            e=entry(lemma,variants=[v])
            self.assertTrue(quiz._check_ru_de_strict(e,lemma+' '+pr+' '+v['perfekt_fragment'])[0])
            self.assertFalse(quiz._check_ru_de_strict(e,lemma+' '+p2)[0])
    def test_revealed_paths_and_mask(self):
        e=entry('hängen',variants=[variant('hängen','hing','gehangen','sein'),variant('hängen','hängte','gehängt')])
        for text in [F._format_word_card(e),F._format_de_ru_question(e),F._format_feedback_ru_de(e,True),F._format_feedback_ru_de(e,False),F._format_learning_preview_prompt(e,'ru'),V.answer(e,'phrase_ru_de')]:
            self.assertIn('hing, ist gehangen',text)
            self.assertIn('hängte, hat gehängt',text)
            self.assertIn('Natürliches Beispiel. Ich habe es gesehen.',text)
            self.assertNotIn('hing, hat gehängt',text)
        e['example']='Er ist gehangen.'
        for text in [F._format_ru_question(e,'ru'),V.question(e,'phrase_ru_de')]:
            self.assertNotIn('gehangen',text)
            self.assertNotIn('Natürliches',text)
    def test_legacy_diagnostic_card_remains_readable(self):
        e=entry('sich waschen','wusch sich','gewaschen')
        e['reflexive_case']=None
        e['perfekt_variants_json']=None
        self.assertIn('sich waschen',F._format_word_card(e))
        self.assertIn('gewaschen',F._format_feedback_ru_de(e,False))
    def test_refresh_keeps_learning_runtime_and_independent_nested_copies(self):
        runtime=dict(learning_step=3,learning_status='active',learning_recovery_remaining=2,
            learning_batch_id=7,learning_due='2026-10-25',learning_first_due_pending=0,
            srs={'interval':5,'repetitions':2},study_generation=4,
            study_generations={'a':4,'alias':2},learning_lifecycle=3,
            learning_lifecycles={'a':3,'alias':1},answer_aliases=[{'id':'alias','german':'other'}])
        for kind in ['verb','nomen']:
            selected={**entry(wid='a'),**runtime,'type':kind}
            fresh={**entry(wid='a'),'type':kind,'russian':'новый перевод','example':'Personal hint.'}
            expected=copy.deepcopy(selected)
            result=G.preserve_selected_entry(fresh,selected)
            for key in runtime:
                self.assertEqual(result[key],expected[key],key)
            self.assertEqual(result['russian'],'новый перевод')
            self.assertEqual(result['example'],'Personal hint.')
            result['srs']['interval']=99
            result['answer_aliases'][0]['german']='changed'
            result['study_generations']['alias']=99
            result['learning_lifecycles']['alias']=99
            self.assertEqual(selected,expected)
            self.assertEqual(fresh['example'],'Personal hint.')
    def test_exact_example_duplicates_keep_first_choice_and_variant_examples(self):
        first='Die Jacke hängt im Schrank.'
        specific_a='Die Jacke hing gestern dort.'
        specific_b='Sie hat die Jacke an den Haken gehängt.'
        a=variant('hängen','hing','gehangen','sein')
        b=variant('hängen','hängte','gehängt')
        a['examples']=[{'text_de':first},{'text_de':specific_a}]
        b['examples']=[{'text_de':specific_b}]
        e=entry('hängen',variants=[a,b]);e['example']=first+'\nUnselected legacy second example.'
        renderings={'de_ru':F._format_de_ru_question(e),'card':F._format_word_card(e),
            'preview':F._format_learning_preview_prompt(e,'ru')}
        for name,text in renderings.items():
            self.assertEqual(text.count(first),1,name)
            self.assertEqual(text.count(specific_a),1,name)
            self.assertEqual(text.count(specific_b),1,name)
            self.assertIn('hing, ist gehangen',text)
            self.assertIn('hängte, hat gehängt',text)
            self.assertEqual('Unselected legacy second example.' in text,name=='card')
        for text in [F._format_feedback_ru_de(e,True),V.answer(e,'phrase_ru_de')]:
            self.assertEqual(text.count(first),1)
            self.assertIn(specific_a,text);self.assertIn(specific_b,text)
        for lang in ['ru','uk','en']:
            self.assertNotIn(first,F._format_ru_question(e,lang))
    def test_personal_first_hint_and_exact_case_boundary(self):
        original='Meine Großmutter stammt aus Polen.'
        personal='MEINE Großmutter stammt aus Polen.'
        v=variant('stammen','stammte','gestammt')
        v['examples']=[{'text_de':original}]
        e=entry('stammen',variants=[v]);e['example']=personal+'\nPersonal second hint.'
        for text in [F._format_de_ru_question(e),F._format_learning_preview_prompt(e,'ru')]:
            self.assertEqual(text.count(personal),1)
            self.assertEqual(text.count(original),1)
            self.assertNotIn('Personal second hint.',text)
        card=F._format_word_card(e)
        self.assertEqual(card.count(personal),1)
        self.assertEqual(card.count(original),1)
        self.assertIn('Personal second hint.',card)
    def test_invalid_annotation_never_fallback(self):
        for raw in ['{bad','','{}','{"version":1,"version":1}']:
            e=entry(); e['perfekt_variants_json']=raw
            with self.assertRaises(InvalidForms): quiz._check_ru_de_strict(e,'treffen getroffen')
            with self.assertRaises(InvalidForms): F._format_feedback_ru_de(e,False)
    def test_frozen_alias_and_legacy_bypass(self):
        self.aliases()
        legacy=entry('sehen','sah','gesehen',wid='c');legacy['perfekt_variants_json']=None
        storage.add_word(self.db,legacy)
        self.assertTrue(storage.add_synonym_link(self.db,'a','c')[0])
        e=self.reserve()
        self.assertEqual(set(e['study_generations']),{'a','b','c'})
        self.assertEqual(set(e['learning_lifecycles']),{'a','b','c'})
        self.assertTrue(quiz._check_ru_de_strict(e,'begegnen hat begegnet')[0])
        self.assertFalse(quiz._check_ru_de_strict(e,'sehen gesehen')[0])
        frozen=json.loads(json.dumps(e)); e['answer_aliases'][0]['partizip2']='changed'
        with R.transaction(self.db) as c:c.execute("UPDATE words SET partizip2='future',perfekt_variants_json=NULL WHERE id='b'")
        with patch.object(quiz,'get_synonym_group',side_effect=AssertionError('future lookup')):
            self.assertTrue(quiz._check_ru_de_strict(frozen,'begegnen hat begegnet')[0])
            old={**frozen,'perfekt_variants_json':None};old.pop('answer_aliases')
            self.assertTrue(quiz._check_ru_de_strict(old,'treffen getroffen')[0])
            self.assertFalse(quiz._check_ru_de_strict(old,'begegnen future')[0])
    def test_alias_generation_and_lifecycle_guard_actual_reservation(self):
        self.aliases()
        for changed in ['generation','lifecycle']:
            self.ctx.user_data.clear();e=self.reserve()
            with R.transaction(self.db) as c:
                if changed=='generation':G.advance_generation(c,1,'b',e['study_generations']['b'])
                else:c.execute("INSERT INTO learning_requests(user_id,word_id,status,request_id,queued_at,learning_lifecycle) VALUES(1,'b','consumed','synthetic','2000-01-01',1)")
            before=self.rows('progress')
            with self.assertRaises(G.StaleGeneration):
                S.original(self.db,self.update(),self.ctx,'ru_de',QUIZ_RU_DE,e,'correct',1.65)
            self.assertEqual(self.rows('progress'),before)
            self.assertEqual([r for r in self.rows('quiz_events') if r['mode']=='ru_de'],[])
            self.assertEqual(self.rows('study_actions'),[])
    def test_refresh_preserves_contract(self):
        storage.add_word(self.db,entry(wid='a'));e=self.select()
        fresh=entry('sehen','sah','gesehen',wid='a');fresh['russian']='новый перевод'
        refreshed=clarify_fields._preserve_selection(fresh,e)
        self.assertEqual(refreshed['russian'],'новый перевод')
        self.assertTrue(quiz._check_ru_de_strict(refreshed,'treffen hat getroffen')[0])
        old={k:v for k,v in e.items() if k not in ['perfekt_variants_json','answer_aliases']}
        refreshed=clarify_fields._preserve_selection(fresh,old)
        self.assertIsNone(refreshed['perfekt_variants_json'])
        self.assertTrue(quiz._check_ru_de_strict(refreshed,'treffen getroffen')[0])
        frame=D.new_frame('de_ru');frame['hidden']=D.slot(e)
        with R.transaction(self.db) as c:c.execute("UPDATE words SET perfekt_variants_json=NULL,russian='новый перевод' WHERE id='a'")
        with patch.object(oral_repo,'selected_entry',side_effect=AssertionError('refresh reservation')):
            refreshed=OralService(self.db).refresh(1,frame,'ru')['hidden']['entry']
        self.assertEqual(refreshed['perfekt_variants_json'],e['perfekt_variants_json'])
        self.assertEqual(refreshed['russian'],'новый перевод')
    def test_written_malformed_answer_and_idk_no_effect(self):
        storage.add_word(self.db,entry(wid='a'))
        for text in ['treffen hat getroffen',I_DONT_KNOW_TEXT]:
            e={**self.select(),'perfekt_variants_json':'{bad'};self.ctx.user_data.clear()
            W.establish_view(self.ctx,'ru_de',QUIZ_RU_DE,e)
            with patch.object(quiz,'_user_interface_lang',return_value='en'),patch.object(quiz,'_action_key_from_text',return_value=None),patch.object(quiz,'_mode_l1_de',return_value='ru_de'),patch.object(quiz,'_back_to_main_menu',new=AsyncMock(return_value=0)):
                asyncio.run(quiz.quiz_ru_de(self.update(text),self.ctx))
            self.assertEqual(self.rows('quiz_events'),[])
            self.assertEqual(self.rows('progress'),[])
    def test_learning_malformed_answer_and_idk_no_effect(self):
        storage.add_word(self.db,entry(wid='a'))
        with R.transaction(self.db) as c:
            b=learning_store.get_or_create_batch(c,1,20,NOW)
            learning_store.add_items(c,1,['a'],b,NOW)
            learning_store.preview_transition(c,1,'a','preview_next',NOW,0)
            c.execute("UPDATE learning_items SET next_due='2000-01-01T00:00:00Z'")
        for text in ['treffen hat getroffen',I_DONT_KNOW_TEXT]:
            self.ctx.user_data.clear()
            with R.transaction(self.db) as c:plan=learning._next_learning_view(c,self.ctx,1)
            plan['entry']['perfekt_variants_json']='{bad'
            learning._establish_learning_frame(self.ctx,1,plan=plan)
            self.ctx.user_data[learning.FRAME]['pending_render']=False
            before=copy.deepcopy(self.rows('learning_items'))
            with patch.object(learning,'_user_interface_lang',return_value='en'),patch.object(learning,'_action_key_from_text',return_value=None),patch.object(learning,'_send_learning_menu',new=AsyncMock(return_value=0)):
                asyncio.run(learning.learning_quiz(self.update(text),self.ctx))
            self.assertEqual(self.rows('learning_items'),before)
            self.assertEqual(self.rows('quiz_events'),[])
            self.assertEqual(self.rows('progress'),[])
    def original_wrong(self):
        self.ctx.user_data.clear()
        e=self.reserve()
        self.assertFalse(quiz._check_ru_de_strict(e,'treffen getroffen')[0])
        self.assertTrue(quiz._accept_written_answer(self.update('treffen getroffen'),self.ctx,
            e,'ru_de',False,False,QUIZ_RU_DE))
        return e,self.ctx.user_data[W.FRAME]['last_receipt']
    def test_annotated_original_input_err_undo_retains_alias_progress(self):
        self.aliases()
        with R.transaction(self.db) as c:
            R.set_progress(c,1,'b','ru_de',dict(interval=30,repetitions=7,ease=2.5,due='2099-01-01'))
        alias_before=[r for r in self.rows('progress') if r['word_id']=='b']
        e,original=self.original_wrong()
        self.assertEqual(original['before']['generations'],{'a':e['study_generation']})
        self.assertEqual(self.ctx.user_data[W.FRAME]['selected_entry']['study_generations'],e['study_generations'])
        self.assertEqual(len([r for r in self.rows('quiz_events') if r['mode']=='ru_de']),1)
        self.assertTrue(asyncio.run(quiz._apply_input_err(self.update(),self.ctx,'ru_de')))
        event=next(r for r in self.rows('quiz_events') if r['mode']=='ru_de')
        self.assertEqual(event['is_correct'],1)
        outcome=answers.undo_steps(self.db,1,2,self.update().update_id)
        self.assertEqual(outcome['restored'],2)
        self.assertEqual([r for r in self.rows('progress') if r['word_id']=='b'],alias_before)
        self.assertEqual([r for r in self.rows('quiz_events') if r['mode']=='ru_de'],[])
    def test_target_generation_stale_correction_and_undo_remain_guarded(self):
        self.aliases()
        e,original=self.original_wrong()
        with R.transaction(self.db) as c:
            G.advance_generation(c,1,'a',e['study_generation'])
        before=(self.rows('progress'),self.rows('quiz_events'))
        with self.assertRaises(G.StaleGeneration):
            S.correction(self.db,self.update(),self.ctx,'ru_de',1.65)
        self.assertEqual((self.rows('progress'),self.rows('quiz_events')),before)
        outcome=answers.undo_steps(self.db,1,1,self.update().update_id)
        self.assertEqual(outcome['restored'],0)
        self.assertEqual((self.rows('progress'),self.rows('quiz_events')),before)
    def test_alias_change_does_not_remove_target_undo_right(self):
        self.aliases()
        e,original=self.original_wrong()
        with R.transaction(self.db) as c:
            G.advance_generation(c,1,'b',e['study_generations']['b'])
        alias_before=[r for r in self.rows('progress') if r['word_id']=='b']
        outcome=answers.undo_steps(self.db,1,1,self.update().update_id)
        self.assertEqual(outcome['restored'],1)
        self.assertEqual([r for r in self.rows('quiz_events') if r['mode']=='ru_de'],[])
        self.assertEqual([r for r in self.rows('progress') if r['word_id']=='b'],alias_before)
    def test_oral_diagnostic_rollback(self):
        storage.add_word(self.db,entry(wid='a'));e=self.select();e['perfekt_variants_json']='{bad'
        frame=D.new_frame('de_ru');frame['pending']=D.slot(e);frame['phase']='grading';frame['revision']=1
        with self.assertRaises(InvalidForms):
            OralService(self.db).transition(1,frame,'correct',500,dict(gap=1,limit=3,dict_lang='ru'),today=NOW.date(),now=NOW)
        self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(self.rows('progress'),[])

if __name__=='__main__':unittest.main()
