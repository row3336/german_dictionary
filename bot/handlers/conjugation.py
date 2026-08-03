"""spec #3: passive conjugation drill (carousel) + active-recall extras.

The user is shown a verb's conjugation table and scrolls it "in their head" —
there is no input and no right/wrong. "next" (далее) counts the current card as
seen (progress++) and shows the next verb; "stop" (стоп) leaves with a per-session
count. Selection + weighted-rotation order live in storage.next_conjugation_word;
the card layout in ui.formatting._format_conjugation_card. This module drives the
conversation loop (state CONJUGATION_MENU).

Two active-recall aids:
  * 🎲 dice — a reply-keyboard button (stays at the bottom, easy to reach). On
    each tap the bot DELETES the user's tap-message (so the chat/table never
    scrolls and no dice-emoji animation piles up) and shows a random personal
    pronoun in one dedicated message — sent on the first roll, edited in place
    afterwards ("🎲 ich ?" → "🎲 er/sie/es ?" …). A pronoun never repeats more
    than once per 3 shows, so all six persons get covered. The button label is
    "🎲 <word>", not a bare 🎲, so nothing animates.
  * hide/show — an inline button on the card masks every form as ``***`` and back.
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from ..app_config import CONJUGATION_MENU, get_db_path
from ..common.conjugation import pick_dice_pronoun
from ..common.lang import _mode_l1_de, _user_dict_lang, _user_interface_lang
from ..common.navigation import (
    _action_key_from_text,
    _back_to_tests,
    _reply_text_with_retry,
)
from ..i18n import t
from ..storage import next_conjugation_word, record_conjugation_shown
from ..ui.formatting import _format_conjugation_card

logger = logging.getLogger(__name__)

# context.user_data keys for the active carousel.
_CURRENT_ID = "conjugation_current_id"
_SESSION_COUNT = "conjugation_session_count"
# {card_message_id: {"entry": <word>, "masked": bool}} for the card on screen, so
# the inline hide toggle re-renders the right verb (and only the current card).
_CARD_ENTRIES = "conjugation_card_entries"
# The single dice message id + the last two pronouns shown (no-repeat-within-3).
# Reset whenever a new card is shown or the drill ends.
_DICE_MSG_ID = "conjugation_dice_msg_id"
_DICE_RECENT = "conjugation_dice_recent"


def _carousel_markup(user_id):
    """[ next | 🎲 ] / [ stop ] reply keyboard, labels localized to the interface language."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup(
        [
            [t(lang, "conjugation_next"), t(lang, "conjugation_dice")],
            [t(lang, "conjugation_stop")],
        ],
        resize_keyboard=True,
    )


def _hide_markup(user_id, masked):
    """Inline hide/show toggle on the forms card."""
    lang = _user_interface_lang(user_id)
    key = "conjugation_show" if masked else "conjugation_hide"
    data = "conj:show" if masked else "conj:hide"
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, key), callback_data=data)]])


def _reset_dice(context):
    """Forget the live dice message / history (a new card or leaving starts fresh)."""
    context.user_data.pop(_DICE_MSG_ID, None)
    context.user_data.pop(_DICE_RECENT, None)


async def _strip_card_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove the inline hide toggle from the previous card(s): once the carousel
    advances (or the drill ends) the worked-through card must lose its button, so
    only the card currently on screen stays interactive (owner, 2026-07-07)."""
    entries = context.user_data.get(_CARD_ENTRIES) or {}
    for mid in entries:
        if mid is None:
            continue
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id, message_id=mid, reply_markup=None
            )
        except Exception:
            # Message gone/already stripped/unchanged — nothing to clean up.
            logger.debug("stripping stale conjugation card button failed", exc_info=True)


async def _show_card(update: Update, context: ContextTypes.DEFAULT_TYPE, entry):
    """Render the current verb's card (with the inline hide toggle) and remember it."""
    user_id = update.effective_user.id
    await _strip_card_buttons(update, context)  # the previous card loses its button
    context.user_data[_CURRENT_ID] = entry.get("id")
    _reset_dice(context)  # dice belongs to the card on screen; start it fresh
    sent = await _reply_text_with_retry(
        update.message,
        _format_conjugation_card(entry, _user_dict_lang(user_id)),
        parse_mode="Markdown",
        reply_markup=_hide_markup(user_id, masked=False),
    )
    mid = getattr(sent, "message_id", None)
    context.user_data[_CARD_ENTRIES] = {mid: {"entry": entry, "masked": False}} if mid is not None else {}


async def _leave_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report "this session: N" and return to the tests menu."""
    user_id = update.effective_user.id
    await _strip_card_buttons(update, context)  # last card loses its button on the way out
    count = context.user_data.pop(_SESSION_COUNT, 0)
    context.user_data.pop(_CURRENT_ID, None)
    context.user_data.pop(_CARD_ENTRIES, None)
    _reset_dice(context)
    await _reply_text_with_retry(
        update.message,
        t(_user_interface_lang(user_id), "conjugation_session").format(count=count),
    )
    return await _back_to_tests(update, context)


async def start_conjugation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry from the tests menu: begin a fresh carousel or bail if the pool is empty."""
    user_id = update.effective_user.id
    context.user_data[_SESSION_COUNT] = 0
    context.user_data.pop(_CURRENT_ID, None)
    context.user_data.pop(_CARD_ENTRIES, None)
    _reset_dice(context)
    entry = next_conjugation_word(get_db_path(), user_id, _mode_l1_de(user_id))
    if entry is None:
        await _reply_text_with_retry(
            update.message, t(_user_interface_lang(user_id), "conjugation_empty")
        )
        return await _back_to_tests(update, context)
    # Prime the persistent далее/🎲/стоп reply keyboard once (the card carries its
    # own inline hide toggle, so the reply keyboard rides a separate message).
    lang = _user_interface_lang(user_id)
    await _reply_text_with_retry(
        update.message, t(lang, "conjugation"), reply_markup=_carousel_markup(user_id)
    )
    await _show_card(update, context, entry)
    return CONJUGATION_MENU


async def _roll_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the user's tap-message, then show a random pronoun in the single dice
    message — sent on the first roll, edited in place afterwards. Deleting the tap
    keeps the forms card from scrolling away and avoids any dice-emoji animation."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        # Can't delete (rare) — the tap message just stays; not fatal.
        logger.debug("could not delete dice tap message", exc_info=True)

    recent = context.user_data.get(_DICE_RECENT) or []
    pronoun = pick_dice_pronoun(recent[-2:])  # exclude the last two shown
    context.user_data[_DICE_RECENT] = (recent + [pronoun])[-2:]
    text = t(_user_interface_lang(user_id), "conjugation_dice_prompt").format(pronoun=pronoun)

    dice_mid = context.user_data.get(_DICE_MSG_ID)
    if dice_mid is not None:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=dice_mid, text=text)
            return CONJUGATION_MENU
        except Exception:
            # Message gone/unchanged — fall through to send a fresh one.
            logger.debug("dice edit failed, sending a new dice message", exc_info=True)
    sent = await context.bot.send_message(chat_id=chat_id, text=text)
    context.user_data[_DICE_MSG_ID] = getattr(sent, "message_id", None)
    return CONJUGATION_MENU


async def conjugation_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a press inside the carousel: next / 🎲 / stop / anything else."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    lang = _user_interface_lang(user_id)

    # "stop" (dedicated label) and the generic back/stop buttons all leave.
    if text == t(lang, "conjugation_stop") or _action_key_from_text(text, user_id) in ("stop", "back"):
        return await _leave_with_summary(update, context)

    if _action_key_from_text(text, user_id) == "conjugation_dice":
        return await _roll_dice(update, context)

    if text == t(lang, "conjugation_next"):
        # "next" counts the card currently on screen, then advances.
        current_id = context.user_data.get(_CURRENT_ID)
        if current_id:
            record_conjugation_shown(get_db_path(), user_id, current_id)
            context.user_data[_SESSION_COUNT] = context.user_data.get(_SESSION_COUNT, 0) + 1
        entry = next_conjugation_word(get_db_path(), user_id, _mode_l1_de(user_id))
        if entry is None:
            # Pool went empty (e.g. the only verb lost eligibility mid-session).
            return await _leave_with_summary(update, context)
        await _show_card(update, context, entry)
        return CONJUGATION_MENU

    # Unrecognized input: re-show the carousel keyboard without advancing.
    await _reply_text_with_retry(
        update.message,
        t(lang, "conjugation_next") + " / " + t(lang, "conjugation_stop"),
        reply_markup=_carousel_markup(user_id),
    )
    return CONJUGATION_MENU


async def conjugation_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle the card's forms hidden/shown in place (inline ^conj:hide / ^conj:show)."""
    query = update.callback_query
    user_id = query.from_user.id
    entries = context.user_data.get(_CARD_ENTRIES) or {}
    state = entries.get(query.message.message_id)
    if state is None:
        # Stale card (carousel already moved on) — acknowledge, nothing to redraw.
        await query.answer()
        return

    state["masked"] = (query.data or "") == "conj:hide"
    await query.answer()
    try:
        await query.edit_message_text(
            _format_conjugation_card(state["entry"], _user_dict_lang(user_id), mask=state["masked"]),
            parse_mode="Markdown",
            reply_markup=_hide_markup(user_id, masked=state["masked"]),
        )
    except Exception:
        # Identical content (double-tap) or an edit race — safe to ignore.
        logger.debug("conjugation hide toggle edit skipped", exc_info=True)
