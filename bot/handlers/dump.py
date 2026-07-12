"""Dump feature handlers: manual on-demand DB / logs dumps to the owner.

Scheduled auto-dump was removed (spec #4, phase 2): nightly off-site backups are
now handled by the dedicated backup bot (see bot/offsite_backup/). These buttons
stay as an owner convenience for grabbing a dump right now.
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    DUMP_MENU,
    SETTINGS_MENU,
    is_owner,
)
from ..common.dumps import _create_dump_file, _create_logs_dump_file
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_main_menu
from ..i18n import t
from ..ui.keyboards import _settings_menu_keys


def _dump_menu_keys(user_id):
    """Handle dump menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "dump_logs"), t(lang, "dump_db")],
        [t(lang, "back")],
    ]


async def dump_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dump menu (owner-only, P3.2). A guest shouldn't reach it — guard just in case."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "dump_db":
        dump_path = _create_dump_file()
        await update.message.reply_document(
            document=dump_path.open("rb"),
            filename=dump_path.name,
            caption="✅ SQLite dump created.",
        )
        return DUMP_MENU

    if action == "dump_logs":
        logs_path = _create_logs_dump_file()
        if logs_path is None:
            await update.message.reply_text(
                "Log file is empty or not found yet.",
                reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
            )
            return DUMP_MENU
        await update.message.reply_document(
            document=logs_path.open("rb"),
            filename=logs_path.name,
            caption="✅ Logs dump created.",
        )
        return DUMP_MENU

    await update.message.reply_text(
        "Use dump menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU
