"""Weak-forms drilling: the verb-weak submenu router, the four weak quiz
handlers (article / plural / präteritum / partizip2), their prompt senders, the
weak/drill entry picker, and the input-err correction for weak modes.
"""

import random

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    FLAG_TEXT,
    I_DONT_KNOW_TEXT,
    QUIZ_ARTICLE_WEAK,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PLURAL_WEAK,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE_MENU,
    QUIZ_VERB_WEAK_MENU,
    get_db_path,
    is_owner,
)
from ..common.lang import _quiz_dir_labels, _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _reply_text_with_retry
from ..common.session import (
    _enqueue_weak_drill,
    _mark_session_word,
    _maybe_take_weak_drill_entry,
    _reset_session,
    _reset_weak_drill_state,
    _set_weak_drill_since_last,
    _weak_drill_since_last,
)
from ..common.settings import _topic_filter_for_query
from ..common.txlog import _capture_transaction_snapshot, _log_transaction
from ..handlers.quiz import _plural_matches, _stop_with_summary
from ..i18n import t
from ..storage import (
    apply_user_word_media,
    apply_weak_input_err,
    get_form_review_stage,
    get_form_state,
    list_weak_words,
    log_quiz_event,
    update_form_stats,
    update_weak_review_state,
)
from ..text_utils import cap_word as _cap_word
from ..text_utils import low_word as _low_word
from ..text_utils import md_escape as _md_escape
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import normalize_russian_singular as _normalize_russian_singular
from ..ui.formatting import (
    _article_matches,
    _entry_translation,
    _expected_partizip2,
    _format_feedback_article,
    _format_feedback_partizip2_weak,
    _format_feedback_plural_weak,
    _format_feedback_prateritum_weak,
)
from ..ui.keyboards import _quiz_markup, _ru_de_menu_keys, _weak_article_markup


def _remember_last_weak_answer(context, entry, field, ok):
    """Handle remember last weak answer."""
    context.user_data["last_weak_answered_entry"] = entry
    context.user_data["last_weak_answered_field"] = field
    context.user_data["last_weak_answered_ok"] = bool(ok)


def _apply_input_err_weak(update, context, field):
    """Handle apply input error for weak modes."""
    last_entry = context.user_data.get("last_weak_answered_entry")
    last_field = context.user_data.get("last_weak_answered_field")
    last_ok = context.user_data.get("last_weak_answered_ok")
    if not last_entry or last_field != field or last_ok is None:
        return False
    if last_ok:
        return False
    mode_name = f"weak_{field}"
    snapshot = _capture_transaction_snapshot(update.effective_user.id, last_entry.get("id"), [mode_name])
    if not apply_weak_input_err(get_db_path(), update.effective_user.id, last_entry.get("id"), field):
        return False
    state = get_form_state(get_db_path(), update.effective_user.id, last_entry.get("id"), field)
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(
            get_db_path(), update.effective_user.id, last_entry.get("id"), field, True
        )
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, last_entry.get("id"), field)
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_entry.get("id"),
        mode=mode_name,
        is_correct=True,
        review_stage_snapshot=stage_snapshot,
    )
    _log_transaction(update.effective_user.id, f"input_err:{mode_name}", snapshot)
    context.user_data["last_weak_answered_ok"] = True
    return True


async def quiz_verb_weak_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choice in the verb weak-mode submenu (Prateritum / Partizip2)."""
    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    lang = _user_interface_lang(update.effective_user.id)
    if action == "back":
        l1de, _ = _quiz_dir_labels(update.effective_user.id)
        await _reply_text_with_retry(
            update.message,
            f"{l1de}:",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    if action == "weak_prateritum":
        _reset_weak_drill_state(context, "weak_prateritum")
        _reset_session(context, "weak_prateritum")
        return await _send_prateritum_weak_prompt(update, context, None)
    if action == "weak_partizip2":
        _reset_weak_drill_state(context, "weak_partizip2")
        _reset_session(context, "weak_partizip2")
        return await _send_partizip2_weak_prompt(update, context, None)
        await _reply_text_with_retry(
            update.message,
            "Use verb weak mode buttons.",
            reply_markup=ReplyKeyboardMarkup([[t(lang, "weak_prateritum_mode"), t(lang, "weak_partizip2_mode")], [t(lang, "back")]], resize_keyboard=True),
        )
    return QUIZ_VERB_WEAK_MENU


async def quiz_article_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article weak."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..main import _flag_current_word

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_article")
    if _action_key_from_text(text, update.effective_user.id) == "input_err":
        applied = _apply_input_err_weak(update, context, "article")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_article_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_ARTICLE_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_weak_prompt(update, context, None)

    ok = _article_matches(text, entry.get("article", ""))
    # Drill repeats are in-session practice only — they don't move weak stats/streaks.
    if not context.user_data.get("quiz_is_weak_drill"):
        state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "article")
        snapshot = _capture_transaction_snapshot(update.effective_user.id, entry.get("id"), ["weak_article"])
        update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"article": ok})
        if not state["active"] and state["review_stage"] > 0:
            stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "article", ok)
        else:
            stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "article")
        log_quiz_event(
            get_db_path(),
            user_id=update.effective_user.id,
            word_id=entry.get("id"),
            mode="weak_article",
            is_correct=ok,
            review_stage_snapshot=stage_snapshot,
        )
        _remember_last_weak_answer(context, entry, "article", ok)
        context.user_data["quiz_last_answered_entry"] = entry
        _mark_session_word(context, "weak_article", entry.get("id"))
        _log_transaction(update.effective_user.id, "quiz:weak_article", snapshot)
        if not ok:
            _enqueue_weak_drill(context, "weak_article", entry.get("id"), update.effective_user.id)
    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id), lang=_user_interface_lang(update.effective_user.id))
    return await _send_article_weak_prompt(update, context, feedback)


async def quiz_plural_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz plural weak."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..main import _flag_current_word

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_plural")
    if _action_key_from_text(text, update.effective_user.id) == "input_err":
        applied = _apply_input_err_weak(update, context, "plural")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_plural_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PLURAL_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PLURAL_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_plural_weak_prompt(update, context, None)

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _plural_matches(text, entry.get("plural", ""))
    if not context.user_data.get("quiz_is_weak_drill"):
        state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "plural")
        snapshot = _capture_transaction_snapshot(update.effective_user.id, entry.get("id"), ["weak_plural"])
        update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"plural": ok})
        if not state["active"] and state["review_stage"] > 0:
            stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "plural", ok)
        else:
            stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "plural")
        log_quiz_event(
            get_db_path(),
            user_id=update.effective_user.id,
            word_id=entry.get("id"),
            mode="weak_plural",
            is_correct=ok,
            review_stage_snapshot=stage_snapshot,
        )
        _remember_last_weak_answer(context, entry, "plural", ok)
        context.user_data["quiz_last_answered_entry"] = entry
        _mark_session_word(context, "weak_plural", entry.get("id"))
        _log_transaction(update.effective_user.id, "quiz:weak_plural", snapshot)
        if not ok:
            _enqueue_weak_drill(context, "weak_plural", entry.get("id"), update.effective_user.id)
    feedback = _format_feedback_plural_weak(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_plural_weak_prompt(update, context, feedback)


async def quiz_prateritum_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz prateritum weak."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..main import _flag_current_word

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_prateritum")
    if _action_key_from_text(text, update.effective_user.id) == "input_err":
        applied = _apply_input_err_weak(update, context, "prateritum")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_prateritum_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PRATERITUM_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PRATERITUM_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_prateritum_weak_prompt(update, context, None)

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_answer(text) == _normalize_answer(entry.get("prateritum", ""))
    if not context.user_data.get("quiz_is_weak_drill"):
        state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum")
        snapshot = _capture_transaction_snapshot(update.effective_user.id, entry.get("id"), ["weak_prateritum"])
        update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"prateritum": ok})
        if not state["active"] and state["review_stage"] > 0:
            stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum", ok)
        else:
            stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum")
        log_quiz_event(
            get_db_path(),
            user_id=update.effective_user.id,
            word_id=entry.get("id"),
            mode="weak_prateritum",
            is_correct=ok,
            review_stage_snapshot=stage_snapshot,
        )
        _remember_last_weak_answer(context, entry, "prateritum", ok)
        context.user_data["quiz_last_answered_entry"] = entry
        _mark_session_word(context, "weak_prateritum", entry.get("id"))
        _log_transaction(update.effective_user.id, "quiz:weak_prateritum", snapshot)
        if not ok:
            _enqueue_weak_drill(context, "weak_prateritum", entry.get("id"), update.effective_user.id)
    feedback = _format_feedback_prateritum_weak(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_prateritum_weak_prompt(update, context, feedback)


async def quiz_partizip2_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz partizip2 weak."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..main import _flag_current_word

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_partizip2")
    if _action_key_from_text(text, update.effective_user.id) == "input_err":
        applied = _apply_input_err_weak(update, context, "partizip2")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_partizip2_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PARTIZIP2_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PARTIZIP2_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_partizip2_weak_prompt(update, context, None)

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_answer(text) == _expected_partizip2(entry)
    if not context.user_data.get("quiz_is_weak_drill"):
        state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2")
        snapshot = _capture_transaction_snapshot(update.effective_user.id, entry.get("id"), ["weak_partizip2"])
        update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"partizip2": ok})
        if not state["active"] and state["review_stage"] > 0:
            stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2", ok)
        else:
            stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2")
        log_quiz_event(
            get_db_path(),
            user_id=update.effective_user.id,
            word_id=entry.get("id"),
            mode="weak_partizip2",
            is_correct=ok,
            review_stage_snapshot=stage_snapshot,
        )
        _remember_last_weak_answer(context, entry, "partizip2", ok)
        context.user_data["quiz_last_answered_entry"] = entry
        _mark_session_word(context, "weak_partizip2", entry.get("id"))
        _log_transaction(update.effective_user.id, "quiz:weak_partizip2", snapshot)
        if not ok:
            _enqueue_weak_drill(context, "weak_partizip2", entry.get("id"), update.effective_user.id)
    feedback = _format_feedback_partizip2_weak(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_partizip2_weak_prompt(update, context, feedback)


def _pick_weak_entry(context, user_id, weak_field, word_type=None):
    """Handle pick weak entry."""
    due = list_weak_words(
        get_db_path(),
        user_id=user_id,
        weak_field=weak_field,
        word_type=word_type,
        topics=_topic_filter_for_query(user_id),
        limit=300,
    )
    if not due:
        return None
    active = [w for w in due if w.get("is_active")]
    pool_src = active if active else due
    # Read-only: avoid repeating the last shown word; the final pick is recorded
    # by _pick_weak_or_drill_entry (so it reflects drill overrides too).
    last_id = context.user_data.get("last_quiz_ids", {}).get(f"weak_{weak_field}")
    pool = [w for w in pool_src if w.get("id") != last_id] if len(pool_src) > 1 else pool_src
    if not pool:
        pool = pool_src
    return random.choice(pool)


def _pick_weak_or_drill_entry(context, user_id, weak_field, word_type=None):
    """Pick the next weak word: a fresh due word, or a drill repeat — never two in a row."""
    mode = f"weak_{weak_field}"
    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    last_id = last_ids.get(mode)
    fresh = _pick_weak_entry(context, user_id, weak_field, word_type=word_type)

    drill = _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=last_id, force=False)
    if drill:
        entry, is_drill = drill, True
    elif fresh:
        entry, is_drill = fresh, False
    else:
        # No fresh due word left — fall back to a drill (still avoid consecutive if possible).
        drill = _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=last_id, force=True)
        if not drill:
            drill = _maybe_take_weak_drill_entry(context, mode, user_id, avoid_id=None, force=True)
        entry, is_drill = (drill, True) if drill else (None, False)

    if not entry:
        return None

    last_ids[mode] = entry.get("id")
    context.user_data["quiz_is_weak_drill"] = is_drill
    if not is_drill:
        _set_weak_drill_since_last(context, mode, _weak_drill_since_last(context, mode) + 1)
    return entry


async def _send_article_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Handle send article weak prompt."""
    entry = force_entry or _pick_weak_or_drill_entry(context, update.effective_user.id, "article", word_type="nomen")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            "No weak article words.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    text = f">>    {_normalize_russian_singular(_entry_translation(entry, _user_dict_lang(update.effective_user.id)))} -> ____ {_cap_word(entry.get('german', ''))}?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await _reply_text_with_retry(
        update.message,
        prompt,
        reply_markup=_weak_article_markup(update.effective_user.id),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE_WEAK


async def _send_plural_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Handle send plural weak prompt."""
    entry = force_entry or _pick_weak_or_drill_entry(context, update.effective_user.id, "plural", word_type="nomen")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            "No weak plural words.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    article = (entry.get("article", "") or "").strip()
    text = f">>    {article} {_cap_word(entry.get('german', ''))} -> plural?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await _reply_text_with_retry(
        update.message,
        prompt,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PLURAL_WEAK


async def _send_prateritum_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Show the Prateritum weak-mode question (or return to submenu if no words)."""
    entry = force_entry or _pick_weak_or_drill_entry(context, update.effective_user.id, "prateritum", word_type="verb")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        lang = _user_interface_lang(update.effective_user.id)
        await _reply_text_with_retry(
            update.message,
            "No weak prateritum words.",
            reply_markup=ReplyKeyboardMarkup([[t(lang, "weak_prateritum_mode"), t(lang, "weak_partizip2_mode")], [t(lang, "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    text = f">>    {_low_word(entry.get('infinitive', ''))} -> Prateritum?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await _reply_text_with_retry(
        update.message,
        prompt,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PRATERITUM_WEAK


async def _send_partizip2_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Show the Partizip2 weak-mode question (or return to submenu if no words)."""
    entry = force_entry or _pick_weak_or_drill_entry(context, update.effective_user.id, "partizip2", word_type="verb")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        lang = _user_interface_lang(update.effective_user.id)
        await _reply_text_with_retry(
            update.message,
            "No weak partizip2 words.",
            reply_markup=ReplyKeyboardMarkup([[t(lang, "weak_prateritum_mode"), t(lang, "weak_partizip2_mode")], [t(lang, "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    text = f">>    {_low_word(entry.get('infinitive', ''))} -> Partizip2?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await _reply_text_with_retry(
        update.message,
        prompt,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PARTIZIP2_WEAK
