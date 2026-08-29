"""Sprint 11: learning input_err preserves the already shown next card."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from _telegram_stub import install

install()

from bot.handlers import learning as L  # noqa: E402


USER_ID = 111
A = {"id": 1, "learning_step": 2, "german": "Apfel"}
B = {"id": 2, "learning_step": 0, "german": "Birne"}
C = {"id": 3, "learning_step": 0, "german": "Citron"}


class _Message:
    def __init__(self, text):
        self.text = text
        self.message_id = 17
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return types.SimpleNamespace(message_id=1017)


class _Update:
    def __init__(self, text):
        self.message = _Message(text)
        self.effective_user = types.SimpleNamespace(id=USER_ID)
        self.effective_chat = types.SimpleNamespace(id=USER_ID)


class _Context:
    def __init__(self):
        self.user_data = {"quiz_entry": A, "learning_preview_mode": False}


def _run(coro):
    return asyncio.run(coro)


class LearningInputErrPreservesNextCardTest(unittest.TestCase):
    def test_wrong_a_then_input_err_keeps_b_without_reselecting_or_updating_it(self):
        update = _Update("wrong answer")
        context = _Context()

        def action_key(text, _user_id):
            return "input_err" if text == "input_err" else None

        with (
            patch.object(L, "_action_key_from_text", side_effect=action_key),
            patch.object(L, "_check_ru_de_strict", return_value=(False, None)),
            patch.object(L, "_user_interface_lang", return_value="en"),
            patch.object(L, "_user_dict_lang", return_value="russian"),
            patch.object(L, "get_db_path", return_value="test.db"),
            patch.object(L, "_learning_schedule_seconds", return_value=[0, 0, 0, 60]),
            patch.object(L, "_capture_transaction_snapshot", return_value={}),
            patch.object(L, "_log_transaction"),
            patch.object(L, "log_quiz_event") as log_event,
            patch.object(L, "learning_update_result", return_value={"completed": False}) as update_result,
            patch.object(L, "learning_apply_input_err", return_value={"completed": False}) as apply_correction,
            patch.object(L, "learning_preview_entry", side_effect=[None, C, C]) as preview_selector,
            patch.object(L, "_pick_learning_due_entry", return_value=B) as due_selector,
            patch.object(L, "resolve_bot_word_media", side_effect=lambda _db, _uid, entry: entry),
            patch.object(L, "_format_feedback_ru_de", return_value="wrong A"),
            patch.object(L, "_format_ru_question", side_effect=lambda entry, _lang: f"question {entry['id']}"),
            patch.object(L, "_set_reaction", new_callable=AsyncMock) as set_reaction,
        ):
            _run(L.learning_quiz(update, context))
            self.assertIs(context.user_data["quiz_entry"], B)
            update_result.assert_called_once()
            self.assertEqual(update_result.call_args.kwargs["word_id"], A["id"])
            self.assertEqual(due_selector.call_count, 1)

            update.message.text = "input_err"
            update.message.calls.clear()
            _run(L.learning_quiz(update, context))

            self.assertIs(context.user_data["quiz_entry"], B)
            apply_correction.assert_called_once()
            self.assertEqual(apply_correction.call_args.kwargs["word_id"], A["id"])
            self.assertEqual(update_result.call_count, 1)
            self.assertEqual(due_selector.call_count, 1)
            self.assertEqual(preview_selector.call_count, 1)
            self.assertIn("question 2", update.message.calls[-1][0])

            update.message.calls.clear()
            _run(L.learning_quiz(update, context))

            self.assertIs(context.user_data["quiz_entry"], B)
            apply_correction.assert_called_once()
            self.assertEqual(update_result.call_count, 1)
            self.assertEqual(due_selector.call_count, 1)
            self.assertEqual(preview_selector.call_count, 1)
            self.assertIn(L.t("en", "input_err_nothing"), update.message.calls[-1][0])
            self.assertIn("question 2", update.message.calls[-1][0])
            self.assertEqual(set_reaction.await_count, 2)

        event_word_ids = [call.kwargs["word_id"] for call in log_event.call_args_list]
        self.assertEqual(event_word_ids, [A["id"], A["id"]])


if __name__ == "__main__":
    unittest.main()
