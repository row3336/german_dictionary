"""spec #7 — per-user personal translation override (user_word_media.<lang>).

Storage + overlay + De>L1 accept-both (migration in test_migrations.py::
UserWordMediaTranslationMigrationTest; clarify UI flow driven live / by handler
tests). A user overrides ONLY their dict_lang's translation; the shared `words`
row is never touched; the base translation still counts as correct.
"""

import os
import tempfile
import unittest

from bot.storage import (
    _DICT_TRANSLATION_COLUMN,
    add_word,
    apply_user_word_media,
    get_user_word_media,
    get_word_by_id,
    init_db,
    register_or_update_user,
    set_user_dict_lang,
    set_user_word_translation,
)
from bot.text_utils import russian_variants
from bot.ui.formatting import _entry_translation

OWNER, G_RU, G_UK = 999, 101, 202


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _accepted_de_l1_variants(entry, dict_lang):
    """Replicates the De>L1 checker's accepted-answer set on an overlaid entry."""
    variants = set(russian_variants(_entry_translation(entry, dict_lang)))
    if str(entry.get("my_translation", "") or "").strip():
        base_field = _DICT_TRANSLATION_COLUMN.get(dict_lang, "russian")
        variants |= set(russian_variants(entry.get(base_field, "")))
    return variants


class Spec7Test(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("OWNER_ID")
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        self.wid = add_word(self.db, {
            "type": "verb", "german": "gehen", "infinitive": "gehen",
            "russian": "идти", "ukrainian": "йти", "english": "to go",
        })["id"]
        register_or_update_user(self.db, G_RU, "gru", "G")
        set_user_dict_lang(self.db, G_RU, "ru")
        register_or_update_user(self.db, G_UK, "guk", "G")
        set_user_dict_lang(self.db, G_UK, "uk")

    def _restore(self):
        if self._old is None:
            os.environ.pop("OWNER_ID", None)
        else:
            os.environ["OWNER_ID"] = self._old

    def _base_entry(self):
        return dict(get_word_by_id(self.db, self.wid))

    # --- storage ---------------------------------------------------------------
    def test_writes_to_dict_lang_column_only(self):
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "шагать")
        media = get_user_word_media(self.db, G_RU, self.wid)
        self.assertEqual(media["russian"], "шагать")
        self.assertEqual(media["ukrainian"], "")
        self.assertEqual(media["english"], "")

    def test_invalid_dict_lang_raises(self):
        with self.assertRaises(ValueError):
            set_user_word_translation(self.db, G_RU, self.wid, "de", "x")

    def test_shared_words_row_untouched(self):
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "шагать")
        row = get_word_by_id(self.db, self.wid)
        self.assertEqual(row["russian"], "идти")  # shared dictionary unchanged

    # --- overlay ---------------------------------------------------------------
    def test_overlay_sets_my_translation_for_dict_lang(self):
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "шагать")
        out = apply_user_word_media(self.db, G_RU, self._base_entry())
        self.assertEqual(out["my_translation"], "шагать")
        self.assertEqual(_entry_translation(out, "ru"), "шагать")
        # base fields still present underneath
        self.assertEqual(out["russian"], "идти")

    def test_overlay_uk_guest_uses_ukrainian_column(self):
        set_user_word_translation(self.db, G_UK, self.wid, "uk", "крокувати")
        out = apply_user_word_media(self.db, G_UK, self._base_entry())
        self.assertEqual(_entry_translation(out, "uk"), "крокувати")

    def test_no_override_falls_back_to_base(self):
        out = apply_user_word_media(self.db, G_RU, self._base_entry())
        self.assertEqual(out.get("my_translation", ""), "")
        self.assertEqual(_entry_translation(out, "ru"), "идти")

    def test_clear_reverts_to_base(self):
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "шагать")
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "")
        out = apply_user_word_media(self.db, G_RU, self._base_entry())
        self.assertEqual(_entry_translation(out, "ru"), "идти")

    def test_owner_gets_no_override(self):
        set_user_word_translation(self.db, OWNER, self.wid, "ru", "владельческий")
        out = apply_user_word_media(self.db, OWNER, self._base_entry())
        # owner sees the shared word as-is (early return, no my_translation)
        self.assertNotIn("my_translation", out)
        self.assertEqual(_entry_translation(out, "ru"), "идти")

    def test_isolation_two_guests(self):
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "шагать")
        # G_UK has no personal ru; and a personal uk of their own
        set_user_word_translation(self.db, G_UK, self.wid, "uk", "крокувати")
        out_ru = apply_user_word_media(self.db, G_RU, self._base_entry())
        out_uk = apply_user_word_media(self.db, G_UK, self._base_entry())
        self.assertEqual(_entry_translation(out_ru, "ru"), "шагать")
        self.assertEqual(_entry_translation(out_uk, "uk"), "крокувати")

    # --- De>L1 accept-both -----------------------------------------------------
    def test_de_l1_accepts_personal_and_base(self):
        set_user_word_translation(self.db, G_RU, self.wid, "ru", "шагать")
        out = apply_user_word_media(self.db, G_RU, self._base_entry())
        accepted = _accepted_de_l1_variants(out, "ru")
        # both the personal AND the shared translation count as correct
        self.assertTrue(set(russian_variants("шагать")) & accepted)
        self.assertTrue(set(russian_variants("идти")) & accepted)

    def test_de_l1_without_override_only_base(self):
        out = apply_user_word_media(self.db, G_RU, self._base_entry())
        accepted = _accepted_de_l1_variants(out, "ru")
        self.assertTrue(set(russian_variants("идти")) & accepted)

    # --- clarify keyboard (UI) -------------------------------------------------
    def test_guest_clarify_keyboard_has_my_translation_owner_does_not(self):
        from bot.i18n import t
        from bot.ui.keyboards import _clarify_fields_markup

        def _flat(markup):
            rows = getattr(markup, "keyboard", None) or getattr(markup, "rows", None) or []
            return [getattr(b, "text", b) for row in rows for b in row]

        guest_btns = _flat(_clarify_fields_markup("quiz", G_RU))
        self.assertIn(t("en", "my_translation"), guest_btns)  # guest default interface = en
        owner_btns = _flat(_clarify_fields_markup("quiz", OWNER))
        self.assertNotIn(t("en", "my_translation"), owner_btns)  # owner edits shared dict, no personal


if __name__ == "__main__":
    unittest.main()
