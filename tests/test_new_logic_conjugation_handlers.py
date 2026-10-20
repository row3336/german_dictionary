"""Real conjugation handlers against a memory database and fake Telegram Updates."""

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from _telegram_stub import install

install()

import test_new_logic_conjugation as fixtures
from test_new_logic_conjugation import FORMS, TODAY, NOW
from bot.handlers import conjugation as preparation
from bot.handlers import conjugation_test as testing
from bot.app_config import CONJUGATION_MENU, CONJUGATION_TEST, MENU, TESTS_MENU
from bot.conjugation.service import ConjugationService
from bot.i18n import t
from telegram.error import BadRequest


class Message:
    def __init__(self, text='', mid=100):
        self.text = text
        self.message_id = mid
        self.sent = []
        self.fail_once = False

    async def reply_text(self, text, **kwargs):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('synthetic send failure')
        self.sent.append((text, kwargs.get('reply_markup')))
        return Message(text, self.message_id+len(self.sent))

    async def delete(self):
        pass


class Bot:
    async def edit_message_reply_markup(self, **kwargs):
        pass

    async def edit_message_text(self, **kwargs):
        pass

    async def send_message(self, text=None, **kwargs):
        return Message(text)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ConjugationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ctx = SimpleNamespace(user_data={}, bot=Bot())
        for module in (preparation, testing):
            self.enterContext(patch.object(module, '_service', return_value=self.f.service))
            self.enterContext(patch.object(module, '_mode_l1_de', return_value='ru_de'))
            self.enterContext(patch.object(module, '_user_interface_lang', return_value='en'))
            self.enterContext(patch.object(module, '_action_key_from_text', side_effect=self.action))
        self.enterContext(patch.object(preparation, '_user_dict_lang', return_value='ru'))
        self.enterContext(patch.object(preparation, '_back_to_tests', side_effect=self.back_tests))
        self.enterContext(patch.object(testing, '_back_to_main_menu', side_effect=self.back_main))
        self.enterContext(patch.object(ConjugationService, '_clock',
                                     side_effect=lambda today=None, now=None: (today or TODAY, (now or NOW).isoformat())))

    @staticmethod
    def action(text, _):
        return {t('en', 'input_err'): 'input_err', t('en', 'conjugation_dice'): 'conjugation_dice',
                t('en', 'conjugation_stop'): 'stop'}.get(text, '')

    @staticmethod
    async def back_main(*_):
        return MENU

    @staticmethod
    async def back_tests(*_):
        return TESTS_MENU

    def update(self, text='', transport=None, user=7):
        return SimpleNamespace(message=Message(text), effective_user=SimpleNamespace(id=user),
                               effective_chat=SimpleNamespace(id=user),
                               update_id=transport or self.f.delivery())

    def call(self, handler, update=None):
        return asyncio.run(handler(update or self.update(), self.ctx))

    def frame(self):
        return self.ctx.user_data[preparation.FRAME]

    def answers(self):
        return self.f.conn.execute('SELECT COUNT(*) FROM conjugation_answers').fetchone()[0]

    def callback(self, operation):
        token = next(k for k, v in self.frame()['callbacks'].items() if v['operation'] == operation)
        update = self.update()
        async def answer():
            pass
        update.callback_query = SimpleNamespace(data='conj:'+operation+':'+token,
                                                message=update.message, answer=answer)
        return update

    def start_due(self, two=False):
        self.f.add('a'); self.f.ready('a')
        if two:
            self.f.add('b'); self.f.ready('b')
        self.call(testing.start_conjugation_test)

    def answer(self, text=None):
        q = self.frame()['question']
        return self.call(testing.conjugation_test_input,
                         self.update(FORMS[q['pronoun']] if text is None else text))

    def test_empty_preparation_keeps_test_entry_and_keyboard(self):
        update = self.update()
        self.assertEqual(self.call(preparation.start_conjugation, update), CONJUGATION_MENU)
        keyboard = update.message.sent[0][1]
        rows = getattr(keyboard, 'keyboard', None) or keyboard.rows
        labels = [getattr(button, 'text', button) for row in rows for button in row]
        self.assertIn(t('en', 'conjugation_test'), labels)
        self.assertEqual(self.call(preparation.conjugation_input,
                                   self.update(t('en', 'conjugation_test'))), CONJUGATION_TEST)
        self.assertIsNone(self.frame()['question'])

    def test_inline_ready_moves_only_selected_tense_and_hides_its_forms(self):
        self.f.add(infinitive='haben')
        self.call(preparation.start_conjugation)
        cb = self.callback('ready')
        target = self.frame()['callbacks'][cb.callback_query.data.split(':')[2]]
        tense = target['tense']
        self.assertEqual(self.call(preparation.conjugation_card_callback, cb), CONJUGATION_MENU)
        self.assertEqual(self.f.block(tense=tense)['phase'], 'test')
        self.assertEqual(self.frame()['entry']['conjugation_'+tense], '')
        self.assertEqual(len([b for b in self.frame()['blocks'] if b['phase'] == 'preparation']), 1)
        self.assertEqual(self.f.block(tense=tense)['due'], '2026-10-21')

    def test_last_form_correction_preserves_selected_next_question_and_action_key(self):
        self.start_due(two=True)
        for _ in range(5): self.answer()
        self.answer('typo')
        question, key = dict(self.frame()['question']), self.frame()['action_key']
        self.assertEqual(question['word_id'], 'b')
        self.call(testing.conjugation_test_input, self.update(t('en', 'input_err')))
        self.assertEqual(self.frame()['question'], question)
        self.assertEqual(self.frame()['action_key'], key)
        self.assertIsNone(self.frame()['last_answer'])
        self.assertEqual(self.answers(), 6)
        self.assertEqual(self.f.block()['repetitions'], 1)

    def test_stop_and_restart_resume_same_pass_but_close_correction(self):
        self.start_due(); self.answer('typo')
        pid = self.frame()['question']['pass_id']
        self.assertEqual(self.call(testing.conjugation_test_input,
                                   self.update(t('en', 'conjugation_stop'))), MENU)
        self.assertNotIn(preparation.FRAME, self.ctx.user_data)
        self.call(testing.start_conjugation_test)
        self.assertEqual(self.frame()['question']['pass_id'], pid)
        self.assertEqual(self.frame()['question']['ordinal'], 2)
        self.assertIsNone(self.frame()['last_answer'])
        self.call(testing.conjugation_test_input, self.update(t('en', 'input_err')))
        self.assertEqual(self.f.block()['repetitions'], 0)
        self.assertEqual(self.answers(), 1)

    def test_repeated_transport_never_grades_new_current_question(self):
        self.start_due()
        update = self.update(FORMS[self.frame()['question']['pronoun']])
        self.call(testing.conjugation_test_input, update)
        question, key = dict(self.frame()['question']), self.frame()['action_key']
        self.call(testing.conjugation_test_input, update)
        self.assertEqual(self.answers(), 1)
        self.assertEqual(self.frame()['question'], question)
        self.assertEqual(self.frame()['action_key'], key)

    def test_send_failure_replay_renders_committed_current_frame_without_grade(self):
        self.start_due()
        update = self.update(FORMS[self.frame()['question']['pronoun']])
        update.message.fail_once = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'):
            self.call(testing.conjugation_test_input, update)
        question = dict(self.frame()['question'])
        self.assertEqual(self.answers(), 1)
        self.assertTrue(self.frame()['pending_render'])
        self.call(testing.conjugation_test_input, update)
        self.assertEqual(self.answers(), 1)
        self.assertEqual(self.frame()['question'], question)
        self.assertFalse(self.frame()['pending_render'])
        self.assertTrue(update.message.sent)

    def test_future_idle_block_can_be_returned_to_preparation(self):
        self.f.add(); self.f.ready(day=TODAY)
        self.call(testing.start_conjugation_test)
        self.assertIsNone(self.frame()['question'])
        self.assertEqual(self.call(preparation.conjugation_card_callback, self.callback('reset')), CONJUGATION_MENU)
        self.assertEqual(self.frame()['kind'], 'preparation')
        self.assertEqual(self.f.block()['phase'], 'preparation')
        self.assertEqual(self.f.block()['cycle'], 2)

    def reset_with_failed_send(self):
        self.f.add(); self.f.ready(day=TODAY)
        self.call(testing.start_conjugation_test)
        update = self.callback('reset')
        update.message.fail_once = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'):
            self.call(preparation.conjugation_card_callback, update)
        self.assertEqual(self.f.block()['cycle'], 2)
        self.assertEqual(self.f.block()['phase'], 'preparation')
        self.assertTrue(self.frame()['pending_render'])
        return update

    def test_committed_reset_send_failure_replays_without_token_or_second_effect(self):
        update = self.reset_with_failed_send()
        before = self.f.block()
        self.assertFalse(self.frame()['callbacks'])
        self.assertEqual(self.frame()['pending_render_transport'], update.update_id)
        with patch.object(self.f.service, 'transition', side_effect=AssertionError('second reset')), \
             patch.object(self.f.service, 'preparation', side_effect=AssertionError('picker')):
            self.assertEqual(self.call(preparation.conjugation_card_callback, update), CONJUGATION_MENU)
        self.assertEqual(self.f.block(), before)
        self.assertEqual(self.answers(), 0)
        self.assertFalse(self.frame()['pending_render'])
        self.assertTrue(update.message.sent)

    def test_expired_callback_ack_does_not_block_committed_reset_recovery(self):
        update = self.reset_with_failed_send()
        before = self.f.block()
        async def expired():
            raise BadRequest('Query is too old and response timeout expired')
        update.callback_query.answer = expired
        with patch.object(self.f.service, 'transition', side_effect=AssertionError('second reset')), \
             patch.object(self.f.service, 'preparation', side_effect=AssertionError('picker')):
            self.assertEqual(self.call(preparation.conjugation_card_callback, update), CONJUGATION_MENU)
        self.assertFalse(self.frame()['pending_render'])
        self.assertTrue(update.message.sent)
        self.assertEqual(self.f.block(), before)
        self.assertEqual(self.answers(), 0)

    def test_non_telegram_callback_ack_error_is_not_swallowed(self):
        update = self.reset_with_failed_send()
        before = self.f.block()
        async def broken():
            raise RuntimeError('synthetic acknowledgement bug')
        update.callback_query.answer = broken
        with self.assertRaisesRegex(RuntimeError, 'synthetic acknowledgement bug'):
            self.call(preparation.conjugation_card_callback, update)
        self.assertTrue(self.frame()['pending_render'])
        self.assertEqual(self.f.block(), before)
        self.assertEqual(update.message.sent, [])

    def test_late_reset_replay_does_not_render_a_later_pending_delivery(self):
        old_reset = self.reset_with_failed_send()
        following = self.update(t('en', 'conjugation_next'))
        following.message.fail_once = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic send failure'):
            self.call(preparation.conjugation_input, following)
        current = copy.deepcopy(self.frame())
        before = self.f.block()
        with patch.object(self.f.service, 'transition', side_effect=AssertionError('second reset')), \
             patch.object(self.f.service, 'preparation', side_effect=AssertionError('picker')):
            self.assertEqual(self.call(preparation.conjugation_card_callback, old_reset), CONJUGATION_MENU)
        self.assertEqual(self.frame(), current)
        self.assertEqual(old_reset.message.sent, [])
        self.assertEqual(self.f.block(), before)
        self.assertEqual(self.call(preparation.conjugation_input, following), CONJUGATION_MENU)
        self.assertFalse(self.frame()['pending_render'])

    def test_restart_without_live_cache_ignores_old_reset_but_ledger_keeps_it_applied(self):
        update = self.reset_with_failed_send()
        before = self.f.block()
        self.ctx.user_data.clear()
        with patch.object(self.f.service, 'transition', side_effect=AssertionError('second reset')):
            self.assertIsNone(self.call(preparation.conjugation_card_callback, update))
        self.assertEqual(self.ctx.user_data, {})
        self.assertEqual(self.f.block(), before)
        # The live cache is only presentation recovery. SQL also prevents the
        # old delivery from becoming another reset if reached through a target.
        replay = self.f.service.transition(7, 'ru_de', 'a', 'praesens', before['revision'],
            before['cycle'], 'reset', update.update_id, 'new-flow', today=TODAY, now=NOW)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.f.block(), before)

    def test_reclassified_active_word_is_skipped_without_answering_it(self):
        self.start_due(two=True)
        self.f.conn.execute("UPDATE words SET is_separable=1 WHERE id='a'"); self.f.conn.commit()
        self.answer('ignored')
        self.assertEqual(self.answers(), 0)
        self.assertEqual(self.frame()['question']['word_id'], 'b')

    def pending_empty_undo(self):
        self.f.conn.execute("""INSERT INTO study_deliveries
            (user_id,transport_update_id,flow_id,action_kind,state,result_json)
            VALUES(7,999999,'undo','undo','processing',?)""",
            (json.dumps({'tx_ids': [], 'cursor': 0, 'outcomes': []}),))
        self.f.conn.commit()
        self.ctx.user_data['quiz_mode'] = 'stale'

    def test_pending_undo_recovery_exits_dice_without_ui_effect(self):
        self.f.add(); self.call(preparation.start_conjugation)
        self.pending_empty_undo()
        self.assertEqual(self.call(preparation.conjugation_input,
                                   self.update(t('en', 'conjugation_dice'))), TESTS_MENU)
        self.assertNotIn(preparation.FRAME, self.ctx.user_data)
        self.assertNotIn('quiz_mode', self.ctx.user_data)
        self.assertNotIn('conjugation_dice_recent', self.ctx.user_data)

    def test_pending_undo_recovery_exits_test_without_grading(self):
        self.start_due(); self.pending_empty_undo()
        self.assertEqual(self.answer('ignored'), MENU)
        self.assertEqual(self.answers(), 0)
        self.assertNotIn(preparation.FRAME, self.ctx.user_data)
        self.assertNotIn('quiz_mode', self.ctx.user_data)


if __name__ == '__main__':
    unittest.main()
