"""P6.4 — Telegram Stars donation: button + invoice/thanks (handlers, stubbed)."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.donate as D  # noqa: E402
from bot.app_config import SETTINGS_MENU  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self):
        self.invoices = []
        self.replies = []

    async def reply_invoice(self, **kw):
        self.invoices.append(kw)

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _Update:
    def __init__(self, uid):
        self.message = _Msg()
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
        self.addCleanup(self._restore)

    def _restore(self):
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

    def test_invoice_is_xtr(self):
        upd = _Update(GUEST)
        state = _run(D.start_donate(upd, GUEST))
        self.assertEqual(state, SETTINGS_MENU)
        self.assertEqual(len(upd.message.invoices), 1)
        inv = upd.message.invoices[0]
        self.assertEqual(inv["currency"], "XTR")
        self.assertEqual(inv["provider_token"], "")  # no provider for Stars

    def test_successful_payment_thanks(self):
        upd = _Update(GUEST)
        _run(D.successful_payment_callback(upd, None))
        self.assertTrue(any("Thank you" in r or "Спасибо" in r or "Дякую" in r for r in upd.message.replies))


if __name__ == "__main__":
    unittest.main()
