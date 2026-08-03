"""P6.3 — general user feedback.

A "feedback" entry in Settings (open to everyone) → free-text input → stored in
the `feedback` table. spec #5: no instant owner ping / #feedback tag — the owner
sees it in the thrice-daily digest / inline inbox and reviews/deletes it there.
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import FEEDBACK_INPUT, SETTINGS_MENU, get_db_path
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..i18n import t
from ..storage import add_feedback
from ..ui.keyboards import _settings_menu_keys

# P8: cap feedback length against abuse/bloat.
_MAX_FEEDBACK_LEN = 1000


async def _start_feedback(update: Update, user_id):
    """Prompt the user to type their feedback; enter the FEEDBACK_INPUT state."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_text(
        t(lang, "feedback_prompt"),
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
    )
    return FEEDBACK_INPUT


def _back_to_settings(update, user_id, text):
    """Reply with `text` and the Settings keyboard, returning to SETTINGS_MENU."""
    return update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )


async def feedback_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the typed feedback; back cancels to Settings.

    spec #5: feedback is only persisted — the owner sees it in the thrice-daily
    digest / inline inbox (no instant ping, no #feedback tag).
    """
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back" or not text:
        await _back_to_settings(update, user_id, t(lang, "settings_title"))
        return SETTINGS_MENU
    add_feedback(get_db_path(), user_id, text[:_MAX_FEEDBACK_LEN])
    await _back_to_settings(update, user_id, t(lang, "feedback_saved"))
    return SETTINGS_MENU
