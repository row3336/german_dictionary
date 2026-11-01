"""Process-local written frames. Nothing here resumes a quiz after restart."""

import uuid


def close(context):
    context.user_data.pop("written_frame", None)
    context.user_data.pop("written_last_original", None)
    context.user_data.pop("written_published_actions", None)
    context.user_data.pop("written_correction_status", None)


def prepare(context, user_id, mode, entry, text, kwargs, retry=False, keep=False):
    old = context.user_data.get("written_frame") or {}
    if keep and old.get("word_id") == (entry or {}).get("id") and not old.get("consumed"):
        frame = dict(old)
    else:
        frame = {"user_id": int(user_id), "mode": mode,
                 "flow_id": old.get("flow_id") or uuid.uuid4().hex,
                 "revision": int(old.get("revision", 0)) + 1,
                 "question_token": uuid.uuid4().hex,
                 "word_id": (entry or {}).get("id"), "retry": bool(retry)}
    frame.update(text=text, kwargs=kwargs, send_state="planned",
                 last_original_key=context.user_data.get("written_last_original"))
    context.user_data["written_frame"] = frame
    return frame


async def deliver(update, context, send):
    frame = context.user_data.get("written_frame")
    if not frame:
        return
    identity = (frame["flow_id"], frame["question_token"])
    try:
        sent = await send(update.message, frame["text"], **frame["kwargs"])
    except Exception:
        current = context.user_data.get("written_frame")
        if current and (current["flow_id"], current["question_token"]) == identity:
            current["send_state"] = "failed"
        raise
    current = context.user_data.get("written_frame")
    if current and (current["flow_id"], current["question_token"]) == identity:
        message_id = getattr(sent, "message_id", None) if sent is not None else None
        current["send_state"] = "confirmed" if message_id is not None else "uncertain"
        current["telegram_message_id"] = message_id
