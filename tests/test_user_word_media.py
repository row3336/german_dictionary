"""P4 — personal example/image (user_word_media): storage helpers + overlay.

A guest's per-word image/example override is stored separately and overlaid on
read; the shared `words` row is never changed and other users / the owner are
unaffected.
"""

import os
import tempfile
import unittest

from bot.storage import (
    add_word,
    apply_user_word_media,
    get_user_word_media,
    init_db,
    set_user_word_media,
)

U1, U2 = 101, 202


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


class UserWordMediaTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.wid = add_word(
            self.db,
            {
                "type": "nomen",
                "article": "das",
                "german": "Haus",
                "russian": "дом",
                "image": "shared-img",
                "example": "shared example",
            },
        )["id"]

    def test_none_when_unset(self):
        self.assertEqual(get_user_word_media(self.db, U1, self.wid), {})

    def test_set_and_get(self):
        set_user_word_media(self.db, U1, self.wid, example="my example")
        self.assertEqual(
            get_user_word_media(self.db, U1, self.wid),
            {"image": "", "example": "my example"},
        )
        set_user_word_media(self.db, U1, self.wid, image="my-img")
        media = get_user_word_media(self.db, U1, self.wid)
        self.assertEqual(media["image"], "my-img")
        self.assertEqual(media["example"], "my example")  # earlier value kept

    def test_overlay_substitutes_only_personal_nonempty(self):
        entry = {"id": self.wid, "image": "shared-img", "example": "shared example"}
        # no personal -> unchanged
        self.assertEqual(apply_user_word_media(self.db, U1, entry)["example"], "shared example")
        set_user_word_media(self.db, U1, self.wid, example="personal ex")
        out = apply_user_word_media(self.db, U1, entry)
        self.assertEqual(out["example"], "personal ex")   # personal wins
        self.assertEqual(out["image"], "shared-img")        # image untouched (personal empty)
        self.assertEqual(entry["example"], "shared example")  # input not mutated

    def test_isolated_per_user_and_shared_unchanged(self):
        set_user_word_media(self.db, U1, self.wid, example="u1 only")
        entry = {"id": self.wid, "image": "shared-img", "example": "shared example"}
        self.assertEqual(apply_user_word_media(self.db, U1, entry)["example"], "u1 only")
        self.assertEqual(apply_user_word_media(self.db, U2, entry)["example"], "shared example")
        # owner/None user sees shared
        self.assertEqual(apply_user_word_media(self.db, None, entry)["example"], "shared example")

    def test_word_card_shows_personal_for_guest_only(self):
        # P4.2: the rendered card shows the user's personal example/image; others see shared.
        import os

        from bot.ui.formatting import _format_word_card

        os.environ["DB_PATH"] = self.db  # _format_word_card resolves the DB via get_db_path()
        self.addCleanup(lambda: os.environ.pop("DB_PATH", None))
        set_user_word_media(self.db, U1, self.wid, example="mnemonic ex", image="my-img")
        entry = {
            "id": self.wid, "type": "nomen", "german": "Haus",
            "image": "shared-img", "example": "shared example",
        }
        card_u1 = _format_word_card(entry, user_id=U1)
        self.assertIn("example: mnemonic ex", card_u1)
        self.assertIn("image: my-img", card_u1)
        card_u2 = _format_word_card(entry, user_id=U2)
        self.assertIn("example: shared example", card_u2)
        self.assertIn("image: shared-img", card_u2)


if __name__ == "__main__":
    unittest.main()
