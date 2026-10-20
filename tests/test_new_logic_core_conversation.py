"""Dispatch regression with real PTB, isolated from the suite's Telegram stubs."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class ConjugationConversationTest(unittest.TestCase):
    def test_real_dispatch_reset_changes_state_and_next_uses_preparation(self):
        # Other tracked tests install a deliberately minimal PTB stub globally.
        # A fresh interpreter tests the installed dispatcher without changing it.
        script = textwrap.dedent(r'''
            import asyncio
            from contextlib import ExitStack
            from datetime import datetime, timezone
            from types import SimpleNamespace
            from unittest.mock import patch

            from telegram import CallbackQuery, Chat, Message, Update, User
            from telegram.ext import Application, CallbackQueryHandler, ConversationHandler, ExtBot, MessageHandler
            from telegram.request import BaseRequest

            import bot.main as main
            from bot.handlers import conjugation as conj
            from bot.i18n import t

            UID = 7
            NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

            class NoNetworkRequest(BaseRequest):
                attempts = 0

                @property
                def read_timeout(self):
                    return None

                async def initialize(self):
                    pass

                async def shutdown(self):
                    pass

                async def do_request(self, *args, **kwargs):
                    type(self).attempts += 1
                    raise AssertionError('network request attempted')

            class FakeBot(ExtBot):
                __slots__ = ('sent', 'answered')

                def __init__(self):
                    super().__init__('123456:synthetic', request=NoNetworkRequest(),
                                     get_updates_request=NoNetworkRequest())
                    object.__setattr__(self, 'sent', [])
                    object.__setattr__(self, 'answered', [])

                async def initialize(self):
                    self._bot_user = User(99, 'Synthetic bot', True, username='synthetic_bot')

                async def shutdown(self):
                    pass

                async def send_message(self, chat_id, text, **kwargs):
                    self.sent.append(text)
                    message = Message(100 + len(self.sent), NOW, Chat(chat_id, 'private'),
                                      from_user=self._bot_user, text=text)
                    message.set_bot(self)
                    return message

                async def answer_callback_query(self, callback_query_id, **kwargs):
                    self.answered.append(callback_query_id)
                    return True

                async def edit_message_reply_markup(self, **kwargs):
                    return True

            class RecordingBuilder:
                def __init__(self, app):
                    self.app = app
                    self.calls = []

                def __getattr__(self, name):
                    def configure(*args, **kwargs):
                        self.calls.append(name)
                        return self
                    return configure

                def build(self):
                    self.calls.append('build')
                    return self.app

            class FakeService:
                def __init__(self):
                    self.transitions = []
                    self.next_calls = []

                def view(self):
                    return {'preparation': {'entry': {
                        'id': 'a', 'type': 'verb', 'german': 'gehen', 'infinitive': 'gehen',
                        'russian': 'идти', 'conjugation_praesens':
                        'ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen',
                        'conjugation_praeteritum': ''}, 'blocks': [{
                        'word_id': 'a', 'tense': 'praesens', 'phase': 'preparation',
                        'cycle': 2, 'revision': 4}]}, 'scheduled': []}

                def transition(self, *args, **kwargs):
                    self.transitions.append(args)
                    assert args[6] == 'reset'
                    return self.view()

                def preparation(self, *args, **kwargs):
                    self.next_calls.append((args, kwargs))
                    return self.view()

            def message(bot, text, ident):
                result = Message(ident, NOW, Chat(UID, 'private'),
                                 from_user=User(UID, 'Learner', False), text=text)
                result.set_bot(bot)
                return result

            async def scenario():
                bot = FakeBot()
                app = Application.builder().bot(bot).job_queue(None).build()
                builder = RecordingBuilder(app)
                service = FakeService()
                errors = []
                with ExitStack() as stack:
                    stack.enter_context(patch.dict('os.environ', {'BOT_TOKEN': '123456:synthetic'}))
                    for name in ('load_dotenv', '_configure_logging', 'require_supported_ptb',
                                 '_prepare_storage', '_configure_learning_reminders',
                                 '_configure_fsrs_reports', '_configure_access_reminders',
                                 '_configure_owner_digest'):
                        stack.enter_context(patch.object(main, name))
                    stack.enter_context(patch.object(main, 'Application',
                                                    SimpleNamespace(builder=lambda: builder)))
                    stack.enter_context(patch.object(main, 'AIORateLimiter', return_value=object()))
                    polling = stack.enter_context(patch.object(main, 'run_polling_safely'))
                    stack.enter_context(patch.object(conj, '_service', return_value=service))
                    stack.enter_context(patch.object(conj, '_mode_l1_de', return_value='ru_de'))
                    stack.enter_context(patch.object(conj, '_user_interface_lang', return_value='ru'))
                    stack.enter_context(patch.object(conj, '_user_dict_lang', return_value='ru'))
                    stack.enter_context(patch.object(conj, '_action_key_from_text',
                                                    return_value='conjugation_next'))

                    main.main()
                    polling.assert_called_once_with(app, main.logger)
                    assert builder.calls[-1] == 'build'
                    assert builder.calls.count('build') == 1
                    assert NoNetworkRequest.attempts == 0
                    conv = app.handlers[0][0]
                    assert isinstance(conv, ConversationHandler)
                    for state, text_handler in ((main.CONJUGATION_MENU, main.conjugation_input),
                                                (main.CONJUGATION_TEST, main.conjugation_test_input)):
                        handlers = conv.states[state]
                        assert isinstance(handlers[0], CallbackQueryHandler)
                        assert handlers[0].callback is main.conjugation_card_callback
                        assert handlers[0].pattern.pattern == r'^conj:'
                        assert isinstance(handlers[1], MessageHandler)
                        assert handlers[1].callback is text_handler
                    assert any(isinstance(handler, CallbackQueryHandler)
                               and handler.callback is main.conjugation_card_callback
                               for handler in app.handlers[0][1:])

                    async def capture_error(update, context):
                        errors.append(context.error)

                    app.error_handlers.clear()
                    app.add_error_handler(capture_error)
                    await app.initialize()
                    key = (UID, UID)
                    conv._conversations[key] = main.CONJUGATION_TEST
                    frame = conj._new_frame('ru_de', 'test')
                    frame['callbacks']['reset-token'] = {
                        'word_id': 'a', 'tense': 'praesens', 'revision': 3,
                        'cycle': 1, 'operation': 'reset'}
                    app.user_data[UID][conj.FRAME] = frame
                    query = CallbackQuery('reset-query', User(UID, 'Learner', False),
                                          'synthetic-chat-instance', message=message(bot, 'test', 1),
                                          data='conj:reset:reset-token')
                    query.set_bot(bot)
                    reset = Update(1000, callback_query=query)
                    reset.set_bot(bot)
                    await app.process_update(reset)
                    assert not errors, errors
                    # A direct callback return would not prove this transition.
                    assert conv._conversations[key] == main.CONJUGATION_MENU
                    assert app.user_data[UID][conj.FRAME]['kind'] == 'preparation'
                    assert len(service.transitions) == 1
                    assert bot.answered == ['reset-query']

                    following = Update(1001, message=message(bot, t('ru', 'conjugation_next'), 2))
                    following.set_bot(bot)
                    await app.process_update(following)
                    assert not errors, errors
                    assert conv._conversations[key] == main.CONJUGATION_MENU
                    assert len(service.next_calls) == 1
                    assert app.user_data[UID]['conjugation_session_count'] == 1
                    assert app.user_data[UID][conj.FRAME]['entry']['id'] == 'a'
                    assert NoNetworkRequest.attempts == 0
                    await app.shutdown()
                print('real ConversationHandler: 50 -> reset -> preparation -> next PASS; network=0')

            asyncio.run(scenario())
        ''')
        result = subprocess.run([sys.executable, "-B", "-c", script],
                                cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("network=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
