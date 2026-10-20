"""P3.2 — owner-gating of menu items (keyboards).

Checks that owner-only buttons (add_word, parse_VF, dump, unit_tests, card
edit/delete) are hidden from a guest and shown to the owner. telegram is stubbed
so the test runs in an environment without PTB installed.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Stub telegram before importing keyboards (PTB isn't installed in the test env).
if "telegram" not in sys.modules:
    tg = types.ModuleType("telegram")

    class _RKM:
        def __init__(self, rows, **kwargs):
            self.rows = rows

    tg.ReplyKeyboardMarkup = _RKM
    for _n in ("ReplyKeyboardRemove", "Update", "InlineKeyboardButton", "InlineKeyboardMarkup"):
        setattr(tg, _n, type(_n, (), {"__init__": lambda self, *a, **k: None}))
    err = types.ModuleType("telegram.error")
    for _n in ("NetworkError", "RetryAfter", "TimedOut"):
        setattr(err, _n, type(_n, (Exception,), {}))
    ext = types.ModuleType("telegram.ext")
    ext.ContextTypes = type("C", (), {"DEFAULT_TYPE": object})
    ext.ConversationHandler = type("CH", (), {"END": -1})
    sys.modules["telegram"] = tg
    sys.modules["telegram.error"] = err
    sys.modules["telegram.ext"] = ext

from bot.storage import init_db  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


def _flat(rows):
    return [cell for row in rows for cell in row]


class OwnerGatingTest(unittest.TestCase):
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

    def test_main_menu_add_word_owner_only(self):
        # Accepted temporary default: Add is hidden from both roles.
        owner = _flat(K._main_menu_keys(OWNER))
        guest = _flat(K._main_menu_keys(GUEST))
        self.assertFalse(any("Add" in x or "Добав" in x or "Додат" in x for x in owner))
        self.assertFalse(any("Add" in x or "Добав" in x or "Додат" in x for x in guest))
        # Enabling only this preserved internal path still respects owner rights.
        with patch("bot.common.add_words_gate.ADD_WORDS_ENABLED", True):
            owner = _flat(K._main_menu_keys(OWNER))
            guest = _flat(K._main_menu_keys(GUEST))
            self.assertTrue(any("Add" in x or "Добав" in x or "Додат" in x for x in owner))
            self.assertFalse(any("Add" in x or "Добав" in x or "Додат" in x for x in guest))

    def test_settings_menu_dump_owner_only(self):
        owner = _flat(K._settings_menu_keys(OWNER))
        guest = _flat(K._settings_menu_keys(GUEST))
        # bug #27: parse_VF (proxy mini-menu) needs a visible OWNER entry — the
        # parsing itself lives in the Add submenu, but the proxy settings would
        # otherwise be reachable only by typing the command.
        self.assertIn("parse_VF", owner)
        self.assertNotIn("parse_VF", guest)
        self.assertTrue(any("dump" in x for x in owner))
        self.assertFalse(any("dump" in x for x in guest))
        # algo settings are available to a guest
        self.assertTrue(any("algo" in x.lower() or "алгоритм" in x.lower() for x in guest))

    def test_algo_menu_unit_tests_owner_only(self):
        owner = _flat(K._algo_settings_menu_keys(OWNER))
        guest = _flat(K._algo_settings_menu_keys(GUEST))
        self.assertTrue(any("unit" in x for x in owner))
        self.assertFalse(any("unit" in x for x in guest))

    def test_clarify_card_guest_can_edit_media_report_but_not_delete(self):
        # P4.3/P6: on the unified card a guest gets image/example + report, never
        # delete; the owner gets delete (deleting a shared word stays owner-only).
        owner = _flat(K._clarify_fields_markup("quiz", OWNER).rows)
        guest = _flat(K._clarify_fields_markup("quiz", GUEST).rows)
        self.assertIn("delete", owner)
        self.assertNotIn("delete", guest)
        self.assertIn("image", guest)
        self.assertIn("example", guest)
        self.assertTrue(any("ошибка" in x or "помилка" in x or "report" in x for x in guest))

    def test_clarify_card_guest_restricted_to_media(self):
        # P4.3: a guest may only edit image/example (+ report/back); the owner gets
        # the full grid (translations, article, type, etc.).
        guest = _flat(K._clarify_fields_markup("quiz", GUEST).rows)
        for f in ("ru", "article", "type", "plural", "prateritum"):
            self.assertNotIn(f, guest)
        owner = _flat(K._clarify_fields_markup("quiz", OWNER).rows)
        self.assertIn("article", owner)

    def test_send_learn_on_card_only_in_search(self):
        # P6: send-to-learning on the card appears only when opened from db_search
        # (in a quiz it's already on the quiz keyboard).
        quiz = _flat(K._clarify_fields_markup("quiz", GUEST, in_search=False).rows)
        search = _flat(K._clarify_fields_markup("quiz", GUEST, in_search=True).rows)
        self.assertFalse(any("учить" in x or "вчити" in x or "send_learn" in x for x in quiz))
        self.assertTrue(any("учить" in x or "вчити" in x or "send_learn" in x for x in search))


if __name__ == "__main__":
    unittest.main()
