"""#17 — quiz-direction buttons carry the user's DICTIONARY language.

"Lang > De"/"De > Lang" placeholders are gone: the buttons now read "Ru > De",
"Ua > De" or "En > De" (and the reverse) per the user's dict language. Because
those constants also drove routing, navigation must map every direction variant
back to its action — so a press routes regardless of which dict language (or a
stale keyboard) produced the label.
"""

import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.common.lang import _quiz_dir_labels  # noqa: E402
from bot.common.navigation import _action_key_from_text  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_dict_lang  # noqa: E402
from bot.ui.keyboards import _tests_menu_keys  # noqa: E402

OWNER, GUEST = 999, 111
# dict language -> (L1>De label, De>L1 label). uk renders as "Ua" (existing UI convention).
CASES = {"ru": ("Ru > De", "De > Ru"), "uk": ("Ua > De", "De > Ua"), "en": ("En > De", "De > En")}


class QuizDirLabelsTest(unittest.TestCase):
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
        register_or_update_user(self.db, GUEST, "g", "G")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_labels_follow_dict_language(self):
        for dl, (l1de, del1) in CASES.items():
            set_user_dict_lang(self.db, GUEST, dl)
            with self.subTest(dict_lang=dl):
                self.assertEqual(_quiz_dir_labels(GUEST), (l1de, del1))
                flat = [c for row in _tests_menu_keys(GUEST) for c in row]
                self.assertIn(l1de, flat)
                self.assertIn(del1, flat)

    def test_routing_maps_every_direction_variant(self):
        # Whatever the guest's own dict language, ALL variants route correctly
        # (guards against a stale keyboard after a dict-language change).
        set_user_dict_lang(self.db, GUEST, "ru")
        for _dl, (l1de, del1) in CASES.items():
            self.assertEqual(_action_key_from_text(l1de, GUEST), "quiz_l1_de")
            self.assertEqual(_action_key_from_text(del1, GUEST), "quiz_de_l1")

    def test_old_placeholder_no_longer_routes(self):
        # The former neutral placeholders must not map to a quiz action anymore
        # (unmapped text yields a falsy action key).
        self.assertFalse(_action_key_from_text("Lang > De", GUEST))
        self.assertFalse(_action_key_from_text("De > Lang", GUEST))


if __name__ == "__main__":
    unittest.main()
