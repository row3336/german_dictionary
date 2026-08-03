"""Learning mode in the chat: the learning menu, the preview/quiz prompts, the
👍/👎 answer reactions, the plain end-of-queue service message and the helper that
pushes a quiz entry into the learning batch (used by quiz / clarify_fields /
db_search).

The rules themselves — the static show order, the verdict, the ladder writes, the
batch top-up — live in `common/learning_core.py`, shared with the Mini App
(miniapp.md §5.1). What stays here is what speaks Telegram.
"""

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
from ..common.input_err import apply_learning_input_err
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.learning_core import (
    FILL_BATCH_FULL,
    FILL_NO_NEW,
    add_word_to_batch,
    due_entries,
    fill_batch,
    forget_shown_history,
    learning_verdict,
    mark_preview_next,
    note_learning_shown,
    pick_learning_due_entry,
    preview_entry,
    score_learning_answer,
    skip_word,
)
from ..common.navigation import _action_key_from_text, _reply_text_with_retry
from ..common.settings import (
    _learning_daily_qty,
    _learning_intervals_text,
    _learning_mix_mode_enabled,
    _learning_ready_quorum,
    _set_learning_mix_mode,
    _topic_filter_banner,
)
from ..common.word_filter import count_words_for_filter
from ..handlers.quiz import _set_reaction
from ..i18n import t
from ..storage import (
    apply_user_word_media,
    learning_reset_session,
    learning_status_snapshot,
    list_learning_active_words,
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


async def _apply_learning_reaction(update, context, ok):
    """React to the learning answer message: 👍 correct, 👎 wrong. Flat — no streak
    escalation — mirroring the FSRS quiz reaction contract. Best-effort. The
    message id is remembered so an input_err correction can lift the 👎 later."""
    context.user_data["last_learning_answer_message_id"] = update.message.message_id
    emoji = "\U0001F44D" if ok else "\U0001F44E"  # 👍 / 👎
    await _set_reaction(context, update.effective_user.id, update.message.message_id, [emoji])


def _send_entry_to_learning(update: Update, context: ContextTypes.DEFAULT_TYPE, entry_key="quiz_entry"):
    """Send the word the user is looking at into the learning batch."""
    entry = context.user_data.get(entry_key)
    if not entry:
        return False
    return add_word_to_batch(update.effective_user.id, entry.get("id"))


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
    """Top the batch up and report what came of it."""
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    result = fill_batch(user_id)
    if result.reason == FILL_BATCH_FULL:
        message = t(lang, "learning_batch_full").format(active=result.active, daily=result.daily)
    elif result.reason == FILL_NO_NEW:
        message = t(lang, "learning_no_new")
    else:
        message = t(lang, "learning_added").format(added=result.added)
    await _reply_text_with_retry(
        update.message,
        message,
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def _send_learning_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Show the next question/preview in the learning session."""
    user_id = update.effective_user.id
    pending_preview = preview_entry(user_id)
    if pending_preview:
        pending_preview = apply_user_word_media(get_db_path(), user_id, pending_preview)  # personal media (P4)
        context.user_data["quiz_entry"] = pending_preview
        context.user_data["learning_preview_mode"] = True
        prompt_text = _format_learning_preview_prompt(pending_preview, _user_dict_lang(user_id))
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
    if not entry:
        entry = pick_learning_due_entry(context, user_id)
    if not entry:
        context.user_data["learning_preview_mode"] = False
        forget_shown_history(context)
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
    entry = apply_user_word_media(get_db_path(), user_id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    context.user_data["learning_preview_mode"] = False
    note_learning_shown(context, entry.get("id"))
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
        forget_shown_history(context)
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "learning_session_reset"),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    if action == "learning_start":
        forget_shown_history(context)
        # Empty queue: nothing previewed and nothing due -> don't enter the session
        # (no lone Back button, no 🔥). Just show the service message and stay in the
        # menu, where the "add for learning" button is already in reach.
        has_preview = preview_entry(user_id) is not None
        has_due = bool(due_entries(user_id))
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
            skip_word(user_id, entry.get("id"))
            return await _send_learning_prompt(update, context, t(_user_interface_lang(user_id), "learning_skipped"))
        if action == "learning_next":
            mark_preview_next(user_id, entry.get("id"))
            return await _send_learning_prompt(update, context, t(_user_interface_lang(user_id), "learning_saved_next"))
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "learning_use_next_skip_back"),
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "learning_next"), t(_user_interface_lang(user_id), "learning_skip")], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return LEARNING_QUIZ

    if action == "input_err":
        applied = apply_learning_input_err(context, user_id)
        lang = _user_interface_lang(user_id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        if applied:
            # lift the 👎 from the corrected answer message; no new reaction is set
            await _set_reaction(context, user_id, context.user_data.get("last_learning_answer_message_id"), [])
        # Don't re-show the corrected word: learning_apply_input_err already advanced
        # it one step (as if answered correctly from its pre-wrong step), so re-asking
        # would let it advance AGAIN and overtake the queue. Move on to whatever is due
        # now instead — on the last word this ends the cycle ("nothing to repeat").
        # (Warm-up steps are 0s, so a still-due word is picked up naturally by the
        # normal show layer; only the interval phase changes.)
        return await _send_learning_prompt(update, context, prefix)

    if action == "learning_skip":
        skip_word(user_id, entry.get("id"))
        return await _send_learning_prompt(update, context, t(_user_interface_lang(user_id), "learning_skipped"))

    ok = learning_verdict(entry, text, dont_know=text == I_DONT_KNOW_TEXT)
    await _apply_learning_reaction(update, context, ok)
    result = score_learning_answer(context, user_id, entry, ok)
    feedback = _format_feedback_ru_de(entry, ok, lang=_user_interface_lang(user_id))
    if result.get("completed"):
        feedback = f"{feedback}\n\n{t(_user_interface_lang(user_id), 'learning_done_sent_fsrs')}"
    return await _send_learning_prompt(update, context, feedback)
