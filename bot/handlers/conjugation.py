"""spec #3: passive conjugation drill (carousel) + active-recall extras.

The user is shown a verb's conjugation table and scrolls it "in their head" —
there is no input and no right/wrong. "next" (далее) counts the current card as
seen (progress++) and shows the next verb; "stop" (стоп) leaves with a per-session
count. Selection + weighted-rotation order live in storage.next_conjugation_word;
the card layout in ui.formatting._format_conjugation_card. This module drives the
conversation loop (state CONJUGATION_MENU).

Two active-recall aids sit on top of the passive carousel:
  * 🎲 dice (reply-keyboard button) — pops a single random personal pronoun so the
    user recites that form from memory; re-pressing edits the same message in place.
  * "hide forms" (inline button under the card) — masks every form as ``***`` in
    place, toggling back to reveal. Both edit rather than spam new messages.

The dice/hide edits use context.bot / callback_query; the carousel далее/🎲/стоп
buttons are a persistent reply keyboard set once per session (it "stays below"
while each card carries its own inline hide toggle — the two keyboard kinds
coexist on separate messages).
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
# {message_id: entry} of the card currently on screen, so the inline hide toggle
# can re-render the right verb even after the carousel advanced.
_CARD_ENTRIES = "conjugation_card_entries"
# Message id of the live 🎲 dice message + the pronoun it last showed (avoid an
# immediate repeat). Both reset whenever a new card is shown or the drill ends.
_DICE_MSG_ID = "conjugation_dice_msg_id"
_DICE_LAST = "conjugation_dice_last"


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
    """Inline toggle under the card: offers "hide" while shown, "show" while masked."""
    lang = _user_interface_lang(user_id)
    if masked:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(lang, "conjugation_show"), callback_data="conj:show")]]
        )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "conjugation_hide"), callback_data="conj:hide")]]
    )


def _reset_dice(context):
    """Forget the live dice message (a new card / leaving starts a fresh roll)."""
    context.user_data.pop(_DICE_MSG_ID, None)
    context.user_data.pop(_DICE_LAST, None)


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
    await _strip_card_buttons(update, context)  # the previous card loses its toggle
    context.user_data[_CURRENT_ID] = entry.get("id")
    _reset_dice(context)
    sent = await _reply_text_with_retry(
        update.message,
        _format_conjugation_card(entry, _user_dict_lang(user_id)),
        parse_mode="Markdown",
        reply_markup=_hide_markup(user_id, masked=False),
    )
    mid = getattr(sent, "message_id", None)
    # Only the current card is toggle-able: previous cards had their button stripped
    # above, and the callback still no-ops on any message not in this dict (defense).
    context.user_data[_CARD_ENTRIES] = {mid: entry} if mid is not None else {}


async def _leave_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report "this session: N" and return to the tests menu."""
    user_id = update.effective_user.id
    await _strip_card_buttons(update, context)  # last card loses its toggle on the way out
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
    # Prime the persistent далее/🎲/стоп reply keyboard once (cards themselves carry
    # the inline hide toggle, so the reply keyboard has to ride a separate message).
    lang = _user_interface_lang(user_id)
    await _reply_text_with_retry(
        update.message, t(lang, "conjugation"), reply_markup=_carousel_markup(user_id)
    )
    await _show_card(update, context, entry)
    return CONJUGATION_MENU


async def _roll_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a random personal pronoun; edit the live dice message in place on re-press."""
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    pronoun = pick_dice_pronoun(context.user_data.get(_DICE_LAST))
    context.user_data[_DICE_LAST] = pronoun
    text = t(lang, "conjugation_dice_prompt").format(pronoun=pronoun)

    dice_mid = context.user_data.get(_DICE_MSG_ID)
    if dice_mid is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=dice_mid, text=text
            )
            return CONJUGATION_MENU
        except Exception:
            # Message gone/unchanged — fall back to sending a fresh one below.
            logger.debug("dice edit failed, sending new message", exc_info=True)

    sent = await _reply_text_with_retry(update.message, text)
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
    entry = entries.get(query.message.message_id)
    if entry is None:
        # Stale card (carousel already moved on) — acknowledge, nothing to redraw.
        await query.answer()
        return

    masked = (query.data or "") == "conj:hide"
    await query.answer()
    try:
        await query.edit_message_text(
            _format_conjugation_card(entry, _user_dict_lang(user_id), mask=masked),
            parse_mode="Markdown",
            reply_markup=_hide_markup(user_id, masked=masked),
        )
    except Exception:
        # Identical content (double-tap) or an edit race — safe to ignore.
        logger.debug("conjugation hide toggle edit skipped", exc_info=True)
