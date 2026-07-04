"""spec #3: passive conjugation drill (carousel).

The user is shown a verb's conjugation table and scrolls it "in their head" —
there is no input and no right/wrong. "next" (далее) counts the current card as
seen (progress++) and shows the next verb; "stop" (стоп) leaves with a per-session
count. Selection + weighted-rotation order live in storage.next_conjugation_word;
the card layout in ui.formatting._format_conjugation_card. This module only drives
the conversation loop (state CONJUGATION_MENU).
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import CONJUGATION_MENU, get_db_path
from ..common.lang import _mode_l1_de, _user_dict_lang, _user_interface_lang
from ..common.navigation import (
    _action_key_from_text,
    _back_to_tests,
    _reply_text_with_retry,
)
from ..i18n import t
from ..storage import next_conjugation_word, record_conjugation_shown
from ..ui.formatting import _format_conjugation_card

# context.user_data keys for the active carousel
_CURRENT_ID = "conjugation_current_id"
_SESSION_COUNT = "conjugation_session_count"


def _carousel_markup(user_id):
    """[ next | stop ] keyboard, labels localized to the user's interface language."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup(
        [[t(lang, "conjugation_next"), t(lang, "conjugation_stop")]],
        resize_keyboard=True,
    )


async def _show_card(update: Update, context: ContextTypes.DEFAULT_TYPE, entry):
    """Render the current verb's card and remember which word is on screen."""
    user_id = update.effective_user.id
    context.user_data[_CURRENT_ID] = entry.get("id")
    await _reply_text_with_retry(
        update.message,
        _format_conjugation_card(entry, _user_dict_lang(user_id)),
        parse_mode="Markdown",
        reply_markup=_carousel_markup(user_id),
    )


async def _leave_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report "this session: N" and return to the tests menu."""
    user_id = update.effective_user.id
    count = context.user_data.pop(_SESSION_COUNT, 0)
    context.user_data.pop(_CURRENT_ID, None)
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
    entry = next_conjugation_word(get_db_path(), user_id, _mode_l1_de(user_id))
    if entry is None:
        await _reply_text_with_retry(
            update.message, t(_user_interface_lang(user_id), "conjugation_empty")
        )
        return await _back_to_tests(update, context)
    await _show_card(update, context, entry)
    return CONJUGATION_MENU


async def conjugation_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a press inside the carousel: next / stop / anything else."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    lang = _user_interface_lang(user_id)

    # "stop" (dedicated label) and the generic back/stop buttons all leave.
    if text == t(lang, "conjugation_stop") or _action_key_from_text(text, user_id) in ("stop", "back"):
        return await _leave_with_summary(update, context)

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
