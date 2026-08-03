"""P6.4 — Telegram Stars donation: amount choice (presets + custom) → XTR invoice.

Bug #16: the amount is no longer hardcoded — the guest picks a preset or types a
custom number, then an XTR invoice for that amount is sent. Handlers are stubbed
(no telegram / no payments in the dev env); LabeledPrice is replaced with a
capturing fake so the invoice amount is inspectable."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.donate as D  # noqa: E402
from bot.app_config import DONATE_AMOUNT, DONATE_CUSTOM, SETTINGS_MENU  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


class _LabeledPrice:
    """Capturing stand-in so the invoice amount is assertable."""

    def __init__(self, label, amount):
        self.label = label
        self.amount = amount


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.invoices = []
        self.replies = []

    async def reply_invoice(self, **kw):
        self.invoices.append(kw)

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _Update:
    def __init__(self, uid, text=""):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()


def _run(coro):
    return asyncio.run(coro)


class DonateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self._orig_lp = D.LabeledPrice
        D.LabeledPrice = _LabeledPrice
        self.addCleanup(self._restore)
        self.lang = D._user_interface_lang(GUEST)

    def _restore(self):
        D.LabeledPrice = self._orig_lp
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_support_button_guest_only(self):
        guest = [c for row in K._settings_menu_keys(GUEST) for c in row]
        owner = [c for row in K._settings_menu_keys(OWNER) for c in row]
        self.assertTrue(any("support project" in c for c in guest))
        self.assertFalse(any("support project" in c for c in owner))

    def test_parse_star_amount(self):
        self.assertEqual(D._parse_star_amount("⭐ 25"), 25)  # preset label
        self.assertEqual(D._parse_star_amount("50"), 50)     # plain number
        self.assertEqual(D._parse_star_amount("1"), 1)       # min
        self.assertEqual(D._parse_star_amount(str(D._MAX_STARS)), D._MAX_STARS)  # max
        for bad in ("0", str(D._MAX_STARS + 1), "abc", "", "Назад"):
            self.assertIsNone(D._parse_star_amount(bad), bad)

    def test_start_offers_amount_choice_without_invoice(self):
        upd = _Update(GUEST)
        state = _run(D.start_donate(upd, GUEST))
        self.assertEqual(state, DONATE_AMOUNT)
        self.assertEqual(upd.message.invoices, [])  # no invoice until amount picked
        labels = [c for row in D._donate_amount_keyboard(self.lang).rows for c in row]
        for n in (25, 50, 100, 250):
            self.assertIn(f"⭐ {n}", labels)
        self.assertIn(t(self.lang, "donate_custom_btn"), labels)
        self.assertIn(t(self.lang, "back"), labels)

    def test_preset_sends_xtr_invoice_for_that_amount(self):
        upd = _Update(GUEST, text="⭐ 100")
        state = _run(D.donate_amount_choice(upd, None))
        self.assertEqual(state, SETTINGS_MENU)
        self.assertEqual(len(upd.message.invoices), 1)
        inv = upd.message.invoices[0]
        self.assertEqual(inv["currency"], "XTR")
        self.assertEqual(inv["provider_token"], "")  # no provider for Stars
        self.assertEqual(inv["prices"][0].amount, 100)

    def test_custom_button_prompts_without_invoice(self):
        upd = _Update(GUEST, text=t(self.lang, "donate_custom_btn"))
        state = _run(D.donate_amount_choice(upd, None))
        self.assertEqual(state, DONATE_CUSTOM)
        self.assertEqual(upd.message.invoices, [])

    def test_back_from_amount_returns_to_settings(self):
        upd = _Update(GUEST, text=t(self.lang, "back"))
        state = _run(D.donate_amount_choice(upd, None))
        self.assertEqual(state, SETTINGS_MENU)
        self.assertEqual(upd.message.invoices, [])

    def test_custom_input_valid_sends_invoice(self):
        upd = _Update(GUEST, text="75")
        state = _run(D.donate_custom_input(upd, None))
        self.assertEqual(state, SETTINGS_MENU)
        self.assertEqual(upd.message.invoices[0]["prices"][0].amount, 75)

    def test_custom_input_invalid_reprompts(self):
        upd = _Update(GUEST, text="отправь много")
        state = _run(D.donate_custom_input(upd, None))
        self.assertEqual(state, DONATE_CUSTOM)
        self.assertEqual(upd.message.invoices, [])
        self.assertIn(t(self.lang, "donate_invalid_amount"), upd.message.replies)

    def test_custom_input_back_returns_to_settings(self):
        upd = _Update(GUEST, text=t(self.lang, "back"))
        state = _run(D.donate_custom_input(upd, None))
        self.assertEqual(state, SETTINGS_MENU)
        self.assertEqual(upd.message.invoices, [])

    def test_successful_payment_thanks(self):
        upd = _Update(GUEST)
        _run(D.successful_payment_callback(upd, None))
        self.assertTrue(any("Thank you" in r or "Спасибо" in r or "Дякую" in r for r in upd.message.replies))


if __name__ == "__main__":
    unittest.main()
