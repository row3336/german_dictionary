"""Learning mode: the learning menu, the preview/quiz flow, the static show
order (first round in preview order, later rounds random with spacing) plus the
separate topic-mix word selection, 👍/👎 answer reactions, the plain
end-of-queue service message, batch top-up, input-err correction, and the helper
that pushes a quiz entry into the learning batch (used by quiz / clarify_fields /
db_search).
"""

import random
from datetime import datetime, timezone

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    I_DONT_KNOW_TEXT,
    LEARNING_MENU,
    LEARNING_QUIZ,
    TESTS_MENU,
    get_db_path,
)
from ..common.bot_media import resolve_bot_word_media
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
)
from ..common.word_filter import count_words_for_filter, resolve_quiz_filter
from ..common.txlog import _capture_transaction_snapshot, _log_transaction
from ..handlers.quiz import _check_ru_de_strict, _set_reaction
from ..i18n import t
from ..storage import (
    learning_active_count,
    learning_add_word_by_id,
    learning_add_words,
    learning_apply_input_err,
    learning_due_entries,
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


def _learning_empty_text(user_id):
    """Service message when the running queue has nothing to show now.

    Distinguishes 'the selected topics hold no words at all' (spec #8) from
    'words exist but nothing is due yet' — in the latter case the next-due ETA is
    appended, but only when it is a real future time (skip the '-'/'now' filler).

    NOTE: this text is plain (it contains 'next_due', whose underscore would open
    an unterminated italic entity under parse_mode='Markdown'). It must never be
    sent with parse_mode='Markdown'.
    """
    if count_words_for_filter(user_id) == 0:
        return t(_user_interface_lang(user_id), "topic_no_words_learning")
    snapshot = learning_status_snapshot(get_db_path(), user_id, _learning_ready_quorum(get_db_path(), user_id))
    base = t(_user_interface_lang(user_id), "learning_no_due")
    eta = _format_eta_from_iso(snapshot.get("next_due"))
    if eta in ("-", "now"):
        return base
    # "next_due = <eta>" kept as EN technical readout (matches the menu diagnostic block)
    return f"{base}\nnext_due = {eta}"


# A word must not reappear within this many consecutive shows (>= once per three).
_LEARNING_RECENT_SHOWS = 2


def _pick_learning_due_entry(context, user_id):
    """Pick the next word to show in the running learning queue.

    STATIC show rule — identical whether or not mix mode is on (mix only shapes
    which words enter the queue, never their show order; see storage.py:
    learning_pick_new_words):
      - first round (freshly previewed, step_index == 0): strict preview order
        (words as they were reviewed, i.e. created_at ASC from the query);
      - later rounds (step_index > 0): random, but a word is not shown again
        within the last two shows — at most once per three consecutive shows.
    Recent shows are tracked in context ("learning_recent_ids"); when the pool is
    too small to honour the spacing, it relaxes as far as needed. An available
    alternative to the last shown word is selected before step priority is
    applied, so a wrong answer reset to step 0 cannot jump ahead of that
    alternative and immediately repeat.
    """
    due_entries = learning_due_entries(get_db_path(), user_id=user_id)
    if not due_entries:
        return None
    recent = context.user_data.get("learning_recent_ids") or []
    last_shown = recent[-1] if recent else None
    alternatives = [entry for entry in due_entries if entry.get("id") != last_shown]
    selection_entries = alternatives or due_entries

    first_round = [entry for entry in selection_entries if int(entry.get("learning_step") or 0) == 0]
    if first_round:
        # already ordered by created_at ASC (preview order) in the query
        return first_round[0]

    pool = [entry for entry in selection_entries if entry.get("id") not in recent]
    if not pool:
        pool = selection_entries
    return random.choice(pool)


def _note_learning_shown(context, word_id):
    """Record a shown word for the later-round spacing window (keep last N ids)."""
    if not word_id:
        return
    recent = context.user_data.get("learning_recent_ids") or []
    recent.append(word_id)
    context.user_data["learning_recent_ids"] = recent[-_LEARNING_RECENT_SHOWS:]


async def _apply_learning_reaction(update, context, ok):
    """React to the learning answer message: 👍 correct, 👎 wrong. Flat — no streak
    escalation — mirroring the FSRS quiz reaction contract. Best-effort. The
    message id is remembered so an input_err correction can lift the 👎 later."""
    context.user_data["last_learning_answer_message_id"] = update.message.message_id
    emoji = "\U0001F44D" if ok else "\U0001F44E"  # 👍 / 👎
    await _set_reaction(context, update.effective_user.id, update.message.message_id, [emoji])


def _send_entry_to_learning(update: Update, context: ContextTypes.DEFAULT_TYPE, entry_key="quiz_entry"):
    """Handle send entry to learning."""
    entry = context.user_data.get(entry_key)
    if not entry:
        return False
    db_path = get_db_path()
    user_id = update.effective_user.id
    batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=_learning_daily_qty(db_path, user_id))
    added = learning_add_word_by_id(
        db_path,
        user_id=user_id,
        word_id=entry.get("id"),
        batch_id=batch_id,
        first_delay_seconds=_learning_schedule_seconds(db_path, user_id)[0],
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
        intervals_seconds=_learning_schedule_seconds(get_db_path(), update.effective_user.id),
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
    """Show the learning menu with a summary of the current session."""
    stats = learning_status_snapshot(
        get_db_path(),
        user_id=user_id,
        quorum_pct=_learning_ready_quorum(get_db_path(), user_id),
    )
    active_entries = list_learning_active_words(get_db_path(), user_id=user_id)
    active_lines = [_format_topic_learning_line(item) for item in active_entries]
    active_preview = active_lines[:30]
    if len(active_lines) > 30:
        active_preview.append(f"... +{len(active_lines) - 30} more")
    msg = "\n".join(
        [
            f"{t(_user_interface_lang(user_id), 'learning')}:",
            "",
            _topic_filter_banner(user_id),
            "",
            f"{t(_user_interface_lang(user_id), 'learning_daily_qty')} = {_learning_daily_qty(get_db_path(), user_id)}",
            f"{t(_user_interface_lang(user_id), 'learning_intervals')} = {_learning_intervals_text(get_db_path(), user_id)}",
            f"{t(_user_interface_lang(user_id), 'learning_mix_mode')} = {'ON' if _learning_mix_mode_enabled(user_id) else 'OFF'}",
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
    daily_qty = _learning_daily_qty(db_path, user_id)
    batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=daily_qty)
    active_count = learning_active_count(db_path, user_id=user_id, batch_id=batch_id)
    need = max(0, daily_qty - active_count)
    if need <= 0:
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "learning_batch_full").format(active=active_count, daily=daily_qty),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    qf = resolve_quiz_filter(user_id)
    picked = learning_pick_new_words(
        db_path,
        user_id=user_id,
        limit=need,
        topics=qf.topics,
        cefr_levels=qf.cefr_levels,
        mix=_learning_mix_mode_enabled(user_id),
    )
    if not picked:
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "learning_no_new"),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    first_delay = _learning_schedule_seconds(db_path, user_id)[0]
    added = learning_add_words(
        db_path,
        user_id=user_id,
        words=picked,
        batch_id=batch_id,
        first_delay_seconds=first_delay,
    )
    await _reply_text_with_retry(
        update.message,
        t(_user_interface_lang(user_id), "learning_added").format(added=added),
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def _send_learning_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Show the next question/preview in the learning session."""
    user_id = update.effective_user.id
    preview_entry = None
    if force_entry is None:
        preview_entry = learning_preview_entry(get_db_path(), user_id=user_id)
    if preview_entry:
        preview_entry = resolve_bot_word_media(get_db_path(), user_id, preview_entry)
        context.user_data["quiz_entry"] = preview_entry
        context.user_data["learning_preview_mode"] = True
        prompt_text = _format_learning_preview_prompt(preview_entry, _user_dict_lang(user_id))
        if prefix_text:
            prompt_text = f"{prefix_text}\n\n{prompt_text}"
        await _reply_text_with_retry(
            update.message,
            prompt_text,
            reply_markup=ReplyKeyboardMarkup(
                [[t(_user_interface_lang(user_id), "learning_next"), t(_user_interface_lang(user_id), "learning_skip")], [t(_user_interface_lang(user_id), "clarify_fields"), t(_user_interface_lang(user_id), "back")]],
                resize_keyboard=True,
            ),
            parse_mode="Markdown",
        )
        return LEARNING_QUIZ

    entry = force_entry
    if entry is None:
        entry = _pick_learning_due_entry(context, user_id)
    if not entry:
        context.user_data["learning_preview_mode"] = False
        context.user_data.pop("quiz_entry", None)
        context.user_data.pop("learning_recent_ids", None)
        # Send the trailing feedback (Markdown) and the plain service tail as two
        # separate messages: the tail carries 'next_due', whose underscore would
        # break parse_mode='Markdown' (unterminated entity). Keep them apart so the
        # word feedback still renders while the tail stays plain and crash-proof.
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            _learning_empty_text(user_id),
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return LEARNING_QUIZ
    entry = resolve_bot_word_media(get_db_path(), user_id, entry)
    context.user_data["quiz_entry"] = entry
    context.user_data["learning_preview_mode"] = False
    _note_learning_shown(context, entry.get("id"))
    prompt_text = _format_ru_question(entry, _user_dict_lang(user_id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=ReplyKeyboardMarkup(
            [[I_DONT_KNOW_TEXT, t(_user_interface_lang(user_id), "input_err")], [t(_user_interface_lang(user_id), "learning_skip"), t(_user_interface_lang(user_id), "clarify_fields"), t(_user_interface_lang(user_id), "back")]],
            resize_keyboard=True,
        ),
        parse_mode="Markdown",
    )
    return LEARNING_QUIZ


async def learning_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choice in the learning menu (start/mix/add/reset/back)."""
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
    if action == "learning_sett":
        # Same submenu as algo settings; remember the entry point so its "back"
        # returns here instead of to algo settings.
        from .settings import _send_learning_settings_menu  # local import: anti-cycle

        context.user_data["learning_sett_return"] = "learning_menu"
        return await _send_learning_settings_menu(update, user_id)
    if action == "learning_add":
        return await _learning_add_words(update, context)
    if action == "learning_mix_mode":
        enabled = _learning_mix_mode_enabled(user_id)
        _set_learning_mix_mode(user_id, not enabled)
        # Mix only shapes which words enter the queue on the next add — toggling
        # it mid-session must not disturb the current show order/spacing state.
        await _reply_text_with_retry(update.message, f"{t(_user_interface_lang(user_id), 'learning_mix_mode')} = {'ON' if not enabled else 'OFF'}")
        return await _send_learning_menu(update, user_id)
    if action == "learning_reset":
        learning_reset_session(get_db_path(), user_id=user_id)
        context.user_data.pop("learning_recent_ids", None)
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "learning_session_reset"),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    if action == "learning_start":
        context.user_data.pop("learning_recent_ids", None)
        # Empty queue: nothing previewed and nothing due -> don't enter the session
        # (no lone Back button, no 🔥). Just show the service message and stay in the
        # menu, where the "add for learning" button is already in reach.
        has_preview = learning_preview_entry(get_db_path(), user_id=user_id) is not None
        has_due = bool(learning_due_entries(get_db_path(), user_id=user_id))
        if not has_preview and not has_due:
            await _reply_text_with_retry(
                update.message,
                _learning_empty_text(user_id),
                reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
            )
            return LEARNING_MENU
        return await _send_learning_prompt(update, context, None)
    await _reply_text_with_retry(
        update.message,
        t(_user_interface_lang(user_id), "learning_use_menu_buttons"),
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def learning_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer/action in the learning session (preview and normal mode)."""
    from ..handlers.clarify_fields import _start_clarify_fields

    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        return await _send_learning_menu(update, user_id)
    if action == "clarify_fields":
        return await _start_clarify_fields(update, context, LEARNING_QUIZ, prefer_current=True)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_learning_prompt(update, context, None)

    is_preview = bool(context.user_data.get("learning_preview_mode"))
    if is_preview:
        if action == "learning_skip":
            snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
            learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
            _log_transaction(user_id, "learning:skip", snapshot)
            return await _send_learning_prompt(update, context, t(_user_interface_lang(user_id), "learning_skipped"))
        if action == "learning_next":
            snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
            learning_mark_preview_next(
                get_db_path(),
                user_id=user_id,
                word_id=entry.get("id"),
                first_interval_seconds=_learning_schedule_seconds(get_db_path(), user_id)[0],
            )
            _log_transaction(user_id, "learning:preview_next", snapshot)
            return await _send_learning_prompt(update, context, t(_user_interface_lang(user_id), "learning_saved_next"))
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "learning_use_next_skip_back"),
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "learning_next"), t(_user_interface_lang(user_id), "learning_skip")], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return LEARNING_QUIZ

    if action == "input_err":
        next_entry = entry
        corrected_entry_id = context.user_data.get("last_learning_entry_id")
        applied = _apply_input_err_learning(update, context)
        lang = _user_interface_lang(user_id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        if applied:
            # lift the 👎 from the corrected answer message; no new reaction is set
            await _set_reaction(context, user_id, context.user_data.get("last_learning_answer_message_id"), [])
        # Preserve a separately selected next card. If the shown card is the one
        # just corrected, its embedded learning step is stale: re-read the queue
        # so a future step ends the cycle and a zero-delay warm-up is shown with
        # its current step instead of allowing an accidental double advance.
        same_entry = str(next_entry.get("id")) == str(corrected_entry_id)
        force_entry = None if same_entry else next_entry
        return await _send_learning_prompt(update, context, prefix, force_entry=force_entry)

    if action == "learning_skip":
        snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
        learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
        _log_transaction(user_id, "learning:skip", snapshot)
        return await _send_learning_prompt(update, context, t(_user_interface_lang(user_id), "learning_skipped"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok, _ = _check_ru_de_strict(entry, text)
    context.user_data["last_learning_entry_id"] = entry.get("id")
    context.user_data["last_learning_ok"] = bool(ok)
    context.user_data["last_learning_prev_step"] = int(entry.get("learning_step") or 0)
    await _apply_learning_reaction(update, context, ok)
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
        intervals_seconds=_learning_schedule_seconds(get_db_path(), user_id),
    )
    _log_transaction(user_id, "learning:answer", snapshot)
    feedback = _format_feedback_ru_de(entry, ok, lang=_user_interface_lang(user_id))
    if result.get("completed"):
        feedback = f"{feedback}\n\n{t(_user_interface_lang(user_id), 'learning_done_sent_fsrs')}"
    return await _send_learning_prompt(update, context, feedback)
