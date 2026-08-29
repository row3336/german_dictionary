import os
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from bot.storage import add_word, init_db, search_word  # noqa: E402


def _word(wid, german, **extra):
    entry = {
        "id": wid,
        "type": "nomen",
        "german": german,
    }
    entry.update(extra)
    return entry


class StorageSearchLikeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(delete=False)
        temp.close()
        self.db = temp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_search_like_treats_percent_literally(self):
        add_word(self.db, _word("w-no-percent", "axb"))
        add_word(self.db, _word("w-literal-percent", "xa%by"))
        found = search_word(self.db, "a%b")
        self.assertEqual(found["id"], "w-literal-percent")

    def test_search_like_treats_percent_without_literal_percent_card_as_none(self):
        add_word(self.db, _word("w-no-percent", "aaxby"))
        add_word(self.db, _word("w-another", "abbbby"))
        self.assertIsNone(search_word(self.db, "%"))

    def test_search_like_treats_underscore_literally(self):
        add_word(self.db, _word("w-no-underscore", "acb"))
        add_word(self.db, _word("w-literal-underscore", "xa_bz"))
        found = search_word(self.db, "a_b")
        self.assertEqual(found["id"], "w-literal-underscore")

    def test_search_like_treats_underscore_without_literal_underscore_card_as_none(self):
        add_word(self.db, _word("w-no-underscore", "acb"))
        add_word(self.db, _word("w-hint", "ad3b"))
        self.assertIsNone(search_word(self.db, "_"))

    def test_search_like_handles_backslash(self):
        add_word(self.db, _word("w-no-literal-backslash", "a\\cb"))
        add_word(self.db, _word("w-literal-backslash", "xa\\%by"))
        found = search_word(self.db, "a\\%b")
        self.assertEqual(found["id"], "w-literal-backslash")

    def test_search_like_handles_standalone_backslash(self):
        add_word(self.db, _word("w-no-literal-backslash", "abc"))
        add_word(self.db, _word("w-literal-backslash", "xa\\by"))
        found = search_word(self.db, "\\")
        self.assertEqual(found["id"], "w-literal-backslash")

    def test_search_like_ordinary_substring_unchanged(self):
        add_word(self.db, _word("w-not-matching", "banana"))
        add_word(self.db, _word("w-ordinary", "suchen"))
        found = search_word(self.db, "uchen")
        self.assertEqual(found["id"], "w-ordinary")

    def test_search_like_strips_leading_article_for_literal_percent(self):
        add_word(self.db, _word("w-plain", "a xay b"))
        add_word(self.db, _word("w-literal", "a xa%y b"))
        found = search_word(self.db, "der xa%y")
        self.assertEqual(found["id"], "w-literal")

    def test_search_like_strips_leading_article_for_literal_underscore(self):
        add_word(self.db, _word("w-plain", "a xay b"))
        add_word(self.db, _word("w-literal", "a xa_by b"))
        found = search_word(self.db, "die xa_by")
        self.assertEqual(found["id"], "w-literal")

    def test_exact_match_not_changed(self):
        add_word(self.db, _word("w-exact", "a%b"))
        found = search_word(self.db, "a%b")
        self.assertEqual(found["id"], "w-exact")
