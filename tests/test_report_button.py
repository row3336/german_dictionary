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

    def test_report_button_guest_only_not_owner(self):
        # #20: the "report / fix" button is for guests (they report TO the owner);
        # the owner edits fields directly and must NOT see it.
        self.assertIn("report / fix", _flat(K._clarify_fields_markup("quiz", GUEST)))
        self.assertNotIn("report / fix", _flat(K._clarify_fields_markup("quiz", OWNER)))

    def test_guest_card_has_no_delete(self):
        self.assertNotIn("delete", _flat(K._clarify_fields_markup("quiz", GUEST)))
        self.assertIn("delete", _flat(K._clarify_fields_markup("quiz", OWNER)))

    def test_guest_quiz_has_clarify_entry(self):
        # P6.2/P6.3: a guest can now open the clarify card from quizzes too.
        self.assertIn("clarify_fields", _flat(K._quiz_markup(GUEST)))
        self.assertIn("clarify_fields", _flat(K._quiz_markup(OWNER)))
        self.assertIn("clarify_fields", _flat(K._weak_article_markup(GUEST)))


if __name__ == "__main__":
    unittest.main()
