"""Scalar hilfsverb checking and real reservation/answer transaction guards."""
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
from bot.common.hilfsverb import MatchRules, HilfsverbValidationError
from bot.common.verb_construction import InvalidForms
from bot.handlers import quiz, learning, clarify_fields
from bot.oral import domain as D, view as V, repository as oral_repo
from bot.oral.service import OralService
from bot.study import repository as R, generations as G, answers
from bot.ui import formatting as F
from bot.written import state as W, service as S

STAND=Path('/Users/wf/py/_agent_work/888_german_dictionary/data18_7_20261025/core')
NOW=datetime(2026,10,25,12,tzinfo=timezone.utc)

def entry(lemma='treffen',pr='traf',p2='getroffen',wid=None,aux='hat',case='none',**extra):
    """Schema22 card: scalar forms plus words.hilfsverb (hat/ist/wechsel/None)."""
    return dict(id=wid,type='verb',german=lemma,infinitive=lemma,reflexive_case=case,
        russian='встречать',ukrainian='зустрічати',english='meet',is_separable=0,topic='1',cefr_level='A1',
        prateritum=pr,partizip2=p2,hilfsverb=aux,**extra)

# Retired schema21 variant-only examples inside an old frozen frame.
OLD_FRAME_JSON=json.dumps(dict(version=1,status='verified',variants=[dict(
    examples=[dict(text_de='Nur im alten Variantenblock.')])]))

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
        for text in ['treffen ist getroffen','treffen hat geworfen','treffen traf ist getroffen']:
            self.assertFalse(quiz._check_ru_de_strict(e,text)[0],text)
        # Rules OFF: the auxiliary is optional but a supplied one must be right.
        self.assertTrue(quiz._check_ru_de_strict(e,'treffen getroffen')[0])
        on=MatchRules(hilfsverb=True)
        self.assertFalse(quiz._check_ru_de_strict(e,'treffen getroffen',on)[0])
        self.assertFalse(quiz._check_ru_de_strict(e,'treffen traf getroffen',on)[0])
        self.assertTrue(quiz._check_ru_de_strict(e,'treffen hat getroffen',on)[0])
        # NULL is unknown, never read as 'hat'.
        for unknown in (None,):
            e['hilfsverb']=unknown
            self.assertTrue(quiz._check_ru_de_strict(e,'treffen getroffen')[0])
            self.assertFalse(quiz._check_ru_de_strict(e,'treffen hat getroffen')[0])
            e.pop('hilfsverb')
            self.assertTrue(quiz._check_ru_de_strict(e,'treffen getroffen')[0])
    def test_missing_required_metadata_is_diagnostic_not_wrong_answer(self):
        self.assertTrue(issubclass(HilfsverbValidationError,InvalidForms))
        unknown=entry(aux=None)
        with self.assertRaises(InvalidForms):
            quiz._check_ru_de_strict(unknown,'treffen getroffen',MatchRules(hilfsverb=True))
        no_pr=entry(pr='')
        with self.assertRaises(InvalidForms):
            quiz._check_ru_de_strict(no_pr,'treffen hat getroffen',MatchRules(prateritum=True))
        # The same cards stay answerable when the requirement is off.
        self.assertTrue(quiz._check_ru_de_strict(unknown,'treffen getroffen')[0])
        self.assertTrue(quiz._check_ru_de_strict(no_pr,'treffen hat getroffen')[0])
    def test_written_handler_missing_metadata_invalidates_without_effect(self):
        # The wrapper that catches InvalidForms is the existing diagnostic path.
        storage.add_word(self.db,entry(wid='a'))
        e={**self.select(),'hilfsverb':'haben'};self.ctx.user_data.clear()
        W.establish_view(self.ctx,'ru_de',QUIZ_RU_DE,e)
        with patch.object(quiz,'_user_interface_lang',return_value='en'),patch.object(quiz,'_action_key_from_text',return_value=None),patch.object(quiz,'_mode_l1_de',return_value='ru_de'),patch.object(quiz,'_back_to_main_menu',new=AsyncMock(return_value=0)):
            asyncio.run(quiz.quiz_ru_de(self.update('treffen hat getroffen'),self.ctx))
        self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(self.rows('progress'),[])
    def test_coherent_positional_pairs_and_wechsel(self):
        e=entry('hängen','hängte, hing','gehängt, gehangen')
        for text in ['hängen hat gehängt','hängen hängte hat gehängt','hängen hing hat gehangen','hängen hat gehangen']:
            self.assertTrue(quiz._check_ru_de_strict(e,text)[0],text)
        for text in ['hängen hing hat gehängt','hängen hängte hat gehangen','hängen ist gehangen','hängen hat gegangen']:
            self.assertFalse(quiz._check_ru_de_strict(e,text)[0],text)
        # One Partizip II, both auxiliaries: wechsel accepts hat and ist.
        e=entry('fahren','fuhr','gefahren',aux='wechsel')
        for text in ['fahren ist gefahren','fahren hat gefahren','fahren fuhr ist gefahren','fahren fuhr hat gefahren']:
            self.assertTrue(quiz._check_ru_de_strict(e,text)[0],text)
        self.assertFalse(quiz._check_ru_de_strict(e,'fahren war gefahren')[0])
    def test_mixed_aux_with_different_p2_is_rejected_not_valid_card(self):
        # hing/gehangen with ist and hängte/gehängt with hat are separate cards.
        e=entry('hängen','hing, hängte','gehangen, gehängt',aux='wechsel')
        with self.assertRaises(InvalidForms):
            quiz._check_ru_de_strict(e,'hängen ist gehangen')
        with self.assertRaises(InvalidForms):
            F._format_feedback_ru_de(e,False)
    def test_ambiguous_pair_counts_are_rejected_for_known_aux(self):
        e=entry('hängen','a, b','x, y, z')
        with self.assertRaises(InvalidForms):
            quiz._check_ru_de_strict(e,'hängen hat x')
    def test_reflexive_and_reciprocal_plural(self):
        for case in ['akkusativ','dativ']:
            e=entry('sich waschen','wusch sich','gewaschen',case=case)
            for text in ['sich waschen hat sich gewaschen','sich waschen wusch sich hat sich gewaschen']:
                self.assertTrue(quiz._check_ru_de_strict(e,text)[0])
            for text in ['waschen hat sich gewaschen','sich waschen hat gewaschen','sich waschen hat mir gewaschen','sich waschen wusch hat sich gewaschen']:
                self.assertFalse(quiz._check_ru_de_strict(e,text)[0],text)
    def test_seven_mutual_verbs_six_hat_one_ist(self):
        mutual=[('sich kennenlernen','lernten sich kennen','kennengelernt','hat'),('sich lieben','liebten sich','geliebt','hat'),
                ('sich sehen','sahen sich','gesehen','hat'),('sich begegnen','begegneten sich','begegnet','ist'),
                ('sich duzen','duzten sich','geduzt','hat'),('sich küssen','küssten sich','geküsst','hat'),
                ('sich siezen','siezten sich','gesiezt','hat')]
        self.assertEqual([m[3] for m in mutual].count('hat'),6)
        for lemma,pr,p2,aux in mutual:
            e=entry(lemma,pr,p2,aux=aux,case='dativ' if aux=='ist' else 'akkusativ')
            other='hat' if aux=='ist' else 'ist'
            self.assertTrue(quiz._check_ru_de_strict(e,lemma+' '+aux+' sich '+p2)[0],lemma)
            self.assertTrue(quiz._check_ru_de_strict(e,lemma+' '+pr+' '+aux+' sich '+p2)[0],lemma)
            self.assertFalse(quiz._check_ru_de_strict(e,lemma+' '+other+' sich '+p2)[0],lemma)
            self.assertFalse(quiz._check_ru_de_strict(e,lemma+' haben sich '+p2)[0],lemma)
    def test_special_forms(self):
        on=MatchRules(hilfsverb=True)
        for lemma,pr,p2,aux in [('geboren werden','wurde geboren','geboren worden','ist'),('lassen','ließ','machen lassen','hat'),('möchten','möchtete','gemöchtet','hat')]:
            e=entry(lemma,pr,p2,aux=aux)
            fragment=aux+' '+p2
            self.assertTrue(quiz._check_ru_de_strict(e,lemma+' '+pr+' '+fragment)[0])
            self.assertTrue(quiz._check_ru_de_strict(e,lemma+' '+fragment,on)[0])
            self.assertFalse(quiz._check_ru_de_strict(e,lemma+' '+p2,on)[0])
    def test_revealed_paths_and_mask(self):
        e=entry('hängen','hängte, hing','gehängt, gehangen',example='Die Jacke hängt im Schrank.')
        card=F._format_word_card(e)
        self.assertNotIn('Perfekt:',card)
        self.assertIn('hilfsverb: hat',card)
        self.assertIn('Die Jacke hängt im Schrank.',card)
        for text in [F._format_de_ru_question(e),F._format_feedback_ru_de(e,True),F._format_feedback_ru_de(e,False),F._format_learning_preview_prompt(e,'ru'),V.answer(e,'phrase_ru_de')]:
            self.assertIn('hängte, hat gehängt',text)
            self.assertIn('hing, hat gehangen',text)
            self.assertNotIn('hing, hat gehängt',text)
        for text in [F._format_ru_question(e,'ru'),V.question(e,'phrase_ru_de')]:
            self.assertNotIn('gehangen',text)
        # Ordinary example stays on the L1->DE oral question.
        self.assertIn('Die Jacke hängt im Schrank.',V.question(e,'phrase_ru_de'))
    def test_legacy_diagnostic_card_remains_readable(self):
        e=entry('sich waschen','wusch sich','gewaschen',aux=None)
        e['reflexive_case']=None
        self.assertIn('sich waschen',F._format_word_card(e))
        self.assertIn('gewaschen',F._format_feedback_ru_de(e,False))
        self.assertNotIn('hat gewaschen',F._format_feedback_ru_de(e,False))
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
    def test_ordinary_example_kept_and_no_variant_only_examples_or_perfekt_block(self):
        first='Die Jacke hängt im Schrank.'
        e=entry('hängen','hängte, hing','gehängt, gehangen',example=first+'\nZweites normales Beispiel.')
        # Old frozen frames may still carry retired variant examples; never rendered.
        e['perfekt_variants_json']=OLD_FRAME_JSON
        renderings={'de_ru':F._format_de_ru_question(e),'card':F._format_word_card(e),
            'preview':F._format_learning_preview_prompt(e,'ru')}
        for name,text in renderings.items():
            self.assertEqual(text.count(first),1,name)
            self.assertNotIn('Nur im alten Variantenblock.',text)
            self.assertNotIn('Perfekt:',text)
            if name=='card':
                self.assertIn('Zweites normales Beispiel.',text)
            else:
                self.assertIn('hängte, hat gehängt',text)
                self.assertIn('hing, hat gehangen',text)
        for text in [F._format_feedback_ru_de(e,True),V.answer(e,'phrase_ru_de')]:
            self.assertNotIn(first,text)
            self.assertNotIn('Nur im alten Variantenblock.',text)
    def test_oral_forms_stay_italic_and_ordinary_example_follows(self):
        original = 'Im Zimmer stelle ich das Radio ein.'
        e = entry('einstellen', 'stellte ein', 'eingestellt', example=original)
        e['perfekt_variants_json'] = OLD_FRAME_JSON
        question = V.question(e, 'de_ru')
        self.assertIn('\n  stellte ein, hat eingestellt', question)
        text, entities = V.card(e, 'de_ru')
        start = text.index('stellte ein, hat eingestellt')
        self.assertIn(('italic', start, len('stellte ein, hat eingestellt')), entities)
        self.assertNotIn('Nur im alten Variantenblock.', question)
        self.assertIn(original, question)
        self.assertIn('\n\n' + original, V.question(e, 'ru_de'))
    def test_personal_first_hint_and_exact_case_boundary(self):
        personal='MEINE Großmutter stammt aus Polen.'
        e=entry('stammen','stammte','gestammt',example=personal+'\nPersonal second hint.')
        for text in [F._format_de_ru_question(e),F._format_learning_preview_prompt(e,'ru')]:
            self.assertEqual(text.count(personal),1)
            self.assertNotIn('Personal second hint.',text)
        card=F._format_word_card(e)
        self.assertEqual(card.count(personal),1)
        self.assertIn('Personal second hint.',card)
    def test_invalid_hilfsverb_never_fallback(self):
        for bad in ['haben','HAT','','hat/ist',1]:
            e=entry(); e['hilfsverb']=bad
            with self.assertRaises(InvalidForms): quiz._check_ru_de_strict(e,'treffen getroffen')
            with self.assertRaises(InvalidForms): F._format_feedback_ru_de(e,False)
        e=entry(); e['type']='nomen'; e['hilfsverb']='hat'
        with self.assertRaises(InvalidForms): quiz._check_ru_de_strict({**entry(),'hilfsverb':'hat','type':'verb','partizip2':''},'treffen getroffen')
    def test_frozen_alias_and_legacy_bypass(self):
        self.aliases()
        legacy=entry('sehen','sah','gesehen',wid='c',aux=None)
        storage.add_word(self.db,legacy)
        self.assertTrue(storage.add_synonym_link(self.db,'a','c')[0])
        e=self.reserve()
        self.assertEqual(set(e['study_generations']),{'a','b','c'})
        self.assertEqual(set(e['learning_lifecycles']),{'a','b','c'})
        self.assertTrue(quiz._check_ru_de_strict(e,'begegnen hat begegnet')[0])
        # Unknown-aux synonym: accepted while hilfsverb is optional, never when required.
        self.assertTrue(quiz._check_ru_de_strict(e,'sehen gesehen')[0])
        self.assertFalse(quiz._check_ru_de_strict(e,'sehen gesehen',MatchRules(hilfsverb=True))[0])
        frozen=json.loads(json.dumps(e)); e['answer_aliases'][0]['partizip2']='changed'
        with R.transaction(self.db) as c:c.execute("UPDATE words SET partizip2='future',hilfsverb=NULL WHERE id='b'")
        with patch.object(quiz,'get_synonym_group',side_effect=AssertionError('future lookup')):
            self.assertTrue(quiz._check_ru_de_strict(frozen,'begegnen hat begegnet')[0])
            old={**frozen,'hilfsverb':None};old.pop('answer_aliases')
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
        self.assertEqual(e['hilfsverb'],'hat')
        fresh=entry('sehen','sah','gesehen',wid='a',aux='ist');fresh['russian']='новый перевод'
        fresh['perfekt_variants_json']=OLD_FRAME_JSON
        refreshed=clarify_fields._preserve_selection(fresh,e)
        self.assertEqual(refreshed['russian'],'новый перевод')
        self.assertEqual((refreshed['german'],refreshed['hilfsverb']),(e['german'],'hat'))
        self.assertNotIn('perfekt_variants_json',refreshed)
        self.assertTrue(quiz._check_ru_de_strict(refreshed,'treffen hat getroffen')[0])
        old={k:v for k,v in e.items() if k not in ['hilfsverb','answer_aliases']}
        refreshed=clarify_fields._preserve_selection(fresh,old)
        self.assertIsNone(refreshed['hilfsverb'])
        self.assertTrue(quiz._check_ru_de_strict(refreshed,'treffen getroffen')[0])
        frame=D.new_frame('de_ru');frame['hidden']=D.slot(e)
        with R.transaction(self.db) as c:c.execute("UPDATE words SET hilfsverb=NULL,russian='новый перевод' WHERE id='a'")
        with patch.object(oral_repo,'selected_entry',side_effect=AssertionError('refresh reservation')):
            refreshed=OralService(self.db).refresh(1,frame,'ru')['hidden']['entry']
        self.assertEqual(refreshed['hilfsverb'],e['hilfsverb'])
        self.assertEqual(refreshed['russian'],'новый перевод')
        self.assertEqual(refreshed['german'],e['german'])
    def test_written_malformed_answer_and_idk_no_effect(self):
        storage.add_word(self.db,entry(wid='a'))
        for text in ['treffen hat getroffen',I_DONT_KNOW_TEXT]:
            e={**self.select(),'hilfsverb':'haben'};self.ctx.user_data.clear()
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
            plan['entry']['hilfsverb']='haben'
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
        self.assertFalse(quiz._check_ru_de_strict(e,'treffen geworfen')[0])
        self.assertTrue(quiz._accept_written_answer(self.update('treffen geworfen'),self.ctx,
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
        storage.add_word(self.db,entry(wid='a'));e=self.select();e['hilfsverb']='haben'
        frame=D.new_frame('de_ru');frame['pending']=D.slot(e);frame['phase']='grading';frame['revision']=1
        with self.assertRaises(InvalidForms):
            OralService(self.db).transition(1,frame,'correct',500,dict(gap=1,limit=3,dict_lang='ru'),today=NOW.date(),now=NOW)
        self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(self.rows('progress'),[])

if __name__=='__main__':unittest.main()
