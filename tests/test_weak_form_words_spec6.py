"""spec #6 — db_search "weak words" = active weak-form episodes (#18A *_active),
not FSRS wrong-streaks. Storage query + line render + empty-list i18n."""

import os
import sqlite3
import tempfile
import unittest

from bot.i18n import t
from bot.storage import init_db, list_weak_form_words


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


class WeakFormWordsTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            INSERT INTO words (id, type, german, russian) VALUES ('n1', 'nomen', 'Haus', 'дом');
            INSERT INTO words (id, type, german, russian) VALUES ('n2', 'nomen', 'Auto', 'машина');
            INSERT INTO words (id, type, german, infinitive, russian) VALUES ('v1', 'verb', 'gehen', 'gehen', 'идти');
            INSERT INTO words (id, type, german, russian) VALUES ('n3', 'nomen', 'Buch', 'книга');
            """
        )
        conn.commit()
        conn.close()

    def _fs(self, user_id, word_id, **flags):
        cols = ["user_id", "word_id"] + list(flags)
        vals = [user_id, word_id] + [flags[k] for k in flags]
        conn = sqlite3.connect(self.db)
        conn.execute(
            f"INSERT INTO form_stats ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )
        conn.commit()
        conn.close()

    def test_lists_active_episodes_with_forms(self):
        self._fs(7, "n1", article_active=1, plural_active=1)
        self._fs(7, "n2", plural_active=1)
        self._fs(7, "v1", prateritum_active=1)
        self._fs(7, "n3", article_active=0)  # no active flag -> excluded
        res = list_weak_form_words(self.db, 7)
        ids = [e["id"] for e in res]
        self.assertEqual(ids, ["n1", "n2", "v1"])  # worst first (2 forms), then alpha (Auto<gehen)
        forms = {e["id"]: e["weak_forms"] for e in res}
        self.assertEqual(forms["n1"], ["artikel", "plural"])
        self.assertEqual(forms["n2"], ["plural"])
        self.assertEqual(forms["v1"], ["präteritum"])

    def test_isolation_per_user(self):
        self._fs(7, "n1", article_active=1)
        self._fs(8, "n2", plural_active=1)
        self.assertEqual([e["id"] for e in list_weak_form_words(self.db, 7)], ["n1"])
        self.assertEqual([e["id"] for e in list_weak_form_words(self.db, 8)], ["n2"])

    def test_cleared_episode_leaves_list(self):
        self._fs(7, "n1", article_active=1)
        self.assertEqual([e["id"] for e in list_weak_form_words(self.db, 7)], ["n1"])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE form_stats SET article_active = 0 WHERE user_id = 7 AND word_id = 'n1'")
        conn.commit()
        conn.close()
        self.assertEqual(list_weak_form_words(self.db, 7), [])

    def test_empty_when_none_active(self):
        self.assertEqual(list_weak_form_words(self.db, 999), [])

    def test_line_render(self):
        from bot.handlers.db_search import _format_weak_form_line
        self._fs(7, "n1", article_active=1, plural_active=1)
        self._fs(7, "v1", partizip2_active=1)
        by_id = {e["id"]: e for e in list_weak_form_words(self.db, 7)}
        self.assertEqual(_format_weak_form_line(by_id["n1"]), "Haus — artikel, plural")  # noun capitalized
        self.assertEqual(_format_weak_form_line(by_id["v1"]), "gehen — partizip2")  # verb lowercase

    def test_no_weak_words_localized(self):
        self.assertEqual(t("en", "no_weak_words"), "No weak words.")
        self.assertEqual(t("ru", "no_weak_words"), "Нет слабых слов.")
        self.assertEqual(t("uk", "no_weak_words"), "Немає слабких слів.")
        # must not fall back to the raw key (that would be an English leak, #15)
        self.assertNotEqual(t("ru", "no_weak_words"), "no_weak_words")


if __name__ == "__main__":
    unittest.main()
