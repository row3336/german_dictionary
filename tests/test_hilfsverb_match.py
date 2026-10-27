"""Point 28: personal preference, launch snapshots, real receipts and diagnostics."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_perfekt_quizzes as fixture
from tests import test_new_logic_learning as learning_fixture
from bot import storage
from bot.common import match_policy as P
from bot.common.hilfsverb import MatchRules
from bot.common.verb_construction import InvalidForms
from bot.handlers import quiz as Q, learning as L, menu as M, settings as S
from bot.ui import keyboards as K
from bot.common.navigation import _action_key_from_text
from bot.i18n import t
from bot.written import state as W
from bot.app_config import QUIZ_RU_DE, I_DONT_KNOW_TEXT


class HilfsverbMatchTests(unittest.TestCase):
    setUp = fixture.PerfektQuizzes.setUp
    update = fixture.PerfektQuizzes.update
    rows = fixture.PerfektQuizzes.rows
    select = fixture.PerfektQuizzes.select

    def flag(self, enabled, user=1):
        storage.set_user_setting(self.db, user, P.KEY, '1' if enabled else '0')

    def test_personal_default_ignores_global_and_other_user(self):
        storage.set_setting(self.db, P.KEY, '1')
        self.flag(True, 999)
        self.assertFalse(P.enabled(self.db, 1))
        self.flag(True)
        self.assertTrue(P.enabled(self.db, 1))
        self.assertFalse(P.enabled(self.db, 2))
        self.assertEqual(json.loads(json.dumps(P.capture(self.db, 1))),
                         {'hilfsverb': True, 'prateritum': False})

    def test_legacy_and_non_boolean_snapshot_are_off(self):
        self.flag(True)
        for frame in (None, {}, {P.FRAME_KEY: {}}, {P.FRAME_KEY: {'hilfsverb': 'true'}}):
            self.assertFalse(P.rules(frame).hilfsverb)
        self.assertFalse(P.rules({P.FRAME_KEY: {'hilfsverb': True, 'prateritum': True}}).prateritum)

    def test_punctuation_case_newlines_keep_order_and_wrong_tokens(self):
        e = fixture.entry()
        on = MatchRules(hilfsverb=True)
        for text in ('TREFFEN; HAT\nGETROFFEN!', 'treffen — traf: hat getroffen', 'treffen\ngetroffen'):
            expected = 'hat' in text.lower()
            self.assertEqual(Q._check_ru_de_strict(e, text, on)[0], expected, text)
        for text in ('hat treffen getroffen', 'treffen HATT getroffen', 'treffen ist getroffen',
                     'treffen hat nicht getroffen', 'treffen hat ist getroffen'):
            for rules in (on, MatchRules()):
                self.assertFalse(Q._check_ru_de_strict(e, text, rules)[0], text)

    def test_reflexive_and_wechsel(self):
        e = fixture.entry('sich freuen', 'freute sich', 'gefreut', case='akkusativ')
        on = MatchRules(hilfsverb=True)
        self.assertTrue(Q._check_ru_de_strict(e, 'SICH FREUEN; HAT SICH GEFREUT', on)[0])
        for text in ('sich freuen hat gefreut', 'sich freuen sich hat gefreut'):
            self.assertFalse(Q._check_ru_de_strict(e, text, on)[0])
        e = fixture.entry('fahren', 'fuhr', 'gefahren', aux='wechsel')
        for aux in ('hat', 'ist'):
            self.assertTrue(Q._check_ru_de_strict(e, f'fahren {aux} gefahren', on)[0])
        self.assertFalse(Q._check_ru_de_strict(e, 'fahren wechsel gefahren', on)[0])

    def test_unknown_target_cannot_be_bypassed_by_alias(self):
        e = fixture.entry(aux=None, answer_aliases=[fixture.entry()])
        with self.assertRaises(InvalidForms):
            Q._check_ru_de_strict(e, 'treffen hat getroffen', MatchRules(hilfsverb=True))

    def test_toggle_languages_status_and_bottom_row(self):
        for lang in ('en', 'ru', 'uk'):
            self.flag(False)
            storage.set_user_interface_lang(self.db, 1, lang)
            u = self.update('Hilfsverb_match')
            self.assertEqual(_action_key_from_text(u.message.text, 1), 'hilfsverb_match')
            asyncio.run(S.fsrs_settings_menu_choice(u, self.ctx))
            self.assertTrue(P.enabled(self.db, 1))
            texts = [c.args[0] for c in u.message.reply_text.call_args_list]
            self.assertIn('Hilfsverb_match = ON', texts[0])
            self.assertEqual(len(texts[0].splitlines()), 2)
            self.assertLess(texts[-1].index(t(lang, 'hilfsverb_match_status')), texts[-1].index(t(lang, 'ses_rtr_gap')))
            self.assertIn('• [ON] - ' + t(lang, 'hilfsverb_match_status'), texts[-1])
            self.assertEqual(K._fsrs_settings_menu_keys(1)[-1], ['Hilfsverb_match', t(lang, 'back')])
            self.assertFalse(P.enabled(self.db, 2))
            asyncio.run(S.fsrs_settings_menu_choice(self.update('Hilfsverb_match'), self.ctx))
            self.assertFalse(P.enabled(self.db, 1))

    def test_reset_defaults_only_this_user_next_launch(self):
        self.flag(True); self.flag(True, 999)
        self.ctx.user_data[W.FRAME] = {P.FRAME_KEY: P.capture(self.db, 1)}
        with patch.object(S, '_send_algo_settings_menu', new=AsyncMock()):
            asyncio.run(S.algo_settings_menu_choice(self.update(t('en', 'reset_defaults')), self.ctx))
        self.assertFalse(P.enabled(self.db, 1))
        self.assertTrue(P.enabled(self.db, 999))
        self.assertTrue(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)

    def launch(self):
        with patch.object(M, '_action_key_from_text', return_value='quiz_words'), \
             patch.object(M, '_mode_l1_de', return_value='ru_de'), \
             patch.object(M, '_send_ru_de_prompt', new=AsyncMock(return_value=QUIZ_RU_DE)):
            asyncio.run(M.quiz_ru_de_menu_choice(self.update('start'), self.ctx))

    def test_written_explicit_launch_captures_then_next_view_retains(self):
        self.launch()
        old = self.ctx.user_data[W.FRAME]['flow_id']
        self.flag(True)
        W.establish_view(self.ctx, 'ru_de', QUIZ_RU_DE, fixture.entry(wid='a'))
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
        W.establish_view(self.ctx, 'ru_de', QUIZ_RU_DE, fixture.entry(wid='b'))
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
        self.launch()
        self.assertTrue(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
        self.assertNotEqual(old, self.ctx.user_data[W.FRAME]['flow_id'])

    def test_missing_aux_idk_diagnostic_has_no_grade_or_count(self):
        storage.add_word(self.db, fixture.entry(wid='a', aux=None))
        W.establish_view(self.ctx, 'ru_de', QUIZ_RU_DE, self.select())
        self.ctx.user_data[W.FRAME][P.FRAME_KEY] = {'hilfsverb': True}
        before = {table: self.rows(table) for table in ('progress', 'quiz_events', 'study_actions')}
        with patch.object(Q, '_user_interface_lang', return_value='en'), \
             patch.object(Q, '_action_key_from_text', return_value=None), \
             patch.object(Q, '_mode_l1_de', return_value='ru_de'), \
             patch.object(Q, '_back_to_main_menu', new=AsyncMock()):
            asyncio.run(Q.quiz_ru_de(self.update(I_DONT_KNOW_TEXT), self.ctx))
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        self.assertNotIn(W.FRAME, self.ctx.user_data)
        self.assertFalse(self.ctx.user_data.get('quiz_seen_ids'))


class LearningPolicyTests(unittest.TestCase):
    setUp = learning_fixture.LearningTests.setUp
    update = learning_fixture.LearningTests.update
    rows = learning_fixture.LearningTests.rows
    add = learning_fixture.LearningTests.add

    def test_accepted_start_receipt_and_replay_keep_policy(self):
        self.add(ready=True)
        u = self.update('learning_start')
        asyncio.run(L.learning_menu_choice(u, self.ctx))
        original = copy.deepcopy(self.ctx.user_data[L.FRAME])
        self.assertFalse(P.rules(original).hilfsverb)
        storage.set_user_setting(self.db, 1, P.KEY, '1')
        asyncio.run(L.learning_menu_choice(u, self.ctx))
        self.assertEqual(self.ctx.user_data[L.FRAME], original)
        # Direct durable reducer replay also restores the accepted result, not today's setting.
        asyncio.run(L._learning_menu_locked(u, self.ctx))
        self.assertFalse(P.rules(self.ctx.user_data[L.FRAME]).hilfsverb)
        self.assertEqual(self.ctx.user_data[L.FRAME]['flow_id'], original['flow_id'])
        asyncio.run(L.learning_menu_choice(self.update('learning_start'), self.ctx))
        self.assertTrue(P.rules(self.ctx.user_data[L.FRAME]).hilfsverb)

    def test_learning_missing_aux_idk_preserves_progress_and_queue(self):
        from bot.study import repository
        with repository.transaction(self.db) as conn:
            conn.execute("UPDATE words SET type='verb',german='treffen',infinitive='treffen',"
                         "prateritum='traf',partizip2='getroffen',reflexive_case='none' WHERE id='a'")
        self.add(ready=True)
        storage.set_user_setting(self.db, 1, P.KEY, '1')
        asyncio.run(L.learning_menu_choice(self.update('learning_start'), self.ctx))
        before = {table: self.rows(table) for table in ('progress', 'quiz_events', 'learning_items', 'study_actions')}
        asyncio.run(L.learning_quiz(self.update(I_DONT_KNOW_TEXT), self.ctx))
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        self.assertNotIn(L.FRAME, self.ctx.user_data)

    def test_learning_next_and_answer_use_captured_rules(self):
        self.add(ready=True)
        asyncio.run(L.learning_menu_choice(self.update('learning_start'), self.ctx))
        storage.set_user_setting(self.db, 1, P.KEY, '1')
        with patch.object(L, '_check_ru_de_strict', return_value=(True, {})) as check:
            asyncio.run(L.learning_quiz(self.update('good'), self.ctx))
            self.assertFalse(check.call_args.args[2].hilfsverb)
        self.assertFalse(P.rules(self.ctx.user_data.get(L.FRAME)).hilfsverb)


from tests import test_new_logic_written_answers as written_fixture
from bot.common import quiz_resume


class WrittenPolicyTransitions(unittest.TestCase):
    transaction = written_fixture.WrittenTests.transaction
    word = written_fixture.WrittenTests.word
    pick = written_fixture.WrittenTests.pick
    update = written_fixture.WrittenTests.update
    run_async = written_fixture.WrittenTests.run_async
    start = written_fixture.WrittenTests.start
    answer = written_fixture.WrittenTests.answer
    rows = written_fixture.WrittenTests.rows

    def setUp(self):
        written_fixture.WrittenTests.setUp(self)
        for field in ('prateritum', 'partizip2', 'hilfsverb', 'reflexive_case'):
            if field not in {r[1] for r in self.conn.execute('PRAGMA table_info(words)')}:
                self.conn.execute('ALTER TABLE words ADD COLUMN ' + field + ' TEXT')
        self.conn.execute("UPDATE words SET type='verb',german='treffen',infinitive='treffen',"
                          "prateritum='traf',partizip2='getroffen',hilfsverb='hat',reflexive_case='none' WHERE id='a'")
        self.conn.commit()
        self.start()
        self.ctx.user_data[W.FRAME][P.FRAME_KEY] = {'hilfsverb': False, 'prateritum': False}
        self.enterContext(patch.object(P, 'enabled', side_effect=AssertionError('live settings read')))

    def test_original_question2_replay_clarify_return_and_input_err_keep_off(self):
        update = self.update('treffen getroffen')
        self.answer('', update=update)
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'], 1)
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
        before = copy.deepcopy(self.ctx.user_data[W.FRAME])
        self.answer('', update=update)
        self.assertEqual(self.ctx.user_data[W.FRAME], before)
        quiz_resume.capture_return_point(self.ctx, QUIZ_RU_DE)
        self.run_async(quiz_resume.render_existing_frame(self.update(), self.ctx))
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
        self.answer('wrong')
        self.answer('fix')
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
        self.assertEqual([r['is_correct'] for r in self.rows('quiz_events')], [1, 1])
        self.assertEqual(len(self.ctx.user_data['session_seen_ru_de']), 2)

    def test_drill_uses_off_without_another_original_event_or_count(self):
        self.ctx.user_data['quiz_is_retry'] = True
        before = self.rows('progress')
        self.answer('treffen getroffen')
        self.assertEqual(self.rows('progress'), before)
        self.assertEqual(self.rows('quiz_events'), [])
        self.assertEqual(len(self.ctx.user_data['session_seen_ru_de']), 0)
        self.assertFalse(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)

    def test_on_is_passed_to_written_checker(self):
        self.ctx.user_data[W.FRAME][P.FRAME_KEY]['hilfsverb'] = True
        self.answer('treffen getroffen')
        self.assertEqual(self.rows('quiz_events')[0]['is_correct'], 0)
        self.assertTrue(P.rules(self.ctx.user_data[W.FRAME]).hilfsverb)
