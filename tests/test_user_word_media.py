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
    get_word_by_id,
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
            {"image": "", "example": "my example", "russian": "", "ukrainian": "", "english": ""},
        )
        set_user_word_media(self.db, U1, self.wid, image="my-img")
        media = get_user_word_media(self.db, U1, self.wid)
        self.assertEqual(media["image"], "my-img")
        self.assertEqual(media["example"], "my example")  # earlier value kept

    def test_guest_sees_blank_until_set_no_owner_fallback(self):
        # A guest never sees the owner's shared image/example; blank until they set their own.
        entry = {"id": self.wid, "image": "shared-img", "example": "shared example"}
        out = apply_user_word_media(self.db, U1, entry)
        self.assertEqual(out["example"], "")  # NOT "shared example"
        self.assertEqual(out["image"], "")
        self.assertEqual(entry["example"], "shared example")  # input not mutated
        set_user_word_media(self.db, U1, self.wid, example="personal ex")
        out = apply_user_word_media(self.db, U1, entry)
        self.assertEqual(out["example"], "personal ex")  # their own value
        self.assertEqual(out["image"], "")               # image still unset -> blank

    def test_isolated_per_user_no_leak(self):
        set_user_word_media(self.db, U1, self.wid, example="u1 only")
        entry = {"id": self.wid, "image": "shared-img", "example": "shared example"}
        self.assertEqual(apply_user_word_media(self.db, U1, entry)["example"], "u1 only")
        self.assertEqual(apply_user_word_media(self.db, U2, entry)["example"], "")  # blank, not u1's
        # user_id=None is a non-personalized context (e.g. owner-less) -> shared as-is
        self.assertEqual(apply_user_word_media(self.db, None, entry)["example"], "shared example")

    def test_two_guests_distinct_personals_no_overlap(self):
        # Mirrors the live P4 check (Himbeere with two personals): two guests set
        # DIFFERENT personal example/image on the same word; each sees only their
        # own, neither leaks, and the shared `words` row stays untouched.
        set_user_word_media(self.db, U1, self.wid, example="guest1 ex", image="g1-img")
        set_user_word_media(self.db, U2, self.wid, example="guest2 ex", image="g2-img")
        entry = {"id": self.wid, "image": "shared-img", "example": "shared example"}
        out1 = apply_user_word_media(self.db, U1, entry)
        out2 = apply_user_word_media(self.db, U2, entry)
        self.assertEqual((out1["example"], out1["image"]), ("guest1 ex", "g1-img"))
        self.assertEqual((out2["example"], out2["image"]), ("guest2 ex", "g2-img"))
        # the shared word row is never changed by guest writes
        shared = get_word_by_id(self.db, self.wid)
        self.assertEqual(shared["example"], "shared example")
        self.assertEqual(shared["image"], "shared-img")

    def test_owner_sees_shared(self):
        os.environ["OWNER_ID"] = "5000"
        self.addCleanup(lambda: os.environ.pop("OWNER_ID", None))
        entry = {"id": self.wid, "image": "shared-img", "example": "shared example"}
        out = apply_user_word_media(self.db, 5000, entry)  # owner
        self.assertEqual(out["example"], "shared example")
        self.assertEqual(out["image"], "shared-img")

    def test_word_card_guest_and_owner_use_personal_to_shared_fallback(self):
        from bot.ui.formatting import _format_word_card

        os.environ["DB_PATH"] = self.db  # _format_word_card resolves the DB via get_db_path()
        os.environ["OWNER_ID"] = "5000"
        self.addCleanup(lambda: os.environ.pop("DB_PATH", None))
        self.addCleanup(lambda: os.environ.pop("OWNER_ID", None))
        entry = {
            "id": self.wid, "type": "nomen", "german": "Haus",
            "image": "shared-img", "example": "shared example",
        }
        # Telegram full cards fall back to shared media when no personal value exists.
        card_guest = _format_word_card(entry, user_id=U1)
        self.assertIn("example: shared example", card_guest)
        self.assertIn("image: shared-img", card_guest)
        # guest who set their own -> sees their own
        set_user_word_media(self.db, U1, self.wid, example="mnemonic ex", image="my-img")
        card_guest2 = _format_word_card(entry, user_id=U1)
        self.assertIn("example: mnemonic ex", card_guest2)
        self.assertIn("image: my-img", card_guest2)
        # owner without personal -> shared
        card_owner = _format_word_card(entry, user_id=5000)
        self.assertIn("example: shared example", card_owner)
        self.assertIn("image: shared-img", card_owner)
        # owner personal media is private and wins independently per field
        set_user_word_media(self.db, 5000, self.wid, example="owner-only example")
        card_owner_personal = _format_word_card(entry, user_id=5000)
        self.assertIn("example: owner-only example", card_owner_personal)
        self.assertIn("image: shared-img", card_owner_personal)


if __name__ == "__main__":
    unittest.main()
