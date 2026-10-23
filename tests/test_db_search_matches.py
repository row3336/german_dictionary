"""todo T3 — db_search "show matches" must surface manually linked synonyms.

senden/schicken share neither spelling nor translation text, so the base- and
translation-grouping never paired them even though they are linked; the synonym
group is now a third grouping criterion. A pair that qualifies by several
criteria is shown once.
"""

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

from bot.storage import add_synonym_link, add_word, init_db, map_synonym_group_ids  # noqa: E402


def _verb(wid, infinitive, russian):
    return {"id": wid, "type": "verb", "infinitive": infinitive, "german": infinitive,
            "russian": russian, "topic": "1", "reflexive_case": "none"}


class DbSearchMatchesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(lambda: os.environ.__setitem__("DB_PATH", self._old)
                        if self._old else os.environ.pop("DB_PATH", None))
        self.entries = [
            add_word(self.db, _verb("w-senden", "senden", "посылать")),
            add_word(self.db, _verb("w-schicken", "schicken", "отправлять")),
            add_word(self.db, _verb("w-laufen", "laufen", "бежать")),
            add_word(self.db, _verb("w-rennen", "rennen", "бежать")),  # same translation
        ]

    def _pairs(self):
        from bot.handlers.db_search import _list_match_pairs
        return _list_match_pairs(self.entries, "ru")

    def test_linked_pair_with_different_translations_is_listed(self):
        add_synonym_link(self.db, "w-senden", "w-schicken")
        blocks = self._pairs()
        linked = [b for b in blocks if "senden" in b and "schicken" in b]
        self.assertEqual(len(linked), 1)
        self.assertIn("✅ linked", linked[0])

    def test_same_translation_pair_still_listed(self):
        blocks = self._pairs()
        same_ru = [b for b in blocks if "laufen" in b and "rennen" in b]
        self.assertEqual(len(same_ru), 1)
        self.assertNotIn("✅ linked", same_ru[0])

    def test_pair_matching_two_criteria_shown_once(self):
        # linked AND same translation -> exactly one block
        add_synonym_link(self.db, "w-laufen", "w-rennen")
        blocks = self._pairs()
        both = [b for b in blocks if "laufen" in b and "rennen" in b]
        self.assertEqual(len(both), 1)
        self.assertIn("✅ linked", both[0])

    def test_unlinked_different_words_not_paired(self):
        blocks = self._pairs()
        self.assertFalse([b for b in blocks if "senden" in b and "schicken" in b])

    def test_map_synonym_group_ids(self):
        self.assertEqual(map_synonym_group_ids(self.db), {})
        add_synonym_link(self.db, "w-senden", "w-schicken")
        mapping = map_synonym_group_ids(self.db)
        self.assertEqual(set(mapping), {"w-senden", "w-schicken"})
        self.assertEqual(mapping["w-senden"], mapping["w-schicken"])


if __name__ == "__main__":
    unittest.main()
