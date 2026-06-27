"""Поток одобрения доступа (P3.1): заявка владельцу + inline-решение + отказной экран.

Новая подсистема — первый и единственный в боте сценарий с inline-кнопками и
CallbackQueryHandler. Гость регистрируется как 'pending'; владелец получает
сообщение с хэштегом #user_confirmation_request и кнопками ✅/❌. Пока заявка не
обработана — фоновый джоб (jobs/access_reminders.py) шлёт владельцу напоминания.
Решение владельца редактирует исходное сообщение: снимает кнопки и меняет хэштег
на #user_confirmation_done, чтобы поиск по открытому хэштегу показывал только
необработанные заявки.

Лист-модуль: импортирует только app_config/lang/i18n/storage — без settings/menu,
чтобы те могли импортировать его без циклов.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ..app_config import (
    ACCESS_CALLBACK_PREFIX,
    ACCESS_DONE_HASHTAG,
    ACCESS_MESSAGE,
    ACCESS_REJECTED,
    ACCESS_REQUEST_HASHTAG,
    START_TEXT,
    get_db_path,
    get_owner_id,
    is_owner,
)
from ..common.lang import _user_interface_lang
from ..i18n import t
from ..storage import get_user_info, get_user_status, set_user_status

logger = logging.getLogger(__name__)


def access_allowed(user_id):
    """Пускать ли юзера в бот: владелец или одобренный гость (approved)."""
    return is_owner(user_id) or get_user_status(get_db_path(), user_id) == "approved"


def _owner_lang():
    """Язык интерфейса владельца — для owner-facing сообщений потока доступа."""
    return _user_interface_lang(get_owner_id())


def _user_tag(info):
    """Человекочитаемая подпись юзера: @username, либо имя, либо id."""
    if not info:
        return "?"
    if info.get("username"):
        return "@" + info["username"]
    return info.get("first_name") or str(info.get("user_id") or "?")


def _start_keyboard():
    """Reply-клавиатура с единственной кнопкой «Старт» (ре-вход в conversation)."""
    return ReplyKeyboardMarkup([[START_TEXT]], resize_keyboard=True)


def _matches(text, user_id, key):
    """Совпадает ли текст кнопки с ключом key в языке юзера или в EN."""
    return text in {t(_user_interface_lang(user_id), key), t("en", key)}


async def notify_owner_access_request(context: ContextTypes.DEFAULT_TYPE, user_id):
    """Отправить владельцу заявку гостя на доступ (хэштег + inline ✅/❌)."""
    owner_id = get_owner_id()
    if not owner_id:
        return
    info = get_user_info(get_db_path(), user_id)
    lang = _owner_lang()
    text = (
        f"👤 {_user_tag(info)} (id {user_id}) {t(lang, 'access_request_owner')}.\n\n"
        f"{ACCESS_REQUEST_HASHTAG}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            t(lang, "access_approve_btn"),
            callback_data=f"{ACCESS_CALLBACK_PREFIX}:approve:{user_id}",
        ),
        InlineKeyboardButton(
            t(lang, "access_reject_btn"),
            callback_data=f"{ACCESS_CALLBACK_PREFIX}:reject:{user_id}",
        ),
    ]])
    try:
        await context.bot.send_message(chat_id=owner_id, text=text, reply_markup=keyboard)
    except Exception:
        logger.warning("Не удалось отправить владельцу заявку на доступ", exc_info=True)


async def _finalize_owner_message(query, outcome_text):
    """Заменить текст заявки на исход + сменить хэштег на DONE и убрать кнопки."""
    try:
        await query.edit_message_text(f"{outcome_text}\n\n{ACCESS_DONE_HASHTAG}", reply_markup=None)
    except Exception:
        # Если правка текста не удалась — хотя бы снять inline-кнопки.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.warning("Не удалось обновить сообщение-заявку", exc_info=True)


async def _notify_guest(context: ContextTypes.DEFAULT_TYPE, user_id, key):
    """Сообщить гостю исход (на его языке) + показать кнопку «Старт» для ре-входа."""
    lang = _user_interface_lang(user_id)
    try:
        await context.bot.send_message(chat_id=user_id, text=t(lang, key), reply_markup=_start_keyboard())
    except Exception:
        logger.warning("Не удалось уведомить гостя об исходе доступа", exc_info=True)


async def access_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка inline-решения владельца (✅ разрешить / ❌ отклонить)."""
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return
    try:
        _, action, raw_uid = (query.data or "").split(":")
        uid = int(raw_uid)
    except (ValueError, AttributeError):
        return
    lang = _owner_lang()
    tag = _user_tag(get_user_info(get_db_path(), uid))
    status = get_user_status(get_db_path(), uid)
    if status != "pending":
        # Устаревшая/повторная кнопка: заявка уже обработана — просто снимаем кнопки.
        await _finalize_owner_message(query, f"{t(lang, 'access_already_handled')} ({status}) {tag}")
        return
    if action == "approve":
        set_user_status(get_db_path(), uid, "approved")
        await _finalize_owner_message(query, f"{t(lang, 'access_granted_owner')} {tag} (id {uid})")
        await _notify_guest(context, uid, "access_approved_notice")
    elif action == "reject":
        set_user_status(get_db_path(), uid, "rejected")
        await _finalize_owner_message(query, f"{t(lang, 'access_rejected_owner')} {tag} (id {uid})")
        await _notify_guest(context, uid, "access_rejected")


async def show_rejected_screen(update: Update, user_id):
    """Экран отказа: «написать разработчику» / «отмена» (состояние ACCESS_REJECTED)."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_text(
        t(lang, "access_rejected"),
        reply_markup=ReplyKeyboardMarkup(
            [[t(lang, "access_contact_dev")], [t(lang, "access_cancel")]],
            resize_keyboard=True,
        ),
    )
    return ACCESS_REJECTED


async def _back_to_start(update: Update):
    """Выкинуть гостя обратно на кнопку «Старт» (завершить conversation)."""
    await update.message.reply_text(START_TEXT, reply_markup=_start_keyboard())
    return ConversationHandler.END


async def access_rejected_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор на экране отказа: написать разработчику или отмена."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if _matches(text, user_id, "access_contact_dev"):
        lang = _user_interface_lang(user_id)
        await update.message.reply_text(
            t(lang, "access_msg_prompt"),
            reply_markup=ReplyKeyboardMarkup([[t(lang, "access_cancel")]], resize_keyboard=True),
        )
        return ACCESS_MESSAGE
    if _matches(text, user_id, "access_cancel"):
        return await _back_to_start(update)
    return await show_rejected_screen(update, user_id)


async def access_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод сообщения разработчику: релей владельцу, затем назад на экран отказа."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if _matches(text, user_id, "access_cancel"):
        return await show_rejected_screen(update, user_id)
    owner_id = get_owner_id()
    if owner_id:
        info = get_user_info(get_db_path(), user_id)
        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"{t(_owner_lang(), 'access_msg_from_user')} {_user_tag(info)} (id {user_id}):\n\n{text}",
            )
        except Exception:
            logger.warning("Не удалось переслать владельцу сообщение от гостя", exc_info=True)
    await update.message.reply_text(t(_user_interface_lang(user_id), "access_msg_sent"))
    return await show_rejected_screen(update, user_id)
