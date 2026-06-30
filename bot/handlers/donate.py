"""P6.4 — support the project via Telegram Stars (XTR). Requires PTB 21.x.

XTR invoices need no payment provider (provider_token=""). Flow: a guest taps
"support project" in Settings → picks an amount (presets or a custom number) →
the bot sends a Stars invoice → Telegram raises a PreCheckoutQuery (we approve)
→ on SuccessfulPayment we thank the user.

Verification note: the donation can only be exercised by a real Stars payment on
the server (no test env / no telegram in the dev env), so this is written against
the PTB 21 API but not run locally.
"""

from telegram import LabeledPrice, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import DONATE_AMOUNT, DONATE_CUSTOM, SETTINGS_MENU, get_db_path
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..i18n import t
from ..ui.keyboards import _settings_menu_keys

# Quick-pick amounts in Telegram Stars; the user can also enter a custom amount.
_STAR_PRESETS = (25, 50, 100, 250)
# Guard the custom amount against zero/negative/garbage/absurd values.
_MAX_STARS = 100000


def _donate_amount_keyboard(lang):
    """Amount-selection keyboard: presets in one row, then custom + back."""
    presets = [f"⭐ {n}" for n in _STAR_PRESETS]
    return ReplyKeyboardMarkup(
        [presets, [t(lang, "donate_custom_btn"), t(lang, "back")]],
        resize_keyboard=True,
    )


def _parse_star_amount(text):
    """Star amount from a preset label ('⭐ 25') or a plain number; None if invalid.

    Digits are extracted so the same parser handles both the preset buttons and
    free-typed input; out-of-range (≤0 or > _MAX_STARS) yields None."""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        return None
    amount = int(digits)
    return amount if 1 <= amount <= _MAX_STARS else None


async def start_donate(update: Update, user_id):
    """Offer the amount choice (presets + custom); enter the DONATE_AMOUNT state."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_text(
        t(lang, "donate_choose_amount"),
        reply_markup=_donate_amount_keyboard(lang),
    )
    return DONATE_AMOUNT


async def _send_invoice(update: Update, user_id, amount):
    """Send a Telegram Stars (XTR) invoice for `amount`, then restore Settings."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_invoice(
        title=t(lang, "donate_title"),
        description=t(lang, "donate_desc"),
        payload="donate_stars",
        provider_token="",  # empty for Telegram Stars (XTR)
        currency="XTR",
        prices=[LabeledPrice(t(lang, "donate_label"), amount)],
    )
    # Restore the Settings keyboard so the amount keyboard doesn't linger.
    await update.message.reply_text(
        t(lang, "settings_title"),
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def donate_amount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the amount screen: preset → invoice, custom → prompt, back → Settings."""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back":
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU
    if text == t(lang, "donate_custom_btn"):
        await update.message.reply_text(
            t(lang, "donate_custom_prompt"),
            reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
        )
        return DONATE_CUSTOM
    amount = _parse_star_amount(text)
    if amount is not None:
        return await _send_invoice(update, user_id, amount)
    # Unrecognized — re-show the amount keyboard.
    await update.message.reply_text(
        t(lang, "donate_choose_amount"),
        reply_markup=_donate_amount_keyboard(lang),
    )
    return DONATE_AMOUNT


async def donate_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a typed custom amount: valid → invoice, back → Settings, else reprompt."""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back":
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU
    amount = _parse_star_amount(text)
    if amount is not None:
        return await _send_invoice(update, user_id, amount)
    await update.message.reply_text(
        t(lang, "donate_invalid_amount"),
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
    )
    return DONATE_CUSTOM


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve the pre-checkout (a donation has nothing to validate)."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Thank the user after a successful Stars payment."""
    lang = _user_interface_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "donate_thanks"))
