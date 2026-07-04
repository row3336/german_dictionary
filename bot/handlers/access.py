"""Guest access-approval flow (P3.1): owner request + inline decision + reject screen.

A guest is registered as 'pending'; the owner gets an instant message with inline
✅/❌ buttons. Until a request is handled, a background job (jobs/access_reminders.py)
reminds the owner with a [show requests] button (spec #5) that opens an inline,
actionable pending list → user card → approve/reject. The owner's decision edits
the message to the outcome and drops the buttons. (The old #user_confirmation_*
hashtag search + show/hide toggle were removed in spec #5.)

Leaf module: imports only app_config/lang/i18n/storage — never settings/menu, so
those can import it without cycles.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ..app_config import (
    ACCESS_CALLBACK_PREFIX,
    ACCESS_MESSAGE,
    ACCESS_REJECTED,
    START_TEXT,
    get_db_path,
    get_owner_id,
    is_owner,
)
from ..common.lang import _user_interface_lang
from ..i18n import t
from ..storage import get_user_info, get_user_status, list_pending_users, set_user_status

logger = logging.getLogger(__name__)


def access_allowed(user_id):
    """Whether the user may use the bot: owner or an approved guest."""
    return is_owner(user_id) or get_user_status(get_db_path(), user_id) == "approved"


def _owner_lang():
    """Owner's interface language — for owner-facing messages of the access flow."""
    return _user_interface_lang(get_owner_id())


def _user_tag(info):
    """Human-readable user label: @username, else first name, else id."""
    if not info:
        return "?"
    if info.get("username"):
        return "@" + info["username"]
    return info.get("first_name") or str(info.get("user_id") or "?")


def _start_keyboard():
    """Reply keyboard with a single Start button (re-enter the conversation)."""
    return ReplyKeyboardMarkup([[START_TEXT]], resize_keyboard=True)


def _matches(text, user_id, key):
    """Whether the button text matches key in the user's language or in EN."""
    return text in {t(_user_interface_lang(user_id), key), t("en", key)}


async def notify_owner_access_request(context: ContextTypes.DEFAULT_TYPE, user_id):
    """Send the owner an instant guest access request with inline ✅/❌.

    spec #5: the instant request stays (fast handling) but the
    #user_confirmation_request tag is gone — pending requests are searched via the
    [show requests] button now, not a hashtag.
    """
    owner_id = get_owner_id()
    if not owner_id:
        return
    info = get_user_info(get_db_path(), user_id)
    lang = _owner_lang()
    text = f"👤 {_user_tag(info)} (id {user_id}) {t(lang, 'access_request_owner')}."
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
        logger.warning("Failed to send the access request to the owner", exc_info=True)


async def _finalize_owner_message(query, outcome_text):
    """Replace the request text with the outcome and drop the buttons (spec #5: no
    #user_confirmation_done tag — the tag-search mechanism is gone)."""
    try:
        await query.edit_message_text(outcome_text, reply_markup=None)
    except Exception:
        # If editing the text fails, at least strip the inline buttons.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.warning("Failed to update the request message", exc_info=True)


async def _notify_guest(context: ContextTypes.DEFAULT_TYPE, user_id, key):
    """Tell the guest the outcome (in their language) + show a Start button to re-enter."""
    lang = _user_interface_lang(user_id)
    try:
        await context.bot.send_message(chat_id=user_id, text=t(lang, key), reply_markup=_start_keyboard())
    except Exception:
        logger.warning("Failed to notify the guest about the access decision", exc_info=True)


# spec #5: the #user_confirmation_request tag + show/hide toggle are gone. The
# digest instead carries a [show requests] button that opens an inline, actionable
# pending list -> user card -> approve/reject (editing the same message in place).
ACCESS_LIST = f"{ACCESS_CALLBACK_PREFIX}:list"


def pending_requests_button(lang):
    """[show requests] button attached to the access digest / review menu."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "access_show_requests"), callback_data=ACCESS_LIST)]]
    )


def _pending_list_view(lang):
    """(text, markup) for the inline pending-requests list (owner-only)."""
    pending = list_pending_users(get_db_path())
    if not pending:
        return t(lang, "access_no_pending"), InlineKeyboardMarkup([])
    lines = [f"⏰ {len(pending)} {t(lang, 'access_reminder_owner')}:"]
    rows = []
    for i, user in enumerate(pending, 1):
        lines.append(f"{i}) {_user_tag(user)} (id {user['user_id']})")
        rows.append([InlineKeyboardButton(
            f"{i}) {_user_tag(user)}", callback_data=f"{ACCESS_CALLBACK_PREFIX}:item:{user['user_id']}"
        )])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _request_card_view(lang, uid):
    """(text, markup) for a single pending user's card (approve / reject / back)."""
    info = get_user_info(get_db_path(), uid)
    text = f"{_user_tag(info)} (id {uid})\n{t(lang, 'access_request_owner')}"
    rows = [[
        InlineKeyboardButton(t(lang, "access_approve_btn"), callback_data=f"{ACCESS_CALLBACK_PREFIX}:approve:{uid}"),
        InlineKeyboardButton(t(lang, "access_reject_btn"), callback_data=f"{ACCESS_CALLBACK_PREFIX}:reject:{uid}"),
    ], [InlineKeyboardButton("↩", callback_data=ACCESS_LIST)]]
    return text, InlineKeyboardMarkup(rows)


async def access_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner inline access actions: show list / open card / approve / reject."""
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return
    data = query.data or ""
    lang = _owner_lang()
    if data == ACCESS_LIST:
        text, markup = _pending_list_view(lang)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            logger.debug("access list edit skipped", exc_info=True)
        return
    try:
        _, action, raw_uid = data.split(":")
        uid = int(raw_uid)
    except (ValueError, AttributeError):
        return
    if action == "item":
        text, markup = _request_card_view(lang, uid)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            logger.debug("access card edit skipped", exc_info=True)
        return
    tag = _user_tag(get_user_info(get_db_path(), uid))
    status = get_user_status(get_db_path(), uid)
    if status != "pending":
        # Stale/duplicate button: the request is already handled — just drop the buttons.
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
    """Reject screen: "message the developer" / "cancel" (state ACCESS_REJECTED)."""
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
    """Drop the guest back to the Start button (end the conversation)."""
    await update.message.reply_text(START_TEXT, reply_markup=_start_keyboard())
    return ConversationHandler.END


async def access_rejected_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject-screen choice: message the developer, or cancel."""
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
    """Developer-message input: relay to the owner, then back to the reject screen."""
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
            logger.warning("Failed to relay the guest message to the owner", exc_info=True)
    await update.message.reply_text(t(_user_interface_lang(user_id), "access_msg_sent"))
    return await show_rejected_screen(update, user_id)
