"""P6.2 — the report/fix button is present on the clarify card for everyone."""

import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.storage import init_db  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


def _flat(markup):
    return [c for row in markup.rows for c in row]


class ReportButtonTest(unittest.TestCase):
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

    def test_report_button_on_card_for_owner_and_guest(self):
        # en label for report_card.
        self.assertIn("report / fix", _flat(K._clarify_root_markup(OWNER)))
        self.assertIn("report / fix", _flat(K._clarify_root_markup(GUEST)))

    def test_guest_card_has_no_delete(self):
        self.assertNotIn("delete", _flat(K._clarify_root_markup(GUEST)))
        self.assertIn("delete", _flat(K._clarify_root_markup(OWNER)))


if __name__ == "__main__":
    unittest.main()
