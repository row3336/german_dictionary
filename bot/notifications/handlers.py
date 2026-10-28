"""The same personal settings screens serve permanent and reminder-inline entry."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import TelegramError

from ..app_config import MENU, NOTIFICATION_SETTINGS_MENU, NOTIFICATION_SETTINGS_TIME, SETTINGS_MENU, get_db_path
from ..common.lang import _user_interface_lang
from ..handlers.access import access_allowed
from ..i18n import t
from ..ui.keyboards import _settings_menu_keys
from .settings import KINDS, get_preference, parse_time, set_preference


def _message(update):
    return getattr(update, "effective_message", None) or getattr(update, "message", None) or update.callback_query.message


def _matches(text, key):
    return text in {t(lang, key) for lang in ("en", "ru", "uk")}


def notification_inline_markup(user_id, kind):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t(_user_interface_lang(user_id), "notifications_title"), callback_data=f"notify:{kind}")
    ]])


async def show_notification_settings(update, context, kind=None):
    uid = update.effective_user.id
    if not access_allowed(uid):
        return MENU
    lang = _user_interface_lang(uid)
    if kind not in KINDS:
        context.user_data.pop("notification_kind", None)
        text = t(lang, "notifications_title")
        rows = [[t(lang, "notification_quiz"), t(lang, "notification_new_words")], [t(lang, "back")]]
    else:
        context.user_data["notification_kind"] = kind
        preference = get_preference(get_db_path(), uid, kind)
        text = "\n".join((t(lang, f"notification_{kind}"),
            t(lang, "notification_enabled" if preference.enabled else "notification_disabled"),
            f"{t(lang, 'notification_time')}: {preference.time}", t(lang, "notification_berlin")))
        toggle = "notification_disable" if preference.enabled else "notification_enable"
        rows = [[t(lang, toggle), t(lang, "notification_change_time")], [t(lang, "back")]]
    await _message(update).reply_text(text, reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True))
    return NOTIFICATION_SETTINGS_MENU


async def notification_settings_callback(update, context):
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass
    data = str(query.data or "")
    if data not in {f"notify:{kind}" for kind in KINDS}:
        return MENU
    return await show_notification_settings(update, context, data.split(":", 1)[1])


async def notification_menu_input(update, context):
    uid = update.effective_user.id
    if not access_allowed(uid):
        return MENU
    text = update.message.text.strip()
    kind = context.user_data.get("notification_kind")
    if _matches(text, "back"):
        if kind in KINDS:
            return await show_notification_settings(update, context)
        await update.message.reply_text(t(_user_interface_lang(uid), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(uid), resize_keyboard=True))
        return SETTINGS_MENU
    for candidate in KINDS:
        if _matches(text, f"notification_{candidate}"):
            return await show_notification_settings(update, context, candidate)
    if kind in KINDS:
        if _matches(text, "notification_enable") or _matches(text, "notification_disable"):
            set_preference(get_db_path(), uid, kind, enabled=_matches(text, "notification_enable"))
        elif _matches(text, "notification_change_time"):
            lang = _user_interface_lang(uid)
            await update.message.reply_text(t(lang, "notification_time_prompt"),
                reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True))
            return NOTIFICATION_SETTINGS_TIME
    return await show_notification_settings(update, context, kind)


async def notification_time_input(update, context):
    uid = update.effective_user.id
    if not access_allowed(uid):
        return MENU
    kind = context.user_data.get("notification_kind")
    if kind not in KINDS:
        return await show_notification_settings(update, context)
    text = update.message.text or ""
    if _matches(text.strip(), "back"):
        return await show_notification_settings(update, context, kind)
    parsed = parse_time(text)
    if parsed is None:
        await update.message.reply_text(t(_user_interface_lang(uid), "notification_time_invalid"))
        return NOTIFICATION_SETTINGS_TIME
    set_preference(get_db_path(), uid, kind, time=parsed)
    return await show_notification_settings(update, context, kind)
