"""Spec #13 — rescue handler: conversation state lives only in memory
(`main.py`, no `PicklePersistence` — restoring mid-quiz across a code update is
a source of quiet bugs, so we deliberately don't). After a bot restart (or any
chat whose state was otherwise lost) the ConversationHandler has no matching
state and the text isn't one of its entry_points (/start, /undo) — so old
reply-keyboard button presses go silent.

This handler must be registered LAST, in the SAME handler group as the main
ConversationHandler and every CallbackQueryHandler (`bot/main.py`). PTB runs at
most one handler per group (the first whose `check_update` matches), so as
long as this one is added after everything else, it only ever fires when
nothing else claimed the update: a genuinely stale/unmatched text message,
never a live conversation turn (every state's handler is a `TEXT & ~COMMAND`
catch-all, so a live conversation always matches first and wins).
"""

from telegram import Update
from telegram.ext import ContextTypes

from ..common.lang import _user_interface_lang
from ..handlers.access import access_allowed
from ..handlers.menu import _guest_start
from ..i18n import t
from ..ui.keyboards import _main_menu_markup


async def rescue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Soft auto-/start for a user whose conversation state was lost: an
    approved user (or the owner) gets the main menu + a short heads-up;
    anyone else goes through the normal onboarding/access-request flow."""
    user_id = update.effective_user.id
    if access_allowed(user_id):
        lang = _user_interface_lang(user_id)
        await update.message.reply_text(t(lang, "rescue_menu"), reply_markup=_main_menu_markup(user_id))
        return
    await _guest_start(update, context)
