"""K13 QZ01..QZ13: retained synthetic databases and fake Telegram only."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests._telegram_stub import install
install()

from bot import learning_store, storage
from bot.app_config import QUIZ_RU_DE, LEARNING_QUIZ
from bot.common import session
from bot.common.conjugation import PRONOUNS, PRAETERITUM_INFINITIVES
from bot.common.translation_hints import parse_translation_hints
from bot.common.verb_construction import (
    InvalidForms, construction_label, display_infinitive, normalize_entry,
    validate_conjugation,
)
from bot.conjugation import domain as conjugation_domain
from bot.conjugation.service import ConjugationService
from bot.handlers import learning, quiz
from bot.oral import domain as oral_domain, repository as oral_repository, view as oral_view
from bot.oral.service import OralService
from bot.search import match_entries
from bot.study import answers, generations, repair, repository as repo
from bot.text_utils import normalize_russian_singular, russian_variants
from bot.ui import formatting as F
from bot.written import service as written_service, state as written_state

STAND = Path('/Users/wf/py/_agent_work/888_german_dictionary/k13_impl_20261023')
DAY = date(2026, 10, 23)
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
VERBS = ('wasche', 'wäschst', 'wäscht', 'waschen', 'wascht', 'waschen')


def table(case='akkusativ', alternatives=False, particle=''):
    pronouns = ('mich', 'dich', 'sich', 'uns', 'euch', 'sich') if case == 'akkusativ' else ('mir', 'dir', 'sich', 'uns', 'euch', 'sich')
    rows = []
    for subject, form, reflexive in zip(PRONOUNS, VERBS, pronouns):
        fragment = f'{form} {reflexive}' + (' ' + particle if particle else '')
        rows.append(subject + ' ' + fragment + (' / ' + fragment if alternatives else ''))
    return '\n'.join(rows)


def entry(wid='akk', case='akkusativ', **fields):
    inf = 'waschen' if case == 'none' else 'sich waschen'
    return {'id': wid, 'type': 'verb', 'infinitive': inf, 'german': inf,
            'reflexive_case': case, 'partizip2': 'gewaschen',
            'prateritum': 'wusch' if case == 'none' else 'wusch sich',
            'russian': 'мыться (чем-то, кем-то)', 'ukrainian': 'митися (чимось, кимось)',
            'english': 'wash (with something, someone)', 'is_separable': 0, 'topic':'1', 'cefr_level':'A1',
            'conjugation_praesens': table(case) if case != 'none' else '',
            'conjugation_praeteritum': table(case) if case != 'none' else '',
            'example': 'Ich wasche mich.\nDu wäschst dich.\nWir waschen uns.', **fields}


class ReflexiveQuizzesTest(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='qz-', dir=STAND))
        self.db = str(self.folder / 'synthetic.sqlite')
        self.enterContext(patch.dict(os.environ, {'DB_PATH': self.db, 'OWNER_ID': '999'}))
        storage.init_db(self.db)
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO topics(name,level) VALUES('1','A1')")
            conn.executemany("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES(?,'approved','en','ru')", [(1,), (2,)])
        self.transport = 100

    def next_transport(self):
        self.transport += 1
        return self.transport

    def add(self, *items):
        for item in items or (entry(), entry('dat', 'dativ'), entry('ordinary', 'none')):
            storage.add_word(self.db, item)

    def selected(self, wid):
        with repo.transaction(self.db) as conn:
            return generations.selected_entry(conn, 1, dict(conn.execute('SELECT * FROM words WHERE id=?', (wid,)).fetchone()))

    def fake_reset(self, wid):
        # Explicit synthetic reset double; the real repair gate remains closed.
        assert Path(self.db).resolve().is_relative_to(STAND.resolve())
        with repo.transaction(self.db) as conn:
            generations.advance_generation(conn, 1, wid, generations.generation_for(conn, 1, wid))
            conn.execute('DELETE FROM progress WHERE user_id=1 AND word_id=?', (wid,))

    def grade(self, selected, key='original', transport=None, **kwargs):
        return answers.apply_original_answer(self.db, 1, selected['id'], 'ru_de', 'correct', key,
            transport or self.next_transport(), 1, NOW, flow_id='qz', live_revision=1,
            expected_generation=selected.get('study_generation', 0), **kwargs)

    def rows(self, table_name):
        with repo.transaction(self.db) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table_name + ' ORDER BY rowid')]

    def ready(self, wid='akk', pair='ru_de'):
        service = ConjugationService(self.db)
        with repo.transaction(self.db) as conn:
            repo.set_progress(conn, 1, wid, pair, {'interval': 5, 'repetitions': 2, 'ease': 2.5, 'due': DAY.isoformat()})
        service.preparation(1, pair, self.next_transport(), 'prep')
        with repo.transaction(self.db) as conn:
            b = dict(conn.execute('SELECT * FROM conjugation_blocks WHERE user_id=1 AND word_id=? AND pair=? AND tense=?', (wid,pair,'praesens')).fetchone())
        service.transition(1, pair, wid, 'praesens', b['revision'], b['cycle'], 'ready',
            self.next_transport(), 'prep', today=DAY-timedelta(days=1), now=NOW)
        return service

    def test_QZ01_labels_no_german_leak_and_grade_exact_selected_id(self):
        self.add()
        for wid, label in [('akk','AKK'), ('dat','DATIV')]:
            current = self.selected(wid)
            for lang, value in [('ru','мыться'),('uk','митися'),('en','wash')]:
                question = F._format_ru_question(current, lang)
                self.assertIn(value, question)
                self.assertIn(label, question)
                self.assertIn('verb-reflexive', question)
                self.assertNotIn('sich waschen', question)
            self.assertTrue(quiz._check_ru_de_strict(current, 'sich waschen gewaschen')[0])
        self.grade(self.selected('akk'))
        self.assertEqual([(row['word_id'],row['mode']) for row in self.rows('quiz_events')], [('akk','ru_de')])
        self.assertEqual({row['word_id'] for row in self.rows('progress')}, {'akk'})

    def test_QZ02_short_full_auxiliary_punctuation_no_automatic_sich(self):
        current = entry(id=None)
        for answer in ('sich waschen gewaschen', 'sich waschen, wusch sich. gewaschen'):
            ok, checks = quiz._check_ru_de_strict(current, answer)
            self.assertTrue(ok, answer)
            self.assertTrue(checks['partizip2'])
        for answer in ('waschen gewaschen', 'mich waschen gewaschen', 'sich sich waschen gewaschen'):
            self.assertFalse(quiz._check_ru_de_strict(current, answer)[0], answer)
        aux = {**current, 'perfect_aux':'hat'}
        self.assertTrue(quiz._check_ru_de_strict(aux, 'sich waschen hat gewaschen')[0])
        self.assertFalse(quiz._check_ru_de_strict(aux, 'sich waschen gewaschen')[0])
        ordinary = entry(case='none', id=None)
        self.assertFalse(quiz._check_ru_de_strict(ordinary, 'sich waschen gewaschen')[0])

    def test_QZ02_invalid_personal_infinitive_classification_is_rejected(self):
        from bot.common.verb_construction import InvalidClassification
        for inf in ('uns waschen','euch waschen','waschen mich'):
            with self.assertRaises(InvalidClassification):
                normalize_entry(entry(case='none',infinitive=inf,german=inf))
        for inf in ('sich mich waschen','sich mir waschen','sich sich waschen'):
            with self.assertRaises(InvalidClassification):
                normalize_entry(entry(infinitive=inf,german=inf))

    def test_QZ03_atomic_transitive_union_and_old_group_filter_own_answer(self):
        self.add()
        self.add(entry('syn','akkusativ', infinitive='sich säubern', german='sich säubern', partizip2='gesäubert'))
        self.assertTrue(storage.add_synonym_link(self.db,'akk','syn')[0])
        before = (self.rows('word_synonyms'), self.rows('synonym_groups'))
        self.assertFalse(storage.add_synonym_link(self.db,'syn','dat')[0])
        self.assertEqual(before, (self.rows('word_synonyms'), self.rows('synonym_groups')))
        with repo.transaction(self.db) as conn:
            group = conn.execute("SELECT group_id FROM word_synonyms WHERE word_id='akk'").fetchone()[0]
            conn.execute("INSERT INTO word_synonyms(word_id,group_id) VALUES('ordinary',?)", (group,))
            conn.execute("INSERT INTO word_synonyms(word_id,group_id) VALUES('dat',?)", (group,))
        selected = self.selected('akk')
        self.assertTrue(quiz._check_ru_de_strict(selected,'sich waschen gewaschen')[0])
        self.assertTrue(quiz._check_ru_de_strict(selected,'sich säubern gesäubert')[0])
        self.assertFalse(quiz._check_ru_de_strict(selected,'waschen gewaschen')[0])
        # The endpoint is compatible, but its transitive ordinary member is not.
        self.add(entry('other','akkusativ', infinitive='sich baden',german='sich baden'))
        before = (self.rows('word_synonyms'), self.rows('synonym_groups'))
        self.assertFalse(storage.add_synonym_link(self.db,'other','syn')[0])
        self.assertEqual(before, (self.rows('word_synonyms'), self.rows('synonym_groups')))

    def test_QZ03_unknown_legacy_cannot_prove_reflexive_union(self):
        self.add(entry())
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO words(id,type,infinitive,german) VALUES('legacy','verb','baden','baden')")
        self.assertFalse(storage.add_synonym_link(self.db,'akk','legacy')[0])
        self.assertEqual(self.rows('synonym_groups'), [])

    def test_QZ04_six_people_two_cases_and_each_complete_alternative(self):
        for case in ('akkusativ','dativ'):
            current = entry(case=case)
            block = validate_conjugation(current, table(case, alternatives=True, particle='um'))
            for index, line in enumerate(block.splitlines()):
                subject, expected = line.split(None,1)
                expected = expected.split(' / ')[0]
                self.assertTrue(conjugation_domain.check_form(expected, subject, expected))
                self.assertTrue(conjugation_domain.check_form(subject+' '+expected, subject, expected))
                self.assertFalse(conjugation_domain.check_form('du '+expected if index != 1 else 'ich '+expected,subject,expected))
                for bad in (VERBS[index], VERBS[index]+' ihn', VERBS[index]+' dich' if index != 1 else VERBS[index]+' mich'):
                    self.assertFalse(conjugation_domain.check_form(bad, subject, expected))
            own, other = ('mich','mir') if case == 'akkusativ' else ('mir','mich')
            for bad in (f'ich wasche {own} / wasche {other}', f'ich wasche {own} / wasche', f'ich wasche {own} / {own}', f'ich wasche {own} / du wasche {own}'):
                raw = table(case).splitlines()
                raw[0] = bad
                with self.assertRaises(InvalidForms):
                    validate_conjugation(current,'\n'.join(raw))
        for case, wrong in [('akkusativ','mir'),('dativ','mich')]:
            raw = table(case).splitlines(); raw[0] = 'ich wasche '+wrong
            self.assertIsNone(conjugation_domain.forms_for(entry(case=case,conjugation_praesens='\n'.join(raw)), 'praesens'))

    def test_QZ05_reference_examples_preserved_without_linguistic_claim(self):
        self.add(entry())
        current = storage.get_word_by_id(self.db,'akk')
        self.assertEqual(current['example'].splitlines(), ['Ich wasche mich.','Du wäschst dich.','Wir waschen uns.'])
        self.assertEqual(len(current['conjugation_praesens'].splitlines()), 6)
        self.assertEqual(len(current['conjugation_praeteritum'].splitlines()), 6)
        self.assertEqual(normalize_entry(current)['example'], current['example'])

    def test_QZ06_pair_admissions_and_two_case_progress_are_independent(self):
        self.add()
        self.grade(self.selected('akk'),key='one')
        self.grade(self.selected('akk'),key='two')
        self.assertEqual([(r['word_id'],r['pair']) for r in self.rows('conjugation_admissions')], [('akk','ru_de')])
        with repo.transaction(self.db) as conn:
            self.assertEqual(repo.get_progress(conn,1,'dat','ru_de',DAY)['repetitions'],0)
            self.assertEqual(repo.get_progress(conn,1,'akk','uk_de',DAY)['repetitions'],0)
            self.assertEqual(repo.get_progress(conn,2,'akk','ru_de',DAY)['repetitions'],0)

    def test_QZ07_separability_closed_nine_and_reference_blocks(self):
        self.assertEqual(len(PRAETERITUM_INFINITIVES),9)
        for sep in (1,None):
            self.assertFalse(conjugation_domain.eligible_word(entry(is_separable=sep)))
        self.assertTrue(conjugation_domain.eligible_word(entry()))
        for inf in ('sich waschen','mir merken'):
            legacy=entry(reflexive_case=None,infinitive=inf)
            self.assertFalse(conjugation_domain.eligible_word(legacy))
            self.assertIsNone(conjugation_domain.forms_for(legacy,'praesens'))
        self.assertIsNotNone(conjugation_domain.forms_for(entry(),'praesens'))
        self.assertIsNone(conjugation_domain.forms_for(entry(),'praeteritum'))
        self.assertIsNone(conjugation_domain.forms_for(entry(infinitive='sich sein'),'praeteritum'))
        self.assertIsNone(conjugation_domain.forms_for(entry(conjugation_praesens=''),'praesens'))
        ordinary = entry(case='none',conjugation_praesens=table().replace(' mich','').replace(' dich','').replace(' sich','').replace(' uns','').replace(' euch',''))
        self.assertIsNotNone(conjugation_domain.forms_for({**ordinary,'reflexive_case':None},'praesens'))
        self.assertEqual(len(normalize_entry(entry(is_separable=1))['conjugation_praeteritum'].splitlines()),6)

    def test_QZ08_frozen_pass_survives_edit_stop_resume_replay(self):
        self.add(entry())
        service=self.ready()
        result=service.start_test(1,'ru_de',self.next_transport(),'live',today=DAY,now=NOW)
        q=copy.deepcopy(result['question'])
        stored=self.rows('conjugation_passes')[0]
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE words SET german='obsolete',russian='changed',conjugation_praesens=? WHERE id='akk'", (table().replace('wasche ','säubere '),))
        service.stop(1,'ru_de','live',self.next_transport())
        fresh=ConjugationService(self.db).start_test(1,'ru_de',self.next_transport(),'resumed',today=DAY+timedelta(days=1),now=NOW)
        self.assertEqual(q,fresh['question'])
        expected=json.loads(stored['expected_forms_json'])[q['pronoun']]
        transport=self.next_transport()
        accepted=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],expected,'frozen',transport,'resumed',today=DAY,now=NOW)
        self.assertTrue(accepted['ok'])
        self.fake_reset('akk')
        replay=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],'wrong','frozen',transport,'resumed',today=DAY,now=NOW)
        self.assertTrue(replay['replayed'])
        stale=service.submit_form(1,'ru_de',accepted['question']['pass_id'],accepted['question']['revision'],accepted['question']['pronoun'],'wrong','new',self.next_transport(),'resumed',today=DAY,now=NOW)
        self.assertTrue(stale['stale'])
        self.assertEqual(len(self.rows('conjugation_answers')),1)

    def test_QZ09_learning_recovery_three_and_selected_generation(self):
        self.add(entry(),entry('dat','dativ'))
        with repo.transaction(self.db) as conn:
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk','dat'],batch,NOW)
            conn.execute("UPDATE learning_items SET preview_done=1,first_due_pending=0,next_due='2026-01-01'")
            chosen=learning_store.due_entries(conn,1,NOW)[0]
        for index,kind in enumerate(('wrong','correct','correct','correct')):
            receipt=answers.apply_original_answer(self.db,1,chosen['id'],'learning',kind,'learn'+str(index),self.next_transport(),1,NOW,
                flow_id='learn',live_revision=1,learning_transition=learning_store.apply_answer,
                expected_generation=chosen['study_generation'])
        self.assertEqual(receipt['after']['learning_item']['recovery_remaining'],0)
        self.assertEqual(receipt['after']['learning_item']['step_index'],1)
        rows={r['word_id']:r for r in self.rows('learning_items')}
        self.assertEqual(rows['dat']['step_index'],0)
        self.fake_reset(chosen['id'])
        before=self.rows('quiz_events')
        with self.assertRaises(generations.StaleGeneration):
            answers.apply_original_answer(self.db,1,chosen['id'],'learning','correct','old',self.next_transport(),1,NOW,
                flow_id='learn',live_revision=1,learning_transition=learning_store.apply_answer,
                expected_generation=chosen['study_generation'])
        self.assertEqual(self.rows('quiz_events'),before)

    def test_QZ10_oral_reserved_hidden_generation_and_dynamic_options(self):
        self.add(entry(),entry('dat','dativ'))
        with repo.transaction(self.db) as conn:
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk','dat'],batch,NOW)
            conn.execute("UPDATE learning_items SET status='completed'")
        service=OralService(self.db)
        options={'gap':1,'limit':1,'topics':None,'cefr_levels':None,'dict_lang':'ru'}
        first=service.start(1,'de_ru',self.next_transport(),options,DAY)['oral_frame']
        frame=service.transition(1,first,'next',self.next_transport(),options,DAY,NOW)['oral_frame']
        hidden=copy.deepcopy(frame['hidden'])
        self.assertIsNotNone(hidden)
        self.fake_reset(hidden['word_id'])
        before=(self.rows('progress'),self.rows('quiz_events'),self.rows('study_deliveries'))
        with self.assertRaises(generations.StaleGeneration):
            service.transition(1,frame,'correct',self.next_transport(),{**options,'gap':9,'limit':3},DAY,NOW)
        self.assertEqual(before,(self.rows('progress'),self.rows('quiz_events'),self.rows('study_deliveries')))
        self.assertEqual(frame['hidden'],hidden)
        with self.assertRaises(generations.StaleGeneration):
            service.refresh(1,frame,'ru')

    def test_QZ10_current_generation_oral_event_queries(self):
        self.add(entry())
        selected=self.selected('akk')
        self.grade(selected)
        self.fake_reset('akk')
        with repo.transaction(self.db) as conn:
            self.assertEqual(oral_repository.due(conn,1,'de_ru',{},DAY),[])
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk'],batch,NOW)
            conn.execute("UPDATE learning_items SET status='completed'")
            self.assertEqual(len(oral_repository.due(conn,1,'ru_de',{},DAY)),1)

    def test_QZ11_balanced_hints_languages_personal_and_malformed(self):
        for text, expected in [('мыться (чем-то, кем-то)','мыться'),('митися (чимось / кимось)','митися'),('wash (with something; someone)','wash')]:
            self.assertEqual(russian_variants(text),[expected])
            self.assertEqual(normalize_russian_singular(text),text)
            current=entry(my_translation=text)
            self.assertIn(text.replace('_','\\_'),F._format_ru_question(current,'en'))
        self.assertEqual(russian_variants('мыться (чем-то / кем-то), умываться; купаться/мыть'),['мыться','умываться','купаться','мыть'])
        self.assertEqual(russian_variants('(only a hint)'),[])
        for text in ('мыться (чем-то, кем-то','мыться ) кем-то, чем-то','за(пояснение)мок'):
            parsed=parse_translation_hints(text)
            self.assertTrue(parsed.diagnostic)
            self.assertEqual(parsed.display,text)
            self.assertEqual(parsed.variants,())
        self.assertEqual(russian_variants('wash (one (nested, hint)), bathe'),['wash','bathe'])

    def test_QZ12_preview_checks_every_original_participant_and_undo_is_selective(self):
        self.add(entry(),entry('dat','dativ'))
        with repo.transaction(self.db) as conn:
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk','dat'],batch,NOW)
            chosen=learning_store.preview_entry(conn,1)
        self.assertEqual(set(chosen['study_generations']),{'akk','dat'})
        other='dat' if chosen['id']=='akk' else 'akk'
        self.fake_reset(other)
        before=self.rows('learning_items')
        with self.assertRaises(generations.StaleGeneration):
            answers.apply_preview_transition(self.db,1,chosen['id'],'preview_next','preview',self.next_transport(),1,NOW,
                flow_id='preview',live_revision=1,transition=lambda conn,snapshot,now: learning_store.preview_transition(conn,1,chosen['id'],'preview_next',now),
                affected_learning_ids=lambda conn:learning_store.preview_affected_ids(conn,1,chosen['id']),expected_generations=generations.frame_generations(chosen))
        self.assertEqual(self.rows('learning_items'),before)
        # A v3 composite undo restores an unaffected participant after A's reset.
        with repo.transaction(self.db) as conn:
            snap=repo.capture_snapshot(conn,1,'akk',[],affected_learning_ids=['dat'])
            conn.execute("UPDATE learning_items SET step_index=2")
        self.fake_reset('akk')
        with repo.transaction(self.db) as conn:
            repo.restore_snapshot(conn,snap)
            values={r['word_id']:r['step_index'] for r in conn.execute('SELECT * FROM learning_items')}
            self.assertEqual(values,{'akk':2,'dat':0})

    def test_QZ12_written_replay_old_frame_and_correction_preserves_next_generation(self):
        self.add()
        old=self.selected('akk')
        accepted=self.grade(old,transport=800)
        self.fake_reset('akk')
        self.assertTrue(self.grade(old,transport=800)['replayed'])
        self.assertTrue(self.grade(old,transport=801)['replayed'])
        with self.assertRaises(generations.StaleGeneration):
            self.grade(old,key='fresh',transport=802)
        self.assertEqual(len(self.rows('quiz_events')),1)
        legacy={**old};legacy.pop('study_generation');legacy.pop('study_generations')
        with self.assertRaises(generations.StaleGeneration):
            self.grade(legacy,key='legacy',transport=803)

    def test_QZ13_search_and_all_german_displays_use_shared_construction(self):
        items=[entry(),entry('dat','dativ')]
        result=match_entries(items,'sich waschen')
        self.assertEqual(set(result['ordered_ids']),{'akk','dat'})
        for current in items:
            current={**current,'german':'obsolete'}
            case='AKK' if current['reflexive_case']=='akkusativ' else 'DATIV'
            for text in (F._format_word_card(current),F._format_conjugation_card(current,'ru'),F._format_feedback_ru_de(current,True),F._format_learning_preview_prompt(current,'ru'),F._format_de_ru_question(current),oral_view.answer(current,'phrase_ru_de')):
                self.assertIn('sich waschen',text)
                self.assertIn(case,text)
                self.assertNotIn('obsolete',text)

    def test_QZ12_new_pool_selection_refreshes_unreserved_only(self):
        self.add(entry(),entry('dat','dativ'))
        for wid in ('akk','dat'):
            self.grade(self.selected(wid),key=wid)
        context=SimpleNamespace(user_data={})
        real_select_due = session.select_due

        def select_on_fixture_day(*args, **kwargs):
            return real_select_due(*args, **{**kwargs, 'today': DAY})

        with patch.object(session,'_mode_l1_de',return_value='ru_de'), patch.object(session,'_mode_de_l1',return_value='de_ru'), patch.object(session,'_mode_pl_sg',return_value='pl_sg'), patch.object(session.random,'shuffle',side_effect=lambda values:None), patch.object(session,'select_due',side_effect=select_on_fixture_day):
            first=session._pick_due_entry(context,1,'ru_de')
            # The selector uses the same fixed day as the graded fixture events.
            # Backdate history below before exercising the real selector again.
            self.assertIsNone(first)
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE quiz_events SET created_at='2026-01-01'")
            conn.execute("UPDATE progress SET due='2026-01-01'")
        with patch.object(session,'_mode_l1_de',return_value='ru_de'), patch.object(session,'_mode_de_l1',return_value='de_ru'), patch.object(session,'_mode_pl_sg',return_value='pl_sg'), patch.object(session.random,'shuffle',side_effect=lambda values:None), patch.object(session,'select_due',side_effect=select_on_fixture_day):
            first=session._pick_due_entry(context,1,'ru_de')
            other='dat' if first['id']=='akk' else 'akk'
            self.fake_reset(other)
            # An unselected reset card has lost eligibility, so cannot escape
            # from the stale cached pool. The already selected first stays old.
            self.assertIsNone(session._pick_due_entry(context,1,'ru_de'))
            self.assertEqual(first['study_generation'],0)
        self.assertFalse(repair.__dict__.get('INTEGRATION_VERIFIED',False))


    def test_QZ03_explicit_legacy_reflexive_never_bypasses_none_both_directions(self):
        self.add(entry('ordinary','none'))
        for wid,inf in [('legacy','sich baden'),('personal','mir merken')]:
            with repo.transaction(self.db) as conn:
                conn.execute("INSERT INTO words(id,type,infinitive,german,partizip2) VALUES(?,'verb',?,?,'gebadet')",(wid,inf,inf))
                group=conn.execute('INSERT INTO synonym_groups DEFAULT VALUES').lastrowid
                conn.executemany('INSERT OR REPLACE INTO word_synonyms(word_id,group_id) VALUES(?,?)', [('ordinary',group),(wid,group)])
            target=self.selected('ordinary')
            self.assertFalse(quiz._check_ru_de_strict(target,inf+' gebadet')[0])
            from bot.common.verb_construction import InvalidClassification
            with self.assertRaises(InvalidClassification):
                quiz._check_ru_de_strict(self.selected(wid),'waschen gewaschen')
            self.assertFalse(storage.add_synonym_link(self.db,'ordinary',wid)[0])
        # An unrelated unresolved ordinary candidate remains compatible.
        self.add(entry('ordinary2','none',infinitive='schwimmen',german='schwimmen'))
        with repo.transaction(self.db) as conn:
            conn.execute("INSERT INTO words(id,type,infinitive,german,partizip2) VALUES('plain','verb','baden','baden','gebadet')")
        self.assertTrue(storage.add_synonym_link(self.db,'ordinary2','plain')[0])
        self.assertTrue(quiz._check_ru_de_strict(self.selected('ordinary2'),'baden gebadet')[0])

    def test_QZ11_german_fields_urls_and_add_words_gate_are_unchanged(self):
        from bot.common.add_words_gate import add_words_enabled
        from bot.handlers import add_words
        from bot.text_utils import german_form_variants
        with patch('bot.common.translation_hints.parse_translation_hints',side_effect=AssertionError('L1 only')):
            self.assertEqual(german_form_variants('hat gemacht (alt) / ist gegangen'),['hat gemacht (alt)','ist gegangen'])
            current=entry(image='https://example.invalid/a(b)/c')
            self.assertEqual(current['image'],'https://example.invalid/a(b)/c')
            self.assertEqual(add_words._german_form_variants('Häuser, Häuserchen'),['häuser','häuserchen'])
        self.assertFalse(add_words_enabled())

    def test_QZ12_fake_written_stale_grade_and_pending_drain_close_once(self):
        from test_new_logic_written_answers import WrittenTests
        for pending in (False,True):
            fixture=WrittenTests()
            fixture.setUp()
            try:
                first=fixture.update()
                first.message.fail_once=pending
                if pending:
                    with self.assertRaises(RuntimeError):
                        fixture.run_async(quiz._send_written_prompt(first,fixture.ctx,None,QUIZ_RU_DE))
                else:
                    fixture.run_async(quiz._send_written_prompt(first,fixture.ctx,None,QUIZ_RU_DE))
                old=copy.deepcopy(fixture.ctx.user_data[written_state.FRAME])
                with fixture.transaction('synthetic') as conn:
                    generations.advance_generation(conn,7,old['question_id'],0)
                incoming=fixture.update('der Hund Hunde')
                fixture.answer(incoming.message.text,update=incoming)
                self.assertNotIn(written_state.FRAME,fixture.ctx.user_data)
                self.assertEqual(fixture.rows('quiz_events'),[])
                receipt=next(row for row in fixture.rows('study_deliveries') if row['transport_update_id']==incoming.update_id)
                self.assertTrue(json.loads(receipt['result_json'])['stale'])
                fixture.answer(incoming.message.text,update=incoming)
                self.assertEqual(fixture.rows('quiz_events'),[])
            finally:
                fixture.doCleanups()

    def test_QZ12_fake_learning_stale_preview_closes_without_partial_effects(self):
        self.add(entry(),entry('dat','dativ'))
        with repo.transaction(self.db) as conn:
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk','dat'],batch,NOW)
            plan=learning._next_learning_view(conn,SimpleNamespace(user_data={}),1)
        context=SimpleNamespace(user_data={})
        learning._establish_learning_frame(context,1,plan=plan)
        context.user_data[learning.FRAME]['pending_render']=False
        self.fake_reset('dat')
        before=self.rows('learning_items')
        update=SimpleNamespace(update_id=765,effective_user=SimpleNamespace(id=1),message=SimpleNamespace(text='next'))
        with patch.object(learning,'_action_key_from_text',return_value='learning_next'),patch.object(learning,'_send_learning_menu',new=AsyncMock(return_value=123)):
            self.assertEqual(asyncio.run(learning.learning_quiz(update,context)),123)
        self.assertNotIn(learning.FRAME,context.user_data)
        self.assertEqual(self.rows('learning_items'),before)
        self.assertEqual(self.rows('quiz_events'),[])

    def test_QZ08_correction_checks_original_and_separately_preserved_next_pass(self):
        self.add(entry(),entry('dat','dativ'))
        service=self.ready('akk')
        self.ready('dat')
        view=service.start_test(1,'ru_de',self.next_transport(),'live',today=DAY,now=NOW)
        last=None
        for index in range(6):
            q=view['question']
            current=next(row for row in self.rows('conjugation_passes') if row['id']==q['pass_id'])
            expected=json.loads(current['expected_forms_json'])[q['pronoun']]
            view=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],'wrong' if index==5 else expected,'six-'+str(index),self.next_transport(),'live',today=DAY,now=NOW)
            last=view['last_answer']
        self.assertEqual(view['question']['word_id'],'dat')
        original_next=copy.deepcopy(view['question'])
        self.fake_reset('dat')
        before=self.rows('conjugation_answers')
        result=service.correct_last(1,'ru_de',last['id'],'live',self.next_transport(),current_question=original_next,today=DAY,now=NOW)
        self.assertTrue(result['stale'])
        self.assertEqual(self.rows('conjugation_answers'),before)
        self.fake_reset('akk')
        result=service.correct_last(1,'ru_de',last['id'],'live',self.next_transport(),today=DAY,now=NOW)
        self.assertTrue(result['stale'])
        self.assertEqual(self.rows('conjugation_answers'),before)

    def test_QZ12_unreserved_pool_receives_current_generation_when_still_eligible(self):
        self.add(entry(),entry('dat','dativ'))
        for wid in ('akk','dat'):
            self.grade(self.selected(wid),key=wid)
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE quiz_events SET created_at='2026-01-01'")
            conn.execute("UPDATE progress SET due='2026-01-01'")
        context=SimpleNamespace(user_data={})
        with patch.object(session,'_mode_l1_de',return_value='ru_de'),patch.object(session,'_mode_de_l1',return_value='de_ru'),patch.object(session,'_mode_pl_sg',return_value='pl_sg'),patch.object(session.random,'shuffle',side_effect=lambda values:None):
            first=session._pick_due_entry(context,1,'ru_de')
            other='dat' if first['id']=='akk' else 'akk'
            self.fake_reset(other)
            # A new qualifying event belongs to the new generation. Only this
            # unreserved pool member can capture it; first remains generation 0.
            with repo.transaction(self.db) as conn:
                repo.append_event(conn,1,other,'de_ru',False,NOW)
            selected=session._pick_due_entry(context,1,'ru_de')
            self.assertEqual(selected['id'],other)
            self.assertEqual(selected['study_generation'],1)
            self.assertEqual(first['study_generation'],0)


    def test_QZ12_new_preview_participant_requires_an_original_selection(self):
        self.add(entry(),entry('dat','dativ'))
        with repo.transaction(self.db) as conn:
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk'],batch,NOW)
            chosen=learning_store.preview_entry(conn,1)
            learning_store.add_items(conn,1,['dat'],batch,NOW)
        before=self.rows('learning_items')
        with self.assertRaises(generations.StaleGeneration):
            answers.apply_preview_transition(self.db,1,'akk','preview_next','changed-cohort',self.next_transport(),1,NOW,
                flow_id='preview',live_revision=1,transition=lambda conn,snapshot,now: learning_store.preview_transition(conn,1,'akk','preview_next',now),
                affected_learning_ids=lambda conn:learning_store.preview_affected_ids(conn,1,'akk'),expected_generations=chosen['study_generations'])
        self.assertEqual(self.rows('learning_items'),before)

    def test_QZ12_learning_menu_drain_uses_original_snapshot(self):
        self.add(entry())
        context=SimpleNamespace(user_data={})
        with repo.transaction(self.db) as conn:
            batch=learning_store.get_or_create_batch(conn,1,20,NOW)
            learning_store.add_items(conn,1,['akk'],batch,NOW)
            plan=learning._next_learning_view(conn,context,1)
        learning._establish_learning_frame(context,1,plan=plan)
        self.fake_reset('akk')
        update=SimpleNamespace(update_id=991,effective_user=SimpleNamespace(id=1))
        with patch.object(learning,'_send_learning_menu',new=AsyncMock(return_value=123)):
            self.assertEqual(asyncio.run(learning.learning_menu_choice(update,context)),123)
        self.assertNotIn(learning.FRAME,context.user_data)
        self.assertEqual(self.rows('quiz_events'),[])

    def test_QZ08_accepted_correction_replays_before_reset_parent_lookup(self):
        self.add(entry())
        service=self.ready()
        view=service.start_test(1,'ru_de',self.next_transport(),'live',today=DAY,now=NOW)
        q=view['question']
        wrong=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],'wrong','wrong',self.next_transport(),'live',today=DAY,now=NOW)
        accepted=service.correct_last(1,'ru_de',wrong['last_answer']['id'],'live',self.next_transport(),current_question=wrong['question'],today=DAY,now=NOW)
        self.assertTrue(accepted['fixed'])
        self.fake_reset('akk')
        replay=service.correct_last(1,'ru_de',wrong['last_answer']['id'],'live',self.next_transport(),current_question=wrong['question'],today=DAY,now=NOW)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['answer_id'],accepted['answer_id'])


    def test_QZ12_oral_unreserved_pool_rechecks_eligibility_and_generation(self):
        self.add(entry(),entry('dat','dativ'),entry('third','none'))
        for wid in ('akk','dat','third'):
            self.grade(self.selected(wid),key=wid)
        with repo.transaction(self.db) as conn:
            conn.execute("UPDATE quiz_events SET created_at='2026-01-01'")
            conn.execute("UPDATE progress SET due='2026-01-01'")
            frame=oral_domain.new_frame('de_ru')
            first=oral_repository.pick_original(conn,1,'de_ru',{'dict_lang':'ru'},DAY,set(),frame)
        remaining=list(frame['original_pool'])
        self.assertEqual(len(remaining),2)
        lost=remaining[0]
        self.fake_reset(lost)
        with repo.transaction(self.db) as conn:
            chosen=oral_repository.pick_original(conn,1,'de_ru',{'dict_lang':'ru'},DAY,{first['id']},frame)
            self.assertEqual(chosen['id'],remaining[1])
            self.assertNotEqual(chosen['id'],lost)
            repo.append_event(conn,1,lost,'ru_de',False,NOW)
            next_entry=oral_repository.pick_original(conn,1,'de_ru',{'dict_lang':'ru'},DAY,{first['id'],chosen['id']},frame)
        self.assertEqual(next_entry['id'],lost)
        self.assertEqual(next_entry['study_generation'],1)
        self.assertEqual(first['study_generation'],0)

    def test_QZ12_conjugation_stop_checks_original_generation_before_effects(self):
        self.add(entry())
        service=self.ready()
        view=service.start_test(1,'ru_de',self.next_transport(),'live',today=DAY,now=NOW)
        q=view['question']
        answer=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],'wrong','stop-test',self.next_transport(),'live',today=DAY,now=NOW)
        self.fake_reset('akk')
        before=self.rows('conjugation_answers')
        result=service.stop(1,'ru_de','live',self.next_transport(),expected_generations={'akk':0})
        self.assertTrue(result['stale'])
        self.assertEqual(before,self.rows('conjugation_answers'))


    def test_QA01_manual_preview_confirm_generation_receipts_and_undo(self):
        from bot.study import manual_interval
        self.add(entry())
        self.fake_reset('akk')
        chosen=self.selected('akk')
        preview=manual_interval.preview(self.db,1,'akk','ru_de',5,expected_generation=1,interval_divisor=1.65)
        before=copy.deepcopy(preview)
        accepted=manual_interval.confirm(self.db,1,'akk','ru_de',preview,'manual',800,expected_generation=1)
        self.assertEqual(accepted['after'],before)
        self.assertEqual(self.rows('quiz_events'),[])
        self.assertEqual(len(self.rows('transaction_log')),1)
        self.assertEqual([(r['word_id'],r['pair'],r['study_generation']) for r in self.rows('conjugation_admissions')],[('akk','ru_de',1)])
        answers.undo_steps(self.db,1,1,801)
        self.assertEqual(self.rows('progress'),[])
        for transport in (800,802):
            replay=manual_interval.confirm(self.db,1,'other-target','de_ru',{},'manual',transport,expected_generation=999)
            self.assertTrue(replay['replayed'])
            self.assertEqual(replay['word_id'],'akk')
            self.assertEqual(replay['state'],'undone')
        self.assertEqual(self.rows('progress'),[])
        self.assertEqual(preview,before)

    def test_QA01_manual_barrier_precedes_capture_and_all_writes(self):
        from bot.study import manual_interval
        self.add(entry())
        self.fake_reset('akk')
        with patch.object(repo,'capture_snapshot',side_effect=AssertionError('must not capture stale state')),patch.object(repo,'set_progress',side_effect=AssertionError('must not write')):
            with self.assertRaises(generations.StaleGeneration):
                manual_interval.confirm(self.db,1,'akk','ru_de',{'interval':70},'old-manual',777,expected_generation=0)
        self.assertEqual(self.rows('progress'),[])
        self.assertEqual(self.rows('transaction_log'),[])
        self.assertEqual(self.rows('study_actions'),[])
        self.assertEqual(self.rows('study_deliveries'),[])

    def test_QA01_actual_confirm_sql_failure_retains_bound_preview_and_retry(self):
        from bot.handlers import clarify_fields as CF
        from bot.study import manual_interval
        self.add(entry())
        chosen=self.selected('akk')
        preview=manual_interval.preview(self.db,1,'akk','ru_de',5,expected_generation=0)
        context=SimpleNamespace(user_data={'edit_source':'quiz','quiz_edit_entry':chosen,'edit_stage':'add_int_confirm',
            'add_int_mode':'ru_de','add_int_preview':preview,'add_int_action_key':'stable-manual',
            'add_int_selection':{'word_id':'akk','mode':'ru_de','generation':0},'clarify_flow_id':'cf'})
        update=SimpleNamespace(update_id=990,effective_user=SimpleNamespace(id=1),message=SimpleNamespace(text=CF.SYN_CONFIRM_TEXT,reply_text=AsyncMock()))
        baseline=copy.deepcopy(context.user_data)
        original=repo.append_transaction
        with patch.object(CF,'_user_interface_lang',return_value='en'),patch.object(CF,'_action_key_from_text',return_value=''),patch.object(CF,'_clarify_screen',return_value=None):
            with patch.object(repo,'append_transaction',side_effect=RuntimeError('injected atomic failure')):
                with self.assertRaisesRegex(RuntimeError,'injected atomic failure'):
                    asyncio.run(CF.clarify_fields_input(update,context))
            self.assertEqual(context.user_data,baseline)
            for table_name in ('progress','conjugation_admissions','transaction_log','study_actions','study_deliveries'):
                self.assertEqual(self.rows(table_name),[],table_name)
            asyncio.run(CF.clarify_fields_input(update,context))
            self.assertEqual(context.user_data['edit_stage'],'pick_field')
            self.assertEqual(len(self.rows('transaction_log')),1)
            self.assertEqual(len(self.rows('progress')),1)
            # A repeated accepted delivery must not interpret Confirm as B input.
            context.user_data.update(quiz_edit_entry=entry('dat','dativ'),edit_stage='enter_value',edit_field='russian')
            current=copy.deepcopy(context.user_data)
            with patch.object(CF,'_clarify_fields_input_locked',side_effect=AssertionError('old confirmation resolved B')):
                asyncio.run(CF.clarify_fields_input(update,context))
            self.assertEqual(context.user_data,current)
            self.assertEqual(len(self.rows('transaction_log')),1)
        self.assertEqual(self.rows('quiz_events'),[])

    def test_QA05_fresh_search_snapshot_and_old_open_never_freshens(self):
        import sqlite3
        from bot.search import read_search_entry
        from bot.handlers import clarify_fields as CF
        from bot.common.search_view import SEARCH_KEY,ORIGIN_KEY
        self.add(entry())
        with repo.transaction(self.db) as conn:
            conn.execute('PRAGMA journal_mode')
        writer=sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        reader=sqlite3.connect(self.db)
        self.addCleanup(reader.close)
        changed=[]
        def mutate(sql):
            if sql.startswith('SELECT generation FROM study_word_generations') and not changed:
                changed.append(True)
                writer.execute("UPDATE words SET russian='changed' WHERE id='akk'")
                writer.execute("INSERT INTO study_word_generations(user_id,word_id,generation) VALUES(1,'akk',1)")
                writer.commit()
        reader.set_trace_callback(mutate)
        selected=read_search_entry(self.db,'akk',user_id=1,conn=reader)
        self.assertEqual(selected['study_generation'],0)
        self.assertEqual(selected['russian'],'мыться (чем-то, кем-то)')
        fresh=read_search_entry(self.db,'akk',user_id=1)
        self.assertEqual((fresh['study_generation'],fresh['russian']),(1,'changed'))
        self.assertEqual(changed,[True])
        state={'query_id':'q','revision':1}
        origin={'user_id':1,'chat_id':1,'query_id':'q','revision':1,'word_id':'akk'}
        context=SimpleNamespace(user_data={SEARCH_KEY:state,ORIGIN_KEY:origin,'edit_source':'quiz',
            'edit_return_state':__import__('bot.app_config',fromlist=['DB_SEARCH_VIEW']).DB_SEARCH_VIEW,
            'quiz_edit_entry':selected,'edit_stage':'pick_field'})
        update=SimpleNamespace(update_id=350,effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=1),message=SimpleNamespace(text='send',reply_text=AsyncMock()))
        with patch('bot.handlers.access.access_allowed',return_value=True),patch.object(CF,'_user_interface_lang',return_value='en'),patch.object(CF,'_action_key_from_text',return_value='send_learn'),patch.object(CF,'return_to_search',new=AsyncMock(return_value=123)):
            self.assertEqual(asyncio.run(CF.clarify_fields_input(update,context)),123)
        self.assertEqual(context.user_data['quiz_edit_entry']['study_generation'],0)
        self.assertEqual(self.rows('learning_items'),[])

    def test_QA05_explicit_report_open_reserves_current_generation(self):
        from bot.handlers import clarify_fields as CF
        self.add(entry())
        old=self.selected('akk')
        self.fake_reset('akk')
        context=SimpleNamespace(user_data={})
        message=SimpleNamespace(reply_text=AsyncMock())
        update=SimpleNamespace(effective_user=SimpleNamespace(id=1),callback_query=SimpleNamespace(message=message))
        with patch.object(CF,'_format_word_card',return_value='card'),patch.object(CF,'_clarify_screen',return_value=None),patch.object(CF,'_user_interface_lang',return_value='en'):
            asyncio.run(CF.open_clarify_from_report(update,context,old))
        self.assertEqual(context.user_data['quiz_edit_entry']['study_generation'],1)

    def test_QA03_cross_transport_replays_wrong_and_correct_effects_only(self):
        from bot.conjugation import repository as cr
        for correct in (False,True):
            # Independent pairs keep keys/session/deliveries distinct.
            wid='correct' if correct else 'wrong'
            self.add(entry(wid,infinitive='sich '+wid,german='sich '+wid))
            service=self.ready(wid)
            view=service.start_test(1,'ru_de',self.next_transport(),wid,today=DAY,now=NOW)
            q=view['question']
            current=next(row for row in self.rows('conjugation_passes') if row['id']==q['pass_id'])
            expected=json.loads(current['expected_forms_json'])[q['pronoun']]
            accepted=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],expected if correct else 'wrong','accepted-'+wid,self.next_transport(),wid,today=DAY,now=NOW)
            self.assertIs(accepted['ok'],correct)
            self.fake_reset(wid)
            with patch.object(cr,'pass_row',side_effect=AssertionError('accepted replay read mutable parent')):
                replay=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],'whatever','accepted-'+wid,self.next_transport(),wid,today=DAY,now=NOW)
            self.assertTrue(replay['replayed'])
            self.assertIs(replay['ok'],correct)

    def test_QA03_no_effect_correction_does_not_poison_later_legal_action(self):
        self.add(entry())
        service=self.ready()
        transport=self.next_transport()
        noop=service.correct_last(1,'ru_de',1,'live',transport,today=DAY,now=NOW)
        self.assertFalse(noop['fixed'])
        view=service.start_test(1,'ru_de',self.next_transport(),'live',today=DAY,now=NOW)
        q=view['question']
        wrong=service.submit_form(1,'ru_de',q['pass_id'],q['revision'],q['pronoun'],'wrong','fresh',self.next_transport(),'live',today=DAY,now=NOW)
        self.assertEqual(wrong['last_answer']['id'],1)
        replay=service.correct_last(1,'ru_de',1,'live',transport,current_question=wrong['question'],today=DAY,now=NOW)
        self.assertTrue(replay['replayed'])
        self.assertFalse(replay['fixed'])
        fixed=service.correct_last(1,'ru_de',1,'live',self.next_transport(),current_question=wrong['question'],today=DAY,now=NOW)
        self.assertTrue(fixed['fixed'])
        self.fake_reset('akk')
        from bot.conjugation import repository as cr
        with patch.object(cr,'pass_row',side_effect=AssertionError('accepted correction read parent')):
            accepted_replay=service.correct_last(1,'ru_de',1,'live',self.next_transport(),current_question=wrong['question'],today=DAY,now=NOW)
        self.assertTrue(accepted_replay['replayed'])
        self.assertTrue(accepted_replay['fixed'])

    def test_QA04_ordinary_surfaces_keep_prior_labels_and_reflexive_are_distinct(self):
        from bot.common.verb_construction import reflexive_hint
        ordinary=entry(case='none',infinitive='GEHEN',german='obsolete',id=None)
        self.assertEqual(reflexive_hint(ordinary),'')
        self.assertTrue(F._format_learning_preview_prompt(ordinary,'ru').startswith('*gehen*'))
        self.assertNotIn('(verb)',F._format_feedback_ru_de(ordinary,True).splitlines()[0])
        self.assertNotIn('(verb)',F._format_conjugation_card(ordinary,'ru').splitlines()[0])
        self.assertNotIn('(verb)',F._format_de_ru_question(ordinary).splitlines()[0])
        self.assertIn('(verb)',F._format_ru_question(ordinary,'ru'))
        for case in ('akkusativ','dativ'):
            self.assertIn('verb-reflexive',reflexive_hint(entry(case=case)))


    def test_QA01_actual_no_network_main_manual_sql_retry_and_receipt_before_B(self):
        import subprocess,sys
        from test_new_logic_qa_oral import main_oral_script
        script=main_oral_script(r"""
            from bot.oral import repository as oral_repository
            reset(); await enter('DE','Words'); await send(t('en','oral_next'))
            await send(t('en','clarify_fields')); await send('add_int'); await send('3')
            pinned=copy.deepcopy(h.ctx.user_data['add_int_preview'])
            slots=copy.deepcopy((h.frame()['pending'],h.frame()['hidden']))
            confirm=update(clarify.SYN_CONFIRM_TEXT)
            before=rows('progress')
            with patch.object(study_repo,'append_transaction',side_effect=RuntimeError('manual SQL fault')):
                await app.process_update(confirm)
            assert len(errors)==1 and str(errors.pop())=='manual SQL fault'
            assert h.ctx.user_data['edit_stage']=='add_int_confirm'
            assert h.ctx.user_data['add_int_preview']==pinned
            assert rows('progress')==before and rows('transaction_log')==[]
            assert rows('study_actions')==[] and rows('quiz_events')==[]
            await process(confirm)
            assert h.srs(slots[0]['word_id'],'de_ru')==pinned
            assert len(rows('transaction_log'))==1 and rows('quiz_events')==[]
            assert (h.frame()['pending'],h.frame()['hidden'])==slots
            target=oral_repository.word(h.conn,UID,slots[1]['word_id'],'ru')
            h.ctx.user_data.update(quiz_edit_entry=target,edit_stage='enter_value',edit_field='russian')
            chosen=copy.deepcopy(h.ctx.user_data['quiz_edit_entry'])
            await process(confirm)
            assert h.ctx.user_data['quiz_edit_entry']==chosen
            assert h.ctx.user_data['edit_stage']=='enter_value'
            assert len(rows('transaction_log'))==1 and rows('quiz_events')==[]
        """)
        result=subprocess.run([sys.executable,'-B','-c',script],cwd=str(Path(__file__).resolve().parents[1]),env=os.environ.copy(),capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('network=0',result.stdout)

    def test_QA02_stale_replay_does_not_close_newer_question_same_flow(self):
        from bot.handlers import conjugation_test as CT
        frame=CT._new_frame('ru_de','test')
        frame.update(flow_id='same-flow',action_key='new-action',question={'word_id':'dat'})
        context=SimpleNamespace(user_data={CT.FRAME:frame})
        update=SimpleNamespace(effective_user=SimpleNamespace(id=1))
        with patch.object(CT,'_close_stale',new=AsyncMock(side_effect=AssertionError('closed later view'))):
            asyncio.run(CT._replay_state(update,context,{'replayed':True,'stale':True,'flow_id':'same-flow','action_key':'old-action'}))
        self.assertIs(context.user_data[CT.FRAME],frame)


if __name__ == '__main__':
    unittest.main()
