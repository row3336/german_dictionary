"""Learning mode: the learning menu, the preview/quiz flow, the topic-mix due
queue, batch top-up, input-err correction, and the helper that pushes a quiz
entry into the learning batch (used by quiz / clarify_fields / db_search).
"""

import random
from datetime import datetime, timezone

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    AI_CLARIFY_FIELD,
    INPUT_ERR_TEXT,
    I_DONT_KNOW_TEXT,
    LEARNING_MENU,
    LEARNING_QUIZ,
    TESTS_MENU,
    get_db_path,
    is_owner,
)
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _reply_text_with_retry
from ..common.settings import (
    _learning_daily_qty,
    _learning_intervals_text,
    _learning_mix_mode_enabled,
    _learning_ready_quorum,
    _learning_schedule_seconds,
    _set_learning_mix_mode,
    _topic_filter_banner,
    _topic_filter_for_query,
)
from ..common.txlog import _capture_transaction_snapshot, _log_transaction
from ..handlers.quiz import _check_ru_de_strict
from ..i18n import t
from ..labels import LEARNING_MIX_MODE_TEXT, LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT, LEARNING_TEXT
from ..storage import (
    learning_active_count,
    learning_add_word_by_id,
    learning_add_words,
    learning_apply_input_err,
    learning_due_entries,
    learning_due_entry,
    learning_get_or_create_batch,
    learning_mark_preview_next,
    learning_pick_new_words,
    learning_preview_entry,
    learning_reset_session,
    learning_skip_word,
    learning_status_snapshot,
    learning_update_result,
    list_learning_active_words,
    log_quiz_event,
)
from ..text_utils import cap_word as _cap_word
from ..text_utils import low_word as _low_word
from ..ui.formatting import (
    _entry_translation,
    _format_feedback_ru_de,
    _format_learning_preview_prompt,
    _format_ru_question,
    _format_topic_learning_line,
)
from ..ui.keyboards import _learning_menu_keys, _tests_menu_keys


def _format_eta_from_iso(next_due_iso):
    """Handle format eta from iso."""
    token = str(next_due_iso or "").strip()
    if not token:
        return "-"
    try:
        when = datetime.strptime(token, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return token
    now = datetime.now(timezone.utc)
    delta = int((when - now).total_seconds())
    if delta <= 0:
        return "now"
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _build_learning_topic_mix_queue(entries):
    """Build weighted mixed queue by topic."""
    buckets = {}
    for entry in entries:
        topic = str(entry.get("topic") or "").strip() or "-"
        buckets.setdefault(topic, []).append(entry)
    for values in buckets.values():
        random.shuffle(values)
    mixed = []
    last_topic = None
    while True:
        candidates = [(topic, values) for topic, values in buckets.items() if values]
        if not candidates:
            break
        topic_pool = candidates
        if last_topic and len(candidates) > 1:
            alt = [(topic, values) for topic, values in candidates if topic != last_topic]
            if alt:
                topic_pool = alt
        topics = [topic for topic, _ in topic_pool]
        weights = [len(values) for _, values in topic_pool]
        selected_topic = random.choices(topics, weights=weights, k=1)[0]
        mixed.append(buckets[selected_topic].pop(0))
        last_topic = selected_topic
    return mixed


def _pick_learning_due_entry(context, user_id, exclude_word_id=None):
    """Pick next due learning entry with optional topic-mix mode."""
    if not _learning_mix_mode_enabled(user_id):
        return learning_due_entry(get_db_path(), user_id=user_id, exclude_word_id=exclude_word_id)
    queue_key = "learning_mix_queue"
    queue = context.user_data.get(queue_key)
    if not isinstance(queue, list) or not queue:
        due_entries = learning_due_entries(get_db_path(), user_id=user_id, exclude_word_id=exclude_word_id)
        if not due_entries and exclude_word_id:
            due_entries = learning_due_entries(get_db_path(), user_id=user_id, exclude_word_id=None)
        queue = _build_learning_topic_mix_queue(due_entries)
        context.user_data[queue_key] = queue
    while queue:
        entry = queue.pop(0)
        wid = entry.get("id")
        if exclude_word_id and wid == exclude_word_id:
            continue
        if not wid:
            continue
        return entry
    context.user_data.pop(queue_key, None)
    return None


def _send_entry_to_learning(update: Update, context: ContextTypes.DEFAULT_TYPE, entry_key="quiz_entry"):
    """Handle send entry to learning."""
    entry = context.user_data.get(entry_key)
    if not entry:
        return False
    db_path = get_db_path()
    user_id = update.effective_user.id
    batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=_learning_daily_qty(db_path))
    added = learning_add_word_by_id(
        db_path,
        user_id=user_id,
        word_id=entry.get("id"),
        batch_id=batch_id,
        first_delay_seconds=_learning_schedule_seconds(db_path)[0],
    )
    return bool(added)


def _apply_input_err_learning(update, context):
    """Handle apply input error for learning mode."""
    last_id = context.user_data.get("last_learning_entry_id")
    last_ok = context.user_data.get("last_learning_ok")
    last_prev_step = context.user_data.get("last_learning_prev_step")
    if not last_id or last_ok is None:
        return False
    if last_ok:
        return False
    snapshot = _capture_transaction_snapshot(update.effective_user.id, last_id, ["learning"])
    result = learning_apply_input_err(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_id,
        prev_step_index=last_prev_step,
        intervals_seconds=_learning_schedule_seconds(get_db_path()),
    )
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_id,
        mode="learning",
        is_correct=True,
    )
    _log_transaction(update.effective_user.id, "input_err:learning", snapshot)
    context.user_data["last_learning_ok"] = True
    return bool(result)


async def _send_learning_menu(update: Update, user_id):
    """Handle send learning menu."""
    stats = learning_status_snapshot(
        get_db_path(),
        user_id=user_id,
        quorum_pct=_learning_ready_quorum(get_db_path()),
    )
    active_entries = list_learning_active_words(get_db_path(), user_id=user_id)
    active_lines = [_format_topic_learning_line(item) for item in active_entries]
    active_preview = active_lines[:30]
    if len(active_lines) > 30:
        active_preview.append(f"... +{len(active_lines) - 30} more")
    msg = "\n".join(
        [
            f"{LEARNING_TEXT}:",
            "",
            _topic_filter_banner(),
            "",
            f"{t(_user_interface_lang(user_id), 'learning_daily_qty')} = {_learning_daily_qty(get_db_path())}",
            f"{t(_user_interface_lang(user_id), 'learning_intervals')} = {_learning_intervals_text(get_db_path())}",
            f"{LEARNING_MIX_MODE_TEXT} = {'ON' if _learning_mix_mode_enabled(user_id) else 'OFF'}",
            f"active = {stats.get('active_count', 0)}",
            f"preview_pending = {stats.get('preview_pending', 0)}",
            f"due_now = {stats.get('due_count', 0)} / need {stats.get('ready_needed', 0)}",
            f"completed_in_batch = {stats.get('completed_count', 0)}",
            f"next_due = {_format_eta_from_iso(stats.get('next_due'))}",
            "",
            "active words:",
            *(active_preview if active_preview else ["-"]),
        ]
    )
    await _reply_text_with_retry(
        update.message,
        msg,
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


def _format_learning_preview_line(entry, dict_lang):
    """Handle format learning preview line."""
    word_type = (entry.get("type") or "").strip().lower()
    if word_type == "verb":
        left = _low_word(entry.get("infinitive", "")).strip()
    else:
        article = (entry.get("article") or "").strip()
        left = f"{article} {_cap_word(entry.get('german', ''))}".strip()
    right = _entry_translation(entry, dict_lang)
    return f"- {left} = {right}"


async def _learning_add_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning add words."""
    user_id = update.effective_user.id
    db_path = get_db_path()
    daily_qty = _learning_daily_qty(db_path)
    batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=daily_qty)
    active_count = learning_active_count(db_path, user_id=user_id, batch_id=batch_id)
    need = max(0, daily_qty - active_count)
    if need <= 0:
        await update.message.reply_text(
            f"Learning batch is full: {active_count}/{daily_qty}.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    picked = learning_pick_new_words(db_path, user_id=user_id, limit=need, topics=_topic_filter_for_query())
    if not picked:
        await update.message.reply_text(
            "No new words available for learning.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    first_delay = _learning_schedule_seconds(db_path)[0]
    added = learning_add_words(
        db_path,
        user_id=user_id,
        words=picked,
        batch_id=batch_id,
        first_delay_seconds=first_delay,
    )
    await _reply_text_with_retry(
        update.message,
        f"Added for learning: {added}\nStart learning session for first cycle preview.",
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def _send_learning_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Handle send learning prompt."""
    user_id = update.effective_user.id
    preview_entry = learning_preview_entry(get_db_path(), user_id=user_id)
    if preview_entry:
        context.user_data["quiz_entry"] = preview_entry
        context.user_data["learning_preview_mode"] = True
        prompt_text = _format_learning_preview_prompt(preview_entry, _user_dict_lang(user_id))
        if prefix_text:
            prompt_text = f"{prefix_text}\n\n{prompt_text}"
        await _reply_text_with_retry(
            update.message,
            prompt_text,
            reply_markup=ReplyKeyboardMarkup(
                [[LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT], [AI_CLARIFY_FIELD, t(_user_interface_lang(user_id), "back")]],
                resize_keyboard=True,
            ),
            parse_mode="Markdown",
        )
        return LEARNING_QUIZ

    last_learning_word_id = context.user_data.get("last_learning_prompt_word_id")
    entry = force_entry
    if not entry:
        entry = _pick_learning_due_entry(context, user_id, exclude_word_id=last_learning_word_id)
    if not entry:
        context.user_data["learning_preview_mode"] = False
        context.user_data.pop("last_learning_prompt_word_id", None)
        context.user_data.pop("learning_mix_queue", None)
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            f"No due learning words right now.\nnext_due = {_format_eta_from_iso(learning_status_snapshot(get_db_path(), user_id, _learning_ready_quorum(get_db_path())).get('next_due'))}",
            reply_markup=ReplyKeyboardMarkup(
                [[INPUT_ERR_TEXT], [t(_user_interface_lang(user_id), "back")]],
                resize_keyboard=True,
            ),
        )
        return LEARNING_QUIZ
    context.user_data["quiz_entry"] = entry
    context.user_data["learning_preview_mode"] = False
    context.user_data["last_learning_prompt_word_id"] = entry.get("id")
    prompt_text = _format_ru_question(entry, _user_dict_lang(user_id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=ReplyKeyboardMarkup(
            [[INPUT_ERR_TEXT, I_DONT_KNOW_TEXT], [LEARNING_SKIP_TEXT, AI_CLARIFY_FIELD, t(_user_interface_lang(user_id), "back")]],
            resize_keyboard=True,
        ),
        parse_mode="Markdown",
    )
    return LEARNING_QUIZ


async def learning_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU
    if action == "learning_add":
        return await _learning_add_words(update, context)
    if action == "learning_mix_mode":
        enabled = _learning_mix_mode_enabled(user_id)
        _set_learning_mix_mode(user_id, not enabled)
        context.user_data.pop("learning_mix_queue", None)
        await _reply_text_with_retry(update.message, f"{LEARNING_MIX_MODE_TEXT} = {'ON' if not enabled else 'OFF'}")
        return await _send_learning_menu(update, user_id)
    if action == "learning_reset":
        learning_reset_session(get_db_path(), user_id=user_id)
        context.user_data.pop("learning_mix_queue", None)
        await _reply_text_with_retry(
            update.message,
            "Learning session reset. Words returned to new.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    if action == "learning_start":
        context.user_data.pop("learning_mix_queue", None)
        return await _send_learning_prompt(update, context, None)
    await _reply_text_with_retry(
        update.message,
        "Use learning menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def learning_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning quiz."""
    from ..handlers.clarify_fields import _start_clarify_fields

    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        return await _send_learning_menu(update, user_id)
    if text == AI_CLARIFY_FIELD and is_owner(user_id):
        return await _start_clarify_fields(update, context, LEARNING_QUIZ, prefer_current=True)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_learning_prompt(update, context, None)

    is_preview = bool(context.user_data.get("learning_preview_mode"))
    if is_preview:
        if action == "learning_skip" or text == LEARNING_SKIP_TEXT:
            snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
            learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
            _log_transaction(user_id, "learning:skip", snapshot)
            return await _send_learning_prompt(update, context, "Skipped for learning.")
        if action == "learning_next" or text == LEARNING_NEXT_TEXT:
            snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
            learning_mark_preview_next(
                get_db_path(),
                user_id=user_id,
                word_id=entry.get("id"),
                first_interval_seconds=_learning_schedule_seconds(get_db_path())[0],
            )
            _log_transaction(user_id, "learning:preview_next", snapshot)
            return await _send_learning_prompt(update, context, "Saved. Next preview word.")
        await _reply_text_with_retry(
            update.message,
            "Use next/skip/back.",
            reply_markup=ReplyKeyboardMarkup([[LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return LEARNING_QUIZ

    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_learning(update, context)
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        context.user_data.pop("learning_mix_queue", None)
        return await _send_learning_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))

    if action == "learning_skip" or text == LEARNING_SKIP_TEXT:
        snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
        learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
        _log_transaction(user_id, "learning:skip", snapshot)
        context.user_data.pop("learning_mix_queue", None)
        return await _send_learning_prompt(update, context, "Skipped for learning.")

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok, _ = _check_ru_de_strict(entry, text)
    context.user_data["last_learning_entry_id"] = entry.get("id")
    context.user_data["last_learning_ok"] = bool(ok)
    context.user_data["last_learning_prev_step"] = int(entry.get("learning_step") or 0)
    log_quiz_event(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        mode="learning",
        is_correct=ok,
    )
    snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
    result = learning_update_result(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        ok=ok,
        intervals_seconds=_learning_schedule_seconds(get_db_path()),
    )
    _log_transaction(user_id, "learning:answer", snapshot)
    feedback = _format_feedback_ru_de(entry, ok)
    if result.get("completed"):
        feedback = f"{feedback}\n\nLearning done ✅, sent to fsrs"
    context.user_data.pop("learning_mix_queue", None)
    return await _send_learning_prompt(update, context, feedback)
