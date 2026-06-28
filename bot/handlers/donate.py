"""P6.4 — support the project via Telegram Stars (XTR). Requires PTB 21.x.

XTR invoices need no payment provider (provider_token=""). Flow: a guest taps
"support project" in Settings → the bot sends a Stars invoice → Telegram raises a
PreCheckoutQuery (we approve) → on SuccessfulPayment we thank the user.

Verification note: the donation can only be exercised by a real Stars payment on
the server (no test env / no telegram in the dev env), so this is written against
the PTB 21 API but not run locally.
"""

from telegram import LabeledPrice, Update
from telegram.ext import ContextTypes

from ..app_config import SETTINGS_MENU
from ..common.lang import _user_interface_lang
from ..i18n import t

# Default donation size in Telegram Stars.
_STARS_AMOUNT = 50


async def start_donate(update: Update, user_id):
    """Send a Telegram Stars (XTR) invoice; the user stays in Settings."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_invoice(
        title=t(lang, "donate_title"),
        description=t(lang, "donate_desc"),
        payload="donate_stars",
        provider_token="",  # empty for Telegram Stars (XTR)
        currency="XTR",
        prices=[LabeledPrice(t(lang, "donate_label"), _STARS_AMOUNT)],
    )
    return SETTINGS_MENU


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve the pre-checkout (a donation has nothing to validate)."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Thank the user after a successful Stars payment."""
    lang = _user_interface_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "donate_thanks"))
