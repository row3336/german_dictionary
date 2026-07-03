"""Spec #12 — owner service broadcast: a free-text announcement to all users.

Flow: Settings -> owner review menu -> "broadcast" -> owner types the text in
Russian -> AI translates it to uk/en -> preview of all three variants -> "yes"
sends it to every active user in their own interface language (reusing the P5
outbound/rate-limiter plumbing), anything else cancels. Each broadcast carries
one inline "menu" button so a user can pull their main menu keyboard back up
with a single tap (handy right after a planned restart).
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..ai import translate_broadcast
from ..app_config import BROADCAST_CONFIRM, BROADCAST_INPUT, OWNER_REVIEW_MENU, get_db_path, is_owner
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..common.outbound import SendStats, send_to_user
from ..i18n import t
from ..storage import list_active_users
from ..ui.keyboards import _main_menu_markup

BROADCAST_MENU_CALLBACK = "bcastmenu"
# P8: cap the owner's free-text input against abuse/bloat, same policy as feedback.
_MAX_BROADCAST_LEN = 1000


async def _back_to_review(update: Update, user_id, text):
    """Reply with `text` and re-show the owner review menu; returns OWNER_REVIEW_MENU."""
    from .review import _owner_review_keys

    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(_owner_review_keys(user_id), resize_keyboard=True),
    )
    return OWNER_REVIEW_MENU


async def _start_broadcast(update: Update, user_id):
    """Prompt the owner for the broadcast text; enter BROADCAST_INPUT."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_text(
        t(lang, "broadcast_prompt"),
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
    )
    return BROADCAST_INPUT


def _broadcast_preview_text(lang, texts):
    """Owner-facing preview of all three translated variants, in their own language."""
    return (
        f"{t(lang, 'broadcast_preview_hdr')}\n\n"
        f"RU:\n{texts['ru']}\n\n"
        f"UK:\n{texts['uk']}\n\n"
        f"EN:\n{texts['en']}\n\n"
        f"{t(lang, 'broadcast_confirm_prompt')}"
    )


async def broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner typed the Russian broadcast text: AI-translate it and show a preview."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return OWNER_REVIEW_MENU
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back" or not text:
        return await _back_to_review(update, user_id, t(lang, "review_menu"))

    try:
        texts = translate_broadcast(text[:_MAX_BROADCAST_LEN])
    except Exception:
        # AI failure (missing key / network / bad JSON) -> tell the owner, don't start.
        return await _back_to_review(update, user_id, t(lang, "broadcast_ai_error"))

    context.user_data["broadcast_texts"] = texts
    await update.message.reply_text(
        _broadcast_preview_text(lang, texts),
        reply_markup=ReplyKeyboardMarkup([[t(lang, "yes_btn"), t(lang, "no_btn")]], resize_keyboard=True),
    )
    return BROADCAST_CONFIRM


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner confirmed (or not) the previewed broadcast; send it out only on "yes"."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return OWNER_REVIEW_MENU
    lang = _user_interface_lang(user_id)
    texts = context.user_data.pop("broadcast_texts", None)
    if text != t(lang, "yes_btn") or not texts:
        return await _back_to_review(update, user_id, t(lang, "broadcast_canceled"))

    stats = SendStats()
    for target_id in list_active_users(get_db_path()):
        target_lang = _user_interface_lang(target_id)
        body = texts.get(target_lang, texts["ru"])
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(target_lang, "broadcast_menu_btn"), callback_data=BROADCAST_MENU_CALLBACK)]]
        )
        await send_to_user(context.bot, target_id, body, "broadcast", stats=stats, reply_markup=markup)
    stats.log("broadcast")

    return await _back_to_review(
        update,
        user_id,
        t(lang, "broadcast_report").format(delivered=stats.ok, failed=stats.blocked + stats.error),
    )


async def broadcast_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A user tapped the inline "menu" button on a broadcast: resend their main menu."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_interface_lang(user_id)
    await query.answer()
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=t(lang, "main_menu"),
            reply_markup=_main_menu_markup(user_id),
        )
    except Exception:
        # Best-effort convenience button; a failed resend must not raise into the callback dispatcher.
        pass
