"""Field-editing flow ("clarify_fields"): entry from quiz/db_search/add, the
ADD_EDIT-state handler, and resume-after-edit dispatch.

Most cross-feature calls (weak/learning/db_search/synonyms/add prompts) still
live in main and are imported LOCALLY inside the functions to avoid cycles.
Quiz prompt senders come from handlers.quiz (already extracted).

The UI button is "clarify_fields"; functions/keyboards are named to match
(clarify_fields_input, _start_clarify_fields, _resume_after_clarify,
_clarify_fields_markup). The FSM state stays ADD_EDIT
(internal, shared with the add-flow).
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    ADD_EDIT,
    ALLOWED_AI_TYPES,
    BACK_TEXT,
    CASE_TYPES,
    DB_SEARCH_VIEW,
    LEARNING_QUIZ,
    MENU,
    PREPOSITION_TYPES,
    QUIZ_ARTICLE,
    QUIZ_ARTICLE_WEAK,
    QUIZ_DE_RU,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PL_SG,
    QUIZ_PLURAL_WEAK,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE,
    get_db_path,
    get_owner_id,
    is_owner,
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
from ..labels import SYNONYMS_TEXT
from ..storage import (
    add_error_report,
    delete_word,
    set_user_word_media,
    set_user_word_translation,
    update_word_fields,
)
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
    _quiz_markup,
    _type_choice_markup,
    _value_entry_markup,
)


# P8: cap user-supplied free text (report / personal media) against abuse/bloat.
_MAX_REPORT_LEN = 1000
_MAX_MEDIA_LEN = 1000


def _edit_entry(context: ContextTypes.DEFAULT_TYPE):
    """The entry currently being edited, by source (add vs quiz/db_search)."""
    source = context.user_data.get("edit_source", "add")
    entry_key = "pending_entry" if source == "add" else "quiz_edit_entry"
    return context.user_data.get(entry_key) or {}


def _is_prep_entry(entry):
    """True when the entry is a preposition (spec #1 — gates the extra fields)."""
    return str((entry or {}).get("type", "")).strip().lower() == "praeposition"


def _clarify_screen(context: ContextTypes.DEFAULT_TYPE, user_id):
    """Unified clarify keyboard for the current edit context (P6 collapsed card)."""
    in_search = context.user_data.get("edit_return_state") == DB_SEARCH_VIEW
    source = context.user_data.get("edit_source", "add")
    is_prep = _is_prep_entry(_edit_entry(context))
    return _clarify_fields_markup(source, user_id, in_search=in_search, is_prep=is_prep)


def _allowed_clarify_fields(source, user_id=None, is_prep=False):
    """Editable fields for this source. A guest (non-owner) editing a quiz/db_search
    card may only touch image/example (their personal version, P4). The two
    preposition fields (spec #1) are owner-only and only offered for a preposition."""
    if source == "quiz" and user_id is not None and not is_owner(user_id):
        # spec #7: guest may also edit their personal translation (their dict_lang).
        return {"image", "example", "my_translation"}
    if source == "add":
        fields = {
            "ru", "ukr", "en", "de", "article", "plural", "type",
            "prateritum", "partizip2", "infinitive", "komparativ", "superlativ", "image", "example",
        }
    elif source == "quiz":
        fields = {
            "ru", "ukr", "de", "article", "plural", "prateritum",
            "partizip2", "infinitive", "type", SYNONYMS_TEXT, "komparativ", "superlativ", "image", "example",
        }
    else:
        return {"russian", "level"}
    if is_prep:
        fields = fields | {"preposition_type", "case_type"}
    return fields


def _prep_cascade_next(context: ContextTypes.DEFAULT_TYPE, field, updated):
    """Next field in the spec #1 preposition capture cascade, or None.

    Setting type=praeposition arms the cascade and walks preposition_type ->
    case_type, then disarms. A single edit of one preposition field (cascade not
    armed) returns None so it just saves and resumes like any other field.
    """
    if field == "type":
        if _is_prep_entry(updated):
            context.user_data["prep_cascade"] = True
            return "preposition_type"
        context.user_data.pop("prep_cascade", None)
        return None
    if not context.user_data.get("prep_cascade"):
        return None
    if field == "preposition_type":
        return "case_type"
    context.user_data.pop("prep_cascade", None)
    return None


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
        "my_translation",
        "article",
        "plural",
        "prateritum",
        "partizip2",
        "komparativ",
        "superlativ",
        "german",
        "infinitive",
        "type",
        "preposition_type",
        "case_type",
    }
    if field not in allowed:
        return None, f"Unknown field: {field}"

    updated = dict(entry)
    if field == "type":
        normalized_type = _type_label(value)
        if normalized_type not in ALLOWED_AI_TYPES:
            return None, "Invalid type."
        updated["type"] = normalized_type
    elif field in ("preposition_type", "case_type"):
        # spec #1: controlled vocabulary — guard the text path (field=value) too, not
        # just the preset buttons. '' clears the field; anything else must be known.
        normalized = value.strip().lower()
        allowed_vocab = PREPOSITION_TYPES if field == "preposition_type" else CASE_TYPES
        if normalized and normalized not in allowed_vocab:
            return None, f"Invalid {field}."
        updated[field] = normalized
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
    context.user_data["edit_stage"] = "pick_field"
    context.user_data["edit_return_state"] = return_state
    await _reply_text_with_retry(
        update.message,
        f"```text\n{_format_word_card(entry, title, user_id=update.effective_user.id)}\n```",
        parse_mode="Markdown",
    )
    await _reply_text_with_retry(
        update.message,
        "Choose edit action:",
        reply_markup=_clarify_screen(context, update.effective_user.id),
    )
    return ADD_EDIT


async def open_clarify_from_report(update: Update, context: ContextTypes.DEFAULT_TYPE, entry):
    """spec #5: enter the owner clarify card for a report's word, coming from the
    inline inbox (a ConversationHandler entry_point on the `inb:open:` callback).

    The owner edits the shared dictionary as usual. There is no quiz to resume to,
    so back/after-edit lands in the tests menu (edit_return_state stays unset ->
    _resume_after_clarify has no quiz_entry -> _back_to_tests). Messages are sent
    via the callback's chat (a callback has no update.message).
    """
    context.user_data["edit_source"] = "quiz"
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_stage"] = "pick_field"
    context.user_data.pop("edit_return_state", None)
    context.user_data.pop("quiz_entry", None)
    user_id = update.effective_user.id
    msg = update.callback_query.message
    await msg.reply_text(
        f"```text\n{_format_word_card(entry, 'Word from report:', user_id=user_id)}\n```",
        parse_mode="Markdown",
    )
    await msg.reply_text("Choose edit action:", reply_markup=_clarify_screen(context, user_id))
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
        context.user_data["edit_stage"] = "pick_field"
        await _reply_text_with_retry(
            update.message,
            "Choose edit action:",
            reply_markup=_clarify_screen(context, update.effective_user.id),
        )
        return ADD_EDIT

    return await _back_to_tests(update, context)


async def clarify_fields_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle input when clarifying/editing the fields of a word card."""
    from ..handlers.db_search import _send_db_search_prompt
    from ..handlers.synonyms import _open_synonyms_menu
    from ..handlers.weak import (
        _send_article_weak_prompt,
        _send_partizip2_weak_prompt,
        _send_plural_weak_prompt,
        _send_prateritum_weak_prompt,
    )
    from ..handlers.add_words import _show_ai_preview, _stop_queue
    from ..handlers.learning import _send_entry_to_learning

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

    stage = context.user_data.get("edit_stage", "pick_field")

    if stage == "report_comment":
        # P6.2: user typed their error/fix report; back/stop cancels back to the card.
        lang = _user_interface_lang(update.effective_user.id)
        if _action_key_from_text(text, update.effective_user.id) in ("back", "stop"):
            context.user_data["edit_stage"] = "pick_field"
            await _reply_text_with_retry(
                update.message, "Choose edit action:",
                reply_markup=_clarify_screen(context, update.effective_user.id),
            )
            return ADD_EDIT
        text = text[:_MAX_REPORT_LEN]  # P8: cap report length (anti-abuse)
        word_id = entry.get("id") or ""
        return_state = context.user_data.get("edit_return_state")
        add_error_report(
            get_db_path(), update.effective_user.id, word_id, text,
            mode=str(return_state), state="clarify", last_action=entry.get("german", ""),
        )
        owner_id = get_owner_id()
        if owner_id and owner_id != update.effective_user.id:
            u = update.effective_user
            tag = f"@{u.username}" if u.username else (u.first_name or str(u.id))
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=(
                        f"{t(_user_interface_lang(owner_id), 'report_owner_hdr')} #error_report\n"
                        f"{tag} · {entry.get('german', '')}\n\n{text}"
                    ),
                )
            except Exception:
                pass
        context.user_data["edit_stage"] = "pick_field"
        if return_state == DB_SEARCH_VIEW:
            return await _send_db_search_prompt(update, update.effective_user.id, t(lang, "report_saved"))
        return await _resume_after_clarify(update, context, t(lang, "report_saved"))

    if stage == "delete_confirm":
        # P6.2: only an explicit "yes" deletes; anything else (incl. back) cancels.
        if text.strip().lower() != "yes":
            context.user_data["edit_stage"] = "pick_field"
            await _reply_text_with_retry(
                update.message, "Choose edit action:",
                reply_markup=_clarify_screen(context, update.effective_user.id),
            )
            return ADD_EDIT
        word_id = entry.get("id")
        removed = delete_word(get_db_path(), word_id) if word_id else False
        context.user_data.pop("quiz_edit_entry", None)
        current_quiz_entry = context.user_data.get("quiz_entry")
        if current_quiz_entry and current_quiz_entry.get("id") == word_id:
            context.user_data.pop("quiz_entry", None)
        last_answered = context.user_data.get("quiz_last_answered_entry")
        if last_answered and last_answered.get("id") == word_id:
            context.user_data.pop("quiz_last_answered_entry", None)
        context.user_data["edit_stage"] = "pick_field"
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

    if _action_key_from_text(text, update.effective_user.id) == "back":
        context.user_data.pop("prep_cascade", None)  # spec #1: back abandons the cascade
        if source == "quiz":
            # P6: one unified card screen — back from it exits the edit; back from a
            # value-entry returns to the card.
            if stage == "pick_field":
                if context.user_data.get("edit_return_state") == DB_SEARCH_VIEW:
                    context.user_data.pop("quiz_edit_entry", None)
                    return await _send_db_search_prompt(update, update.effective_user.id, None)
                return await _resume_after_clarify(update, context, "❌ Edit canceled.")
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_clarify_screen(context, update.effective_user.id),
            )
            return ADD_EDIT

        if stage == "pick_field":
            return await _show_ai_preview(update, entry)
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_screen(context, update.effective_user.id),
        )
        return ADD_EDIT

    # spec #1: preset-value fields (type + the two preposition fields) use BACK_TEXT
    # in their preset keyboards; back from any of them returns to the card.
    if stage == "enter_value" and context.user_data.get("edit_field") in ("type", "preposition_type", "case_type") and text == BACK_TEXT:
        context.user_data["edit_stage"] = "pick_field"
        context.user_data.pop("edit_field", None)
        context.user_data.pop("prep_cascade", None)
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_screen(context, update.effective_user.id),
        )
        return ADD_EDIT

    if stage == "pick_field":
        if source == "quiz":
            # P6: card actions now live on the unified field screen (no root menu).
            uid = update.effective_user.id
            if _action_key_from_text(text, uid) == "send_learn":
                added = _send_entry_to_learning(update, context, entry_key="quiz_edit_entry")
                state = context.user_data.get("edit_return_state")
                prefix = "✅ Added to learning." if added else "Nothing to send."
                if state == DB_SEARCH_VIEW:
                    return await _send_db_search_prompt(update, uid, prefix)
                return await _resume_after_clarify(update, context, prefix)
            if text == t(_user_interface_lang(uid), "report_card"):
                lang = _user_interface_lang(uid)
                context.user_data["edit_stage"] = "report_comment"
                await update.message.reply_text(
                    t(lang, "report_prompt"),
                    reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
                )
                return ADD_EDIT
            if text.strip().lower() == "delete" and is_owner(uid):
                word_id = entry.get("id")
                if not word_id:
                    await update.message.reply_text(
                        "❌ Cannot delete: missing word id.",
                        reply_markup=_clarify_screen(context, uid),
                    )
                    return ADD_EDIT
                # P6.2: require an explicit "yes" before deleting (like reset_periodicity).
                context.user_data["edit_stage"] = "delete_confirm"
                await update.message.reply_text(
                    'Type "yes" to delete this word:',
                    reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(uid), "back")]], resize_keyboard=True),
                )
                return ADD_EDIT
        # spec #7: the personal-translation button is localized — map it back.
        if text == t(_user_interface_lang(update.effective_user.id), "my_translation"):
            field = "my_translation"
        else:
            field = text.strip().lower()
        allowed_fields = _allowed_clarify_fields(
            source, update.effective_user.id, is_prep=_is_prep_entry(entry)
        )
        if field not in allowed_fields:
            await update.message.reply_text(
                "Use field buttons.",
                reply_markup=_clarify_screen(context, update.effective_user.id),
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
        elif field in ("preposition_type", "case_type"):
            # spec #1: pick from preset buttons (controlled vocabulary), no free text.
            prompt = "Choose preposition category:" if field == "preposition_type" else "Choose case:"
            await update.message.reply_text(
                prompt,
                reply_markup=_value_entry_markup(field, update.effective_user.id),
            )
        elif field == "my_translation":
            # spec #7: localized prompt (personal translation in the user's dict_lang).
            await update.message.reply_text(
                t(_user_interface_lang(update.effective_user.id), "my_translation_prompt"),
                reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True),
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
            reply_markup=_clarify_screen(context, update.effective_user.id),
        )
        return ADD_EDIT

    if field in ("image", "example", "my_translation"):
        text = text[:_MAX_MEDIA_LEN]  # P8: cap personal media/translation length (anti-abuse)

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
        # spec #1: preset-value fields re-show their preset buttons on a bad value.
        reply_markup = (
            _value_entry_markup(field, update.effective_user.id)
            if field in ("type", "preposition_type", "case_type")
            else ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True)
        )
        await update.message.reply_text(
            error,
            reply_markup=reply_markup,
        )
        return ADD_EDIT

    context.user_data[entry_key] = updated
    context.user_data["edit_stage"] = "pick_field"
    context.user_data.pop("edit_field", None)

    # spec #1: setting type=praeposition (or completing preposition_type) chains the
    # cascade; the just-saved value is persisted first so bailing out keeps it.
    next_field = _prep_cascade_next(context, field, updated)

    if source == "quiz":
        user_id = update.effective_user.id
        if is_owner(user_id):
            # Owner edits the shared word directly.
            update_word_fields(get_db_path(), updated.get("id"), updated)
        elif field == "my_translation":
            # spec #7: guest personal translation → their dict_lang column in
            # user_word_media; '' clears it (revert to shared). Shared `words` untouched.
            set_user_word_translation(
                get_db_path(),
                user_id,
                updated.get("id"),
                _user_dict_lang(user_id),
                updated.get("my_translation", ""),
            )
        else:
            # Guest: persist only the edited image/example to their personal version
            # (P4); the shared `words` row is never touched.
            set_user_word_media(
                get_db_path(),
                user_id,
                updated.get("id"),
                image=updated.get("image") if field == "image" else None,
                example=updated.get("example") if field == "example" else None,
            )
        current_quiz_entry = context.user_data.get("quiz_entry")
        if current_quiz_entry and current_quiz_entry.get("id") == updated.get("id"):
            context.user_data["quiz_entry"] = updated
        last_answered = context.user_data.get("quiz_last_answered_entry")
        if last_answered and last_answered.get("id") == updated.get("id"):
            context.user_data["quiz_last_answered_entry"] = updated

    if next_field:
        context.user_data["edit_field"] = next_field
        context.user_data["edit_stage"] = "enter_value"
        prompt = "Choose preposition category:" if next_field == "preposition_type" else "Choose case:"
        await update.message.reply_text(
            prompt,
            reply_markup=_value_entry_markup(next_field, update.effective_user.id),
        )
        return ADD_EDIT

    if source == "quiz":
        if context.user_data.get("edit_return_state") == DB_SEARCH_VIEW:
            return await _resume_after_clarify(update, context, "✅ Updated.", state_override=DB_SEARCH_VIEW)
        return await _resume_after_clarify(update, context, "✅ Updated.")
    return await _show_ai_preview(update, updated)
