"""Spec #13 — rescue handler: conversation state lives only in memory
(`main.py`, no `PicklePersistence` — restoring mid-quiz across a code update is
a source of quiet bugs, so we deliberately don't). After a bot restart (or any
chat whose state was otherwise lost) the ConversationHandler has no matching
state and the text isn't one of its entry_points (/start, /undo, the start
button) — so old reply-keyboard button presses go silent.

This handler is registered as the LAST **entry_point** of the main
ConversationHandler (`bot/main.py`), not a standalone handler. That matters:
entry_points only run when there is NO active conversation (so a live quiz turn
matches its own state handler and is never intercepted), and — crucially — the
value it RETURNS is the conversation state the user lands in. A standalone
handler's return value is ignored by PTB, which is why an earlier version left
the menu keyboard visible but dead: the user was never actually put into MENU,
so the next button press fell through to rescue again (loop) until an explicit
/start. Returning MENU (approved) / the onboarding state (guest) fixes that —
the first stale press shows a heads-up + enters the conversation, and every
press after is handled normally.
"""

from telegram import Update
from telegram.ext import ContextTypes

from ..app_config import MENU
from ..common.lang import _user_interface_lang
from ..handlers.access import access_allowed
from ..handlers.menu import _guest_start
from ..i18n import t
from ..ui.keyboards import _main_menu_markup


async def rescue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Soft auto-/start for a user whose conversation state was lost: an
    approved user (or the owner) gets the main menu + a short heads-up and lands
    back in MENU; anyone else goes through the normal onboarding/access-request
    flow (and the state it returns). The RETURN value is what re-enters the
    ConversationHandler, so it must be a real state — not None."""
    user_id = update.effective_user.id
    if access_allowed(user_id):
        lang = _user_interface_lang(user_id)
        await update.message.reply_text(t(lang, "rescue_menu"), reply_markup=_main_menu_markup(user_id))
        return MENU
    return await _guest_start(update, context)
