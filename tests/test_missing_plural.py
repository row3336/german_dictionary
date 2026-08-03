"""Regression coverage for nouns that have no plural form."""

import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.common.quiz_core import _check_ru_de_strict  # noqa: E402
from bot.storage import add_word, init_db, list_weak_words, update_form_stats  # noqa: E402
from bot.text_utils import cap_plural, has_plural_form  # noqa: E402
from bot.ui.formatting import _format_feedback_ru_de, _is_pl_sg_eligible  # noqa: E402


class MissingPluralContractTest(unittest.TestCase):
    def test_absence_markers_are_not_plural_forms(self):
        for value in (None, "", " ", "-", "–", "—", "―"):
            self.assertFalse(has_plural_form(value), repr(value))
            self.assertEqual(cap_plural(value), "")
        self.assertTrue(has_plural_form("Butter"))

    def test_fsrs_accepts_article_and_noun_only(self):
        for plural in (None, "", "-", "—"):
            entry = {
                "type": "nomen",
                "article": "die",
                "german": "Butter",
                "plural": plural,
            }
            ok, checks = _check_ru_de_strict(entry, "die Butter")
            self.assertTrue(ok, repr(plural))
            self.assertEqual(checks, {"article": True})

    def test_fsrs_rejects_fake_third_field_without_plural_penalty(self):
        entry = {
            "type": "nomen",
            "article": "die",
            "german": "Butter",
            "plural": "",
        }
        ok, checks = _check_ru_de_strict(entry, "die Butter -")
        self.assertFalse(ok)
        self.assertEqual(checks, {"article": True})

    def test_malformed_answer_does_not_create_plural_penalty(self):
        entry = {
            "type": "nomen",
            "article": "die",
            "german": "Butter",
            "plural": "",
        }
        ok, checks = _check_ru_de_strict(entry, "Butter")
        self.assertFalse(ok)
        self.assertEqual(checks, {"article": False})

    def test_real_plural_contract_is_unchanged(self):
        entry = {
            "type": "nomen",
            "article": "das",
            "german": "Buch",
            "plural": "Bücher",
        }
        self.assertEqual(
            _check_ru_de_strict(entry, "das Buch Bücher"),
            (True, {"article": True, "plural": True}),
        )
        ok, checks = _check_ru_de_strict(entry, "das Buch")
        self.assertFalse(ok)
        self.assertEqual(checks, {"article": True, "plural": False})

    def test_absent_plural_is_hidden_and_ineligible_for_pl_sg(self):
        for plural in ("", "-", "—"):
            entry = {
                "type": "nomen",
                "article": "das",
                "german": "Obst",
                "plural": plural,
            }
            self.assertFalse(_is_pl_sg_eligible(entry))
            feedback = _format_feedback_ru_de(entry, False)
            self.assertNotIn("pl. die", feedback)
            self.assertNotIn("die -", feedback)


class MissingPluralWeakListTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def _add_active(self, german, plural):
        word_id = add_word(
            self.db,
            {
                "type": "nomen",
                "article": "das",
                "german": german,
                "plural": plural,
                "russian": german.lower(),
            },
        )["id"]
        update_form_stats(self.db, 1, word_id, {"plural": False})
        update_form_stats(self.db, 1, word_id, {"plural": False})
        return word_id

    def test_weak_plural_excludes_absent_forms(self):
        empty_id = self._add_active("Obst", "")
        dash_id = self._add_active("Eis", "-")
        real_id = self._add_active("Buch", "Bücher")

        result = list_weak_words(
            self.db,
            user_id=1,
            weak_field="plural",
            word_type="nomen",
        )
        ids = {entry["id"] for entry in result}
        self.assertEqual(ids, {real_id})
        self.assertNotIn(empty_id, ids)
        self.assertNotIn(dash_id, ids)


if __name__ == "__main__":
    unittest.main()
