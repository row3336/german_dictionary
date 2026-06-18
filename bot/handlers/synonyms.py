"""Synonyms feature: the synonym-group menu (add / remove / auto-by-translation /
clear / flag), its input + confirm handlers, and the return-to-caller dispatch
that resumes whichever quiz/weak/db-search flow opened the menu.
"""

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes

from ..app_config import (
    BACK_TEXT,
    DB_SEARCH_VIEW,
    QUIZ_ARTICLE,
    QUIZ_ARTICLE_WEAK,
    QUIZ_DE_RU,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PL_SG,
    QUIZ_PLURAL_WEAK,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE,
    SYN_ADD_INPUT,
    SYN_AUTO_CONFIRM,
    SYN_CLEAR_CONFIRM,
    SYN_MENU,
    SYN_REMOVE_INPUT,
    get_db_path,
)
from ..common.lang import _dict_lang_tag, _user_dict_lang
from ..common.navigation import _action_key_from_text, _back_to_tests
from ..labels import (
    SYN_ADD_TEXT,
    SYN_AUTO_TEXT,
    SYN_CANCEL_TEXT,
    SYN_CLEAR_TEXT,
    SYN_CONFIRM_TEXT,
    SYN_FLAG_TEXT,
    SYN_REMOVE_TEXT,
)
from ..storage import (
    add_synonym_link,
    add_word_flag,
    clear_synonym_group,
    find_word_by_value,
    get_synonym_group,
    get_word_by_id,
    list_synonym_candidates_by_ru,
    remove_synonym_link,
)
from ..ui.formatting import _entry_translation


def _syn_auto_text(user_id):
    """Handle syn auto text."""
    return f"{SYN_AUTO_TEXT} {_dict_lang_tag(user_id)}"


def _syn_menu_markup(user_id):
    """Handle syn menu markup."""
    return ReplyKeyboardMarkup(
        [
            [SYN_ADD_TEXT, SYN_REMOVE_TEXT, _syn_auto_text(user_id)],
            [SYN_CLEAR_TEXT, SYN_FLAG_TEXT, BACK_TEXT],
        ],
        resize_keyboard=True,
    )


def _syn_confirm_markup():
    """Handle syn confirm markup."""
    return ReplyKeyboardMarkup([[SYN_CONFIRM_TEXT, SYN_CANCEL_TEXT]], resize_keyboard=True)


def _format_syn_group(anchor_entry, group_entries, dict_lang):
    """Handle format syn group."""
    anchor_type = anchor_entry.get("type", "")
    anchor_base = anchor_entry.get("infinitive", "") if anchor_type == "verb" else anchor_entry.get("german", "")
    lines = [f"Synonyms for: {anchor_base} ({anchor_type})"]
    if not group_entries:
        lines.append("- no linked synonyms")
        return "\n".join(lines)
    for idx, item in enumerate(group_entries, start=1):
        item_type = item.get("type", "")
        base = item.get("infinitive", "") if item_type == "verb" else item.get("german", "")
        article = (item.get("article", "") or "").strip()
        left = f"{article} {base}".strip()
        right = _entry_translation(item, dict_lang)
        lines.append(f"{idx}. {left} >> {right}")
    return "\n".join(lines)


async def _open_synonyms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, anchor_entry, return_state):
    """Handle open synonyms menu."""
    if not anchor_entry or not anchor_entry.get("id"):
        return await _back_to_tests(update, context)
    context.user_data["syn_anchor_id"] = anchor_entry.get("id")
    context.user_data["syn_return_state"] = return_state
    group_entries = get_synonym_group(get_db_path(), anchor_entry.get("id"))
    await update.message.reply_text(
        _format_syn_group(anchor_entry, group_entries, _user_dict_lang(update.effective_user.id)),
        reply_markup=_syn_menu_markup(update.effective_user.id),
    )
    return SYN_MENU


async def _return_from_synonyms(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix="✅ Done."):
    """Handle return from synonyms."""
    from ..handlers.db_search import _send_db_search_prompt
    from ..handlers.quiz import (
        _send_article_prompt,
        _send_de_ru_prompt,
        _send_pl_sg_prompt,
        _send_ru_de_prompt,
    )
    from ..main import (
        _send_article_weak_prompt,
        _send_partizip2_weak_prompt,
        _send_plural_weak_prompt,
        _send_prateritum_weak_prompt,
    )

    anchor_id = context.user_data.get("syn_anchor_id")
    return_state = context.user_data.get("syn_return_state")
    context.user_data.pop("syn_auto_candidates", None)
    context.user_data.pop("syn_anchor_id", None)
    context.user_data.pop("syn_return_state", None)
    entry = get_word_by_id(get_db_path(), anchor_id) if anchor_id else None
    if entry:
        context.user_data["quiz_entry"] = entry
    if return_state == QUIZ_RU_DE:
        return await _send_ru_de_prompt(update, context, prefix)
    if return_state == QUIZ_DE_RU:
        return await _send_de_ru_prompt(update, context, prefix)
    if return_state == QUIZ_PL_SG:
        return await _send_pl_sg_prompt(update, context, prefix)
    if return_state == QUIZ_ARTICLE:
        return await _send_article_prompt(update, context, prefix)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _send_article_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _send_plural_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _send_prateritum_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _send_partizip2_weak_prompt(update, context, prefix)
    if return_state == DB_SEARCH_VIEW:
        return await _send_db_search_prompt(update, update.effective_user.id, prefix)
    return await _back_to_tests(update, context)


async def synonyms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms menu."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if _action_key_from_text(text, update.effective_user.id) == "back":
        return await _return_from_synonyms(update, context, None)
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor_entry = get_word_by_id(get_db_path(), anchor_id) if anchor_id else None
    if not anchor_entry:
        return await _back_to_tests(update, context)
    if text == SYN_ADD_TEXT:
        await update.message.reply_text("Send German word or id to add:", reply_markup=ReplyKeyboardRemove())
        return SYN_ADD_INPUT
    if text == SYN_REMOVE_TEXT:
        group = get_synonym_group(get_db_path(), anchor_id)
        await update.message.reply_text(
            _format_syn_group(anchor_entry, group, _user_dict_lang(update.effective_user.id)) + "\n\nSend number, word, or id to remove.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SYN_REMOVE_INPUT
    if text == _syn_auto_text(update.effective_user.id):
        candidates = list_synonym_candidates_by_ru(get_db_path(), anchor_id)
        if not candidates:
            await update.message.reply_text(f"No {_syn_auto_text(update.effective_user.id)} candidates.", reply_markup=_syn_menu_markup(update.effective_user.id))
            return SYN_MENU
        context.user_data["syn_auto_candidates"] = [x.get("id") for x in candidates]
        preview = _format_syn_group(anchor_entry, candidates, _user_dict_lang(update.effective_user.id))
        await update.message.reply_text(
            f"Will add {len(candidates)} candidate(s):\n\n{preview}",
            reply_markup=_syn_confirm_markup(),
        )
        return SYN_AUTO_CONFIRM
    if text == SYN_CLEAR_TEXT:
        await update.message.reply_text("Clear whole synonym group?", reply_markup=_syn_confirm_markup())
        return SYN_CLEAR_CONFIRM
    if text == SYN_FLAG_TEXT:
        add_word_flag(get_db_path(), word_id=anchor_id, user_id=update.effective_user.id, comment="synonym_review")
        await update.message.reply_text("✅ Flag saved.", reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    await update.message.reply_text("Use synonyms buttons.", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms add input."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        anchor_entry = get_word_by_id(get_db_path(), context.user_data.get("syn_anchor_id"))
        group = get_synonym_group(get_db_path(), context.user_data.get("syn_anchor_id"))
        await update.message.reply_text(_format_syn_group(anchor_entry or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor_entry = get_word_by_id(get_db_path(), anchor_id)
    target = find_word_by_value(get_db_path(), text)
    if not anchor_entry or not target:
        await update.message.reply_text("Target not found. Send German word or id.")
        return SYN_ADD_INPUT
    if (anchor_entry.get("type", "") or "").lower() != (target.get("type", "") or "").lower():
        await update.message.reply_text("Type mismatch. Only same type is allowed.")
        return SYN_ADD_INPUT
    changed, _ = add_synonym_link(get_db_path(), anchor_id, target.get("id"))
    msg = "✅ Linked." if changed else "✅ Already linked."
    group = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"{msg}\n\n{_format_syn_group(anchor_entry, group, _user_dict_lang(update.effective_user.id))}", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_remove_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms remove input."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        return SYN_MENU
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor = get_word_by_id(get_db_path(), anchor_id)
    group = get_synonym_group(get_db_path(), anchor_id)
    if not anchor or not group:
        return await _return_from_synonyms(update, context, None)
    target = None
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(group):
            target = group[idx - 1]
    if target is None:
        target = find_word_by_value(get_db_path(), text)
    if not target:
        await update.message.reply_text("Target not found in group.")
        return SYN_REMOVE_INPUT
    if target.get("id") == anchor_id:
        await update.message.reply_text("Cannot remove anchor word from itself. Use Clear group.")
        return SYN_REMOVE_INPUT
    removed = remove_synonym_link(get_db_path(), target.get("id"))
    msg = "✅ Removed." if removed else "❌ Not linked."
    group_new = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"{msg}\n\n{_format_syn_group(anchor, group_new, _user_dict_lang(update.effective_user.id))}", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_auto_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms auto confirm."""
    text = update.message.text.strip()
    if text == SYN_CANCEL_TEXT or _action_key_from_text(text, update.effective_user.id) == "back":
        anchor_id = context.user_data.get("syn_anchor_id")
        anchor = get_word_by_id(get_db_path(), anchor_id)
        group = get_synonym_group(get_db_path(), anchor_id)
        await update.message.reply_text(_format_syn_group(anchor or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    if text != SYN_CONFIRM_TEXT:
        await update.message.reply_text("Use Confirm or Cancel.", reply_markup=_syn_confirm_markup())
        return SYN_AUTO_CONFIRM
    anchor_id = context.user_data.get("syn_anchor_id")
    ids = context.user_data.get("syn_auto_candidates") or []
    added = 0
    for wid in ids:
        changed, _ = add_synonym_link(get_db_path(), anchor_id, wid)
        if changed:
            added += 1
    anchor = get_word_by_id(get_db_path(), anchor_id)
    group = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"✅ Auto linked: {added}", reply_markup=_syn_menu_markup(update.effective_user.id))
    await update.message.reply_text(_format_syn_group(anchor or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms clear confirm."""
    text = update.message.text.strip()
    if text == SYN_CANCEL_TEXT or _action_key_from_text(text, update.effective_user.id) == "back":
        return SYN_MENU
    if text != SYN_CONFIRM_TEXT:
        await update.message.reply_text("Use Confirm or Cancel.", reply_markup=_syn_confirm_markup())
        return SYN_CLEAR_CONFIRM
    anchor_id = context.user_data.get("syn_anchor_id")
    cleared = clear_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text("✅ Group cleared." if cleared else "✅ Group already empty.", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU
