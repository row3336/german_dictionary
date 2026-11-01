"""Real confirmed written questions for adapter regression fixtures."""

from bot.written.service import apply_original_answer


def confirmed_frame(user_id, word_id, mode="ru_de", token="test-question", retry=False):
    return {"user_id": int(user_id), "word_id": word_id, "mode": mode,
            "flow_id": "fixture-flow", "question_token": token, "revision": 1,
            "retry": retry, "send_state": "confirmed"}


def remember_wrong(db_path, user_id, context, word_id, mode="ru_de"):
    original = confirmed_frame(user_id, word_id, mode)
    receipt = apply_original_answer(db_path, user_id, word_id, mode, "wrong", original, -100)["receipt"]
    current = confirmed_frame(user_id, word_id, mode, token="next-question", retry=True)
    current["last_original_key"] = receipt["action_key"]
    context.user_data["written_frame"] = current
    context.user_data["written_last_original"] = receipt["action_key"]
    return context
