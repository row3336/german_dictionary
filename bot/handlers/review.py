"""P6.3 / spec #5 — owner review submenu (reports / feedback / requests / broadcast).

The reply-keyboard reports/feedback flow was replaced by the inline inbox engine
(handlers/owner_inbox.py): the review buttons just open the same paginated inline
list -> card -> close/delete that the thrice-daily digest uses. Access requests
open the inline pending list (handlers/access.py). This module now only renders
the submenu and routes its buttons.
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import OWNER_REVIEW_MENU, SETTINGS_MENU, is_owner
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..i18n import t
from ..text_utils import md_escape
from ..ui.keyboards import _settings_menu_keys


def _who(item):
    """Display label for a user: @username or first name (never a raw id)."""
    if item.get("username"):
        return "@" + item["username"]
    return item.get("first_name") or "user"


def _clickable(item):
    """Markdown link to a user's profile (works even without a @username).

    The label is escaped and stripped of link-breaking chars so a crafted
    username/first_name can't break out of the Markdown link (P8 hardening).
    Shared with owner_stats (clickable user names).
    """
    label = md_escape(_who(item))
    for ch in "[]()":
        label = label.replace(ch, "")
    return f"[{label}](tg://user?id={int(item['user_id'])})"


def _owner_review_keys(user_id):
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "review_reports"), t(lang, "review_feedback")],
        [t(lang, "review_requests"), t(lang, "review_broadcast")],
        [t(lang, "back")],
    ]


async def _send_review_menu(update: Update, user_id):
    """Owner review submenu: reports / feedback / requests / broadcast / back."""
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "review_menu"),
        reply_markup=ReplyKeyboardMarkup(_owner_review_keys(user_id), resize_keyboard=True),
    )
    return OWNER_REVIEW_MENU


async def owner_review_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route the owner review submenu (all lists are inline now, spec #5)."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return SETTINGS_MENU
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back":
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU
    if text in (t(lang, "review_reports"), t(lang, "review_feedback")):
        # spec #5: reports/feedback use the same inline inbox engine as the digest
        # (paginated list -> card -> close/delete) — one code path, no reply flow.
        from .owner_inbox import _list_view

        kind = "r" if text == t(lang, "review_reports") else "f"
        list_text, markup = _list_view(kind, lang, 0)
        await update.message.reply_text(list_text, reply_markup=markup)
        return OWNER_REVIEW_MENU
    if text == t(lang, "review_requests"):
        # spec #5: on-demand inline pending-access list (same engine as the digest).
        from .access import _pending_list_view

        list_text, markup = _pending_list_view(lang)
        await update.message.reply_text(list_text, reply_markup=markup)
        return OWNER_REVIEW_MENU
    if text == t(lang, "review_broadcast"):
        from .broadcast import _start_broadcast

        return await _start_broadcast(update, user_id)
    return await _send_review_menu(update, user_id)
