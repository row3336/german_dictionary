"""Real ConversationHandler dispatch for the shared notification entry hooks."""

import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests.test_new_logic_core_conversation import synthetic_dispatch_environment


class NotificationDispatchTest(unittest.TestCase):
    def test_idle_menu_and_active_learning_dispatch_to_settings_and_time(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "tests/test_new_logic_core_conversation.py").read_text())
        source = next(node.value.args[0].value for node in ast.walk(tree)
                      if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "script"
                      for target in node.targets) and isinstance(node.value, ast.Call))
        script = textwrap.dedent(source)
        extra = textwrap.dedent('''
            from bot.notifications import handlers as notifications
            from bot.notifications.settings import ReminderPreference
            stack.enter_context(patch.object(main, 'access_allowed', return_value=True))
            control = stack.enter_context(patch.object(main, 'close_study_flow',
                return_value={'flow_id': 'notifications'}))
            stack.enter_context(patch.object(notifications, 'access_allowed', return_value=True))
            stack.enter_context(patch.object(notifications, '_user_interface_lang', return_value='en'))
            stack.enter_context(patch.object(notifications, 'get_preference',
                return_value=ReminderPreference(False, '14:00')))
            save = stack.enter_context(patch.object(notifications, 'set_preference'))
            for index, old_state in enumerate((None, main.MENU, main.LEARNING_QUIZ)):
                if old_state is None:
                    conv._conversations.pop(key, None)
                else:
                    conv._conversations[key] = old_state
                app.user_data[UID]['learning_frame'] = {'flow_id': 'old-learning'}
                app.user_data[UID]['quiz_entry'] = {'id': 'old-question'}
                query = CallbackQuery('notify-query-'+str(index), User(UID, 'Learner', False),
                    'synthetic-chat-instance', message=message(bot, 'reminder', 20+index), data='notify:new_words')
                query.set_bot(bot)
                opening = Update(2000+index*10, callback_query=query)
                opening.set_bot(bot)
                await app.process_update(opening)
                assert not errors, errors
                assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU
                assert 'learning_frame' not in app.user_data[UID]
                assert 'quiz_entry' not in app.user_data[UID]
                time_entry = Update(2001+index*10,
                    message=message(bot, t('en', 'notification_change_time'), 30+index))
                time_entry.set_bot(bot)
                await app.process_update(time_entry)
                assert not errors, errors
                assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_TIME
                time_value = Update(2002+index*10, message=message(bot, '15:30', 40+index))
                time_value.set_bot(bot)
                await app.process_update(time_value)
                assert not errors, errors
                assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU
            assert control.call_count == 3
            assert save.call_count == 3
            save.assert_called_with(main.get_db_path(), UID, 'new_words', time='15:30')
            control.return_value = {'replayed': True, 'flow_id': 'notifications'}
            conv._conversations[key] = main.LEARNING_QUIZ
            app.user_data[UID]['learning_frame'] = {'flow_id': 'new-learning'}
            await app.process_update(opening)
            assert not errors, errors
            assert conv._conversations[key] == main.LEARNING_QUIZ
            assert app.user_data[UID]['learning_frame']['flow_id'] == 'new-learning'
            from telegram.error import TimedOut
            async def fail_settings_send(*args, **kwargs):
                raise TimedOut('synthetic settings presentation')
            control.return_value = {'flow_id': 'notifications'}
            conv._conversations[key] = main.LEARNING_QUIZ
            app.user_data[UID]['learning_frame'] = {'flow_id': 'prior-learning'}
            query = CallbackQuery('notify-failure', User(UID, 'Learner', False), 'synthetic',
                message=message(bot, 'reminder', 80), data='notify:new_words')
            query.set_bot(bot)
            failed_opening = Update(2400, callback_query=query)
            failed_opening.set_bot(bot)
            with patch.object(FakeBot, 'send_message', fail_settings_send):
                await app.process_update(failed_opening)
            assert len(errors) == 1 and isinstance(errors[0], TimedOut), errors
            errors.clear()
            assert conv._conversations[key] == main.LEARNING_QUIZ
            assert 'learning_frame' not in app.user_data[UID]
            assert app.user_data[UID]['quiz_notification_pending']['transport'] == 2400
            control.return_value = {'replayed': True, 'flow_id': 'notifications'}
            await app.process_update(failed_opening)
            assert not errors, errors
            assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU
            assert 'quiz_notification_pending' not in app.user_data[UID]
            # Even an unfinished opening loses its presentation right when a
            # newer study frame appears or navigation invalidates its marker.
            app.user_data[UID]['quiz_notification_pending'] = {'transport': 2400}
            app.user_data[UID]['learning_frame'] = {'flow_id': 'newer-learning'}
            conv._conversations[key] = main.LEARNING_QUIZ
            sent = len(bot.sent)
            await app.process_update(failed_opening)
            assert conv._conversations[key] == main.LEARNING_QUIZ
            assert app.user_data[UID]['learning_frame']['flow_id'] == 'newer-learning'
            assert len(bot.sent) == sent
            main.invalidate_study_frames(type('Context', (), {'user_data': app.user_data[UID]})())
            conv._conversations[key] = main.MENU
            await app.process_update(failed_opening)
            assert conv._conversations[key] == main.MENU
            assert len(bot.sent) == sent
            assert NoNetworkRequest.attempts == 0
        ''')
        needle = "        await app.shutdown()"
        self.assertIn(needle, script)
        script = script.replace(needle, textwrap.indent(extra, "        ") + "\n" + needle)
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=root,
                                env=synthetic_dispatch_environment(),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("network=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
