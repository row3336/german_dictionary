"""Dump feature handlers: manual DB/logs dumps and auto-dump mode selection."""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    AUTO_DUMP_MENU,
    AUTO_DUMP_SETTING_KEY,
    DUMP_MENU,
    SETTINGS_MENU,
    get_db_path,
    is_owner,
)
from ..common.dumps import _create_dump_file, _create_logs_dump_file
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_main_menu
from ..i18n import t
from ..jobs.auto_dump import _configure_auto_dump
from ..storage import get_setting, set_setting
from ..ui.keyboards import _settings_menu_keys


def _dump_menu_keys(user_id):
    """Handle dump menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "dump_logs"), t(lang, "dump_db")],
        [t(lang, "auto_dump"), t(lang, "back")],
    ]


def _auto_dump_keys(user_id):
    """Handle auto dump keys."""
    lang = _user_interface_lang(user_id)
    return [[t(lang, "daily"), t(lang, "weekly")], [t(lang, "off"), t(lang, "back")]]


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

    if action == "auto_dump":
        mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
        await update.message.reply_text(
            f"Current auto dump mode: {mode}",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    await update.message.reply_text(
        "Use dump menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU


async def auto_dump_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle auto dump choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            "dump:",
            reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
        )
        return DUMP_MENU

    mode_map = {"daily": "daily", "weekly": "weekly", "off": "off"}
    if action not in mode_map:
        await update.message.reply_text(
            "Use auto dump buttons.",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    mode = mode_map[action]
    set_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, mode)
    _configure_auto_dump(context.application, mode)
    await update.message.reply_text(
        f"✅ Auto dump mode set: {mode}",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU
