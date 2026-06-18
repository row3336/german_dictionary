"""Field-editing flow ("clarify_fields"): entry from quiz/db_search/add, the
ADD_EDIT-state handler, and resume-after-edit dispatch.

Most cross-feature calls (weak/learning/db_search/synonyms/add prompts) still
live in main and are imported LOCALLY inside the functions to avoid cycles.
Quiz prompt senders come from handlers.quiz (already extracted).

The UI button is "clarify_fields"; functions/keyboards are named to match
(clarify_fields_input, _start_clarify_fields, _resume_after_clarify,
_clarify_fields_markup, _clarify_root_markup). The FSM state stays ADD_EDIT
(internal, shared with the add-flow).
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    ADD_EDIT,
    ALLOWED_AI_TYPES,
    BACK_TEXT,
    DB_SEARCH_VIEW,
    LEARNING_QUIZ,
    MENU,
    QUIZ_ARTICLE,
    QUIZ_ARTICLE_WEAK,
    QUIZ_DE_RU,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PL_SG,
    QUIZ_PLURAL_WEAK,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE,
    get_db_path,
)
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_tests, _reply_text_with_retry
from ..handlers.quiz import (
    _send_article_prompt,
    _send_de_ru_prompt,
    _send_pl_sg_prompt,
    _send_ru_de_prompt,
)
from ..i18n import t
from ..labels import QUIZ_FIELDS_TEXT, SEND_LEARN_TEXT, SYNONYMS_TEXT
from ..storage import delete_word, update_word_fields
from ..text_utils import type_label as _type_label
from ..ui.formatting import (
    _format_article_question,
    _format_de_ru_question,
    _format_pl_sg_question,
    _format_ru_question,
    _format_word_card,
)
from ..ui.keyboards import (
    _clarify_fields_markup,
    _main_menu_markup,
    _clarify_root_markup,
    _quiz_markup,
    _type_choice_markup,
)


def _allowed_clarify_fields(source):
    """Handle allowed edit fields."""
    if source == "add":
        return {
            "ru", "ukr", "en", "de", "article", "plural", "type",
            "prateritum", "partizip2", "infinitive", "komparativ", "superlativ", "image", "example",
        }
    if source == "quiz":
        return {
            "ru", "ukr", "de", "article", "plural", "prateritum",
            "partizip2", "infinitive", "type", SYNONYMS_TEXT, "komparativ", "superlativ", "image", "example",
        }
    return {"russian", "level"}


def _set_entry_field(entry, raw_update):
    """Handle set entry field."""
    if "=" not in raw_update:
        return None, "Use format: field=value"
    field, value = raw_update.split("=", 1)
    field = field.strip().lower()
    value = value.strip()

    alias = {
        "ru": "russian",
        "ukr": "ukrainian",
        "en": "english",
        "de": "german",
    }
    field = alias.get(field, field)

    allowed = {
        "russian",
        "ukrainian",
        "english",
        "image",
        "example",
        "article",
        "plural",
        "prateritum",
        "partizip2",
        "komparativ",
        "superlativ",
        "german",
        "infinitive",
        "type",
    }
    if field not in allowed:
        return None, f"Unknown field: {field}"

    updated = dict(entry)
    if field == "type":
        normalized_type = _type_label(value)
        if normalized_type not in ALLOWED_AI_TYPES:
            return None, "Invalid type."
        updated["type"] = normalized_type
    elif field == "russian":
        updated["russian"] = value
    else:
        updated[field] = value
    return updated, None


def _set_entry_field_value(entry, field, value):
    """Handle set entry field value."""
    return _set_entry_field(entry, f"{field}={value}")


def _get_clarify_target(context: ContextTypes.DEFAULT_TYPE, prefer_current=False):
    """Handle get quiz action entry."""
    current = context.user_data.get("quiz_entry")
    last_answered = context.user_data.get("quiz_last_answered_entry")
    if prefer_current and current:
        return current, "Current quiz word:"
    if last_answered:
        return last_answered, "Last answered word:"
    if current:
        return current, "Current quiz word:"
    return None, ""


async def _start_clarify_fields(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state, prefer_current=False):
    """Handle start quiz edit."""
    entry, title = _get_clarify_target(context, prefer_current=prefer_current)
    if not entry:
        return await _back_to_tests(update, context)
    context.user_data["edit_source"] = "quiz"
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_stage"] = "pick_scope"
    context.user_data["edit_return_state"] = return_state
    await _reply_text_with_retry(update.message, _format_word_card(entry, title, user_id=update.effective_user.id))
    await _reply_text_with_retry(
        update.message,
        "Choose edit action:",
        reply_markup=_clarify_root_markup(update.effective_user.id),
    )
    return ADD_EDIT


async def _resume_after_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, state_override=None):
    """Handle show quiz prompt after edit."""
    from ..handlers.weak import (
        _send_article_weak_prompt,
        _send_partizip2_weak_prompt,
        _send_plural_weak_prompt,
        _send_prateritum_weak_prompt,
    )
    from ..handlers.learning import _send_learning_prompt

    state = state_override or context.user_data.get("edit_return_state")
    if state != DB_SEARCH_VIEW:
        context.user_data.pop("quiz_edit_entry", None)
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _back_to_tests(update, context)

    if state == QUIZ_RU_DE:
        text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await _reply_text_with_retry(
            update.message,
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE

    if state == QUIZ_DE_RU:
        text = _format_de_ru_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await _reply_text_with_retry(
            update.message,
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_DE_RU

    if state == QUIZ_PL_SG:
        text = _format_pl_sg_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await _reply_text_with_retry(
            update.message,
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_PL_SG

    if state == QUIZ_ARTICLE:
        text = _format_article_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await _reply_text_with_retry(
            update.message,
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=True),
            parse_mode="Markdown",
        )
        return QUIZ_ARTICLE

    if state == QUIZ_ARTICLE_WEAK:
        return await _send_article_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PLURAL_WEAK:
        return await _send_plural_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PRATERITUM_WEAK:
        return await _send_prateritum_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PARTIZIP2_WEAK:
        return await _send_partizip2_weak_prompt(update, context, prefix_text)
    if state == LEARNING_QUIZ:
        return await _send_learning_prompt(update, context, prefix_text, force_entry=context.user_data.get("quiz_entry"))

    if state == DB_SEARCH_VIEW:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            f"```text\n{_format_word_card(entry, title='Word card:', user_id=update.effective_user.id)}\n```",
            parse_mode="Markdown",
        )
        context.user_data["quiz_edit_entry"] = entry
        context.user_data["edit_source"] = "quiz"
        context.user_data["edit_stage"] = "pick_scope"
        await _reply_text_with_retry(
            update.message,
            "Choose edit action:",
            reply_markup=_clarify_root_markup(update.effective_user.id),
        )
        return ADD_EDIT

    return await _back_to_tests(update, context)


async def clarify_fields_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add edit."""
    from ..handlers.db_search import _send_db_search_prompt
    from ..handlers.synonyms import _open_synonyms_menu
    from ..handlers.weak import (
        _send_article_weak_prompt,
        _send_partizip2_weak_prompt,
        _send_plural_weak_prompt,
        _send_prateritum_weak_prompt,
    )
    from ..handlers.learning import _send_entry_to_learning
    from ..main import (
        _show_ai_preview,
        _stop_queue,
    )

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)

    source = context.user_data.get("edit_source", "add")
    if source == "add":
        entry_key = "pending_entry"
    else:
        entry_key = "quiz_edit_entry"
    entry = context.user_data.get(entry_key)
    if not entry:
        await update.message.reply_text("No entry to edit.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    stage = context.user_data.get("edit_stage", "pick_scope" if source == "quiz" else "pick_field")
    if _action_key_from_text(text, update.effective_user.id) == "back":
        if source == "quiz":
            if stage == "pick_scope" and context.user_data.get("edit_return_state") == DB_SEARCH_VIEW:
                context.user_data.pop("quiz_edit_entry", None)
                return await _send_db_search_prompt(update, update.effective_user.id, None)
            if stage == "pick_scope":
                return await _resume_after_clarify(update, context, "❌ Edit canceled.")
            if stage == "pick_field":
                context.user_data["edit_stage"] = "pick_scope"
                await update.message.reply_text(
                    "Choose edit action:",
                    reply_markup=_clarify_root_markup(update.effective_user.id),
                )
                return ADD_EDIT
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_clarify_fields_markup(source),
            )
            return ADD_EDIT

        if stage == "pick_field":
            return await _show_ai_preview(update, entry)
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_fields_markup(source),
        )
        return ADD_EDIT

    if source == "quiz" and stage == "pick_scope":
        token = text.strip().lower()
        if token == QUIZ_FIELDS_TEXT:
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_clarify_fields_markup("quiz"),
            )
            return ADD_EDIT
        if token == SEND_LEARN_TEXT:
            added = _send_entry_to_learning(update, context, entry_key="quiz_edit_entry")
            context.user_data["edit_stage"] = "pick_scope"
            state = context.user_data.get("edit_return_state")
            prefix = "✅ Added to learning." if added else "Already in learning."
            if state == DB_SEARCH_VIEW:
                return await _send_db_search_prompt(update, update.effective_user.id, prefix)
            return await _resume_after_clarify(update, context, prefix)
        if token == "delete":
            word_id = entry.get("id")
            if not word_id:
                await update.message.reply_text(
                    "❌ Cannot delete: missing word id.",
                    reply_markup=_clarify_root_markup(update.effective_user.id),
                )
                return ADD_EDIT
            removed = delete_word(get_db_path(), word_id)
            context.user_data.pop("quiz_edit_entry", None)
            current_quiz_entry = context.user_data.get("quiz_entry")
            if current_quiz_entry and current_quiz_entry.get("id") == word_id:
                context.user_data.pop("quiz_entry", None)
            last_answered = context.user_data.get("quiz_last_answered_entry")
            if last_answered and last_answered.get("id") == word_id:
                context.user_data.pop("quiz_last_answered_entry", None)
            context.user_data["edit_stage"] = "pick_scope"
            context.user_data.pop("edit_field", None)
            state = context.user_data.get("edit_return_state")
            prefix = "✅ Deleted." if removed else "❌ Word not found."
            if state == QUIZ_RU_DE:
                return await _send_ru_de_prompt(update, context, prefix)
            if state == QUIZ_DE_RU:
                return await _send_de_ru_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE:
                return await _send_article_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE_WEAK:
                return await _send_article_weak_prompt(update, context, prefix)
            if state == QUIZ_PLURAL_WEAK:
                return await _send_plural_weak_prompt(update, context, prefix)
            if state == QUIZ_PRATERITUM_WEAK:
                return await _send_prateritum_weak_prompt(update, context, prefix)
            if state == QUIZ_PARTIZIP2_WEAK:
                return await _send_partizip2_weak_prompt(update, context, prefix)
            if state == DB_SEARCH_VIEW:
                return await _send_db_search_prompt(update, update.effective_user.id, prefix)
            return await _back_to_tests(update, context)
        await update.message.reply_text(
            "Use buttons: fields, send_learn, delete, back.",
            reply_markup=_clarify_root_markup(update.effective_user.id),
        )
        return ADD_EDIT

    if stage == "enter_value" and context.user_data.get("edit_field") == "type" and text == BACK_TEXT:
        context.user_data["edit_stage"] = "pick_field"
        context.user_data.pop("edit_field", None)
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_fields_markup(source),
        )
        return ADD_EDIT

    if stage == "pick_field":
        field = text.strip().lower()
        allowed_fields = _allowed_clarify_fields(source)
        if field not in allowed_fields:
            await update.message.reply_text(
                "Use field buttons.",
                reply_markup=_clarify_fields_markup(source),
            )
            return ADD_EDIT
        if source == "quiz" and field == SYNONYMS_TEXT:
            state = context.user_data.get("edit_return_state")
            return await _open_synonyms_menu(update, context, entry, state)
        context.user_data["edit_field"] = field

        context.user_data["edit_stage"] = "enter_value"
        if field == "type":
            await update.message.reply_text(
                "Choose new type:",
                reply_markup=_type_choice_markup(),
            )
        else:
            await update.message.reply_text(
                f"Enter new value for {field}:",
                reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True),
            )
        return ADD_EDIT

    field = context.user_data.get("edit_field", "")
    if not field:
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_fields_markup(source),
        )
        return ADD_EDIT

    if field == "type":
        normalized_type = _type_label(text)
        if normalized_type not in ALLOWED_AI_TYPES:
            await update.message.reply_text(
                "Use type buttons.",
                reply_markup=_type_choice_markup(),
            )
            return ADD_EDIT
        updated, error = _set_entry_field_value(entry, field, normalized_type)
    else:
        updated, error = _set_entry_field_value(entry, field, text)
    if error:
        reply_markup = _type_choice_markup() if field == "type" else ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True)
        await update.message.reply_text(
            error,
            reply_markup=reply_markup,
        )
        return ADD_EDIT

    context.user_data[entry_key] = updated
    context.user_data["edit_stage"] = "pick_scope" if source == "quiz" else "pick_field"
    context.user_data.pop("edit_field", None)
    if source == "quiz":
        update_word_fields(get_db_path(), updated.get("id"), updated)
        current_quiz_entry = context.user_data.get("quiz_entry")
        if current_quiz_entry and current_quiz_entry.get("id") == updated.get("id"):
            context.user_data["quiz_entry"] = updated
        last_answered = context.user_data.get("quiz_last_answered_entry")
        if last_answered and last_answered.get("id") == updated.get("id"):
            context.user_data["quiz_last_answered_entry"] = updated
        if context.user_data.get("edit_return_state") == DB_SEARCH_VIEW:
            return await _resume_after_clarify(update, context, "✅ Updated.", state_override=DB_SEARCH_VIEW)
        return await _resume_after_clarify(update, context, "✅ Updated.")
    return await _show_ai_preview(update, updated)
