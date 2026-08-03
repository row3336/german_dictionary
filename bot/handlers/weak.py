"""Weak-forms drilling in the chat: the verb-weak submenu router, the four weak
quiz handlers (article / plural / präteritum / partizip2) and their prompt senders.

What the four handlers have in common — the verdict, the scoring write and the
picker — lives in `common/weak_core.py`, and the input-err rollback in
`common/input_err.py`, because the Mini App runs the same four modes over HTTP and
a second implementation would drift (miniapp.md §5.1). What is left here is the
part that is genuinely Telegram: the prompt text and the keyboard.
"""

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
)
from ..common.input_err import apply_weak_input_err
from ..common.lang import _quiz_dir_labels, _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _reply_text_with_retry
from ..common.session import _reset_session, _reset_weak_drill_state
from ..common.weak_core import pick_weak_or_drill_entry, score_weak_answer, weak_verdict
from ..handlers.quiz import _stop_with_summary
from ..i18n import t
from ..storage import apply_user_word_media
from ..text_utils import cap_word as _cap_word
from ..text_utils import low_word as _low_word
from ..text_utils import md_escape as _md_escape
from ..text_utils import normalize_russian_singular as _normalize_russian_singular
from ..ui.formatting import (
    _entry_translation,
    _format_feedback_article,
    _format_feedback_partizip2_weak,
    _format_feedback_plural_weak,
    _format_feedback_prateritum_weak,
)
from ..ui.keyboards import _quiz_markup, _ru_de_menu_keys, _weak_article_markup


def _apply_input_err_weak(update, context, field):
    """The chat's entry point into the weak rollback (common/input_err.py)."""
    return apply_weak_input_err(context, update.effective_user.id, field)


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
    # Invalid input in the submenu: re-show the buttons (was dead code nested under
    # the weak_partizip2 branch, so it never fired — and the text was hardcoded EN).
    await _reply_text_with_retry(
        update.message,
        t(lang, "weak_use_mode_buttons"),
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
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _send_article_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_ARTICLE_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_weak_prompt(update, context, None)

    ok = weak_verdict(entry, "article", text)
    # A drill repeat is in-session practice only; score_weak_answer skips the write.
    score_weak_answer(context, update.effective_user.id, entry, "article", ok)
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
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _send_plural_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PLURAL_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PLURAL_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_plural_weak_prompt(update, context, None)

    ok = weak_verdict(entry, "plural", text, dont_know=text == I_DONT_KNOW_TEXT)
    score_weak_answer(context, update.effective_user.id, entry, "plural", ok)
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
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _send_prateritum_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PRATERITUM_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PRATERITUM_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_prateritum_weak_prompt(update, context, None)

    ok = weak_verdict(entry, "prateritum", text, dont_know=text == I_DONT_KNOW_TEXT)
    score_weak_answer(context, update.effective_user.id, entry, "prateritum", ok)
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
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _send_partizip2_weak_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PARTIZIP2_WEAK)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PARTIZIP2_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_partizip2_weak_prompt(update, context, None)

    ok = weak_verdict(entry, "partizip2", text, dont_know=text == I_DONT_KNOW_TEXT)
    score_weak_answer(context, update.effective_user.id, entry, "partizip2", ok)
    feedback = _format_feedback_partizip2_weak(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_partizip2_weak_prompt(update, context, feedback)


async def _send_article_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Handle send article weak prompt."""
    entry = force_entry or pick_weak_or_drill_entry(context, update.effective_user.id, "article", word_type="nomen")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(update.effective_user.id), "no_weak_form_words").format(form="artikel"),
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
    entry = force_entry or pick_weak_or_drill_entry(context, update.effective_user.id, "plural", word_type="nomen")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(update.effective_user.id), "no_weak_form_words").format(form="plural"),
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
    entry = force_entry or pick_weak_or_drill_entry(context, update.effective_user.id, "prateritum", word_type="verb")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        lang = _user_interface_lang(update.effective_user.id)
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(update.effective_user.id), "no_weak_form_words").format(form="präteritum"),
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
    entry = force_entry or pick_weak_or_drill_entry(context, update.effective_user.id, "partizip2", word_type="verb")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        lang = _user_interface_lang(update.effective_user.id)
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(update.effective_user.id), "no_weak_form_words").format(form="partizip2"),
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
