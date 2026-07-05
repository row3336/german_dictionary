"""spec #2 step 5 — read the edited review file back, preview, apply with backup."""

import asyncio
import glob
import json
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import sqlite3

import bot.verb_forms_parsing.add_flow as AF  # noqa: E402
from bot.app_config import MENU, VF_APPLY_CONFIRM  # noqa: E402
from bot.common.db_safety import backup_db_with_event  # noqa: E402
from bot.storage import init_db, list_all_words  # noqa: E402
from bot.verb_forms_parsing.vf_file import parse_review_document  # noqa: E402


def _conjugation_of(db, word_id):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT conjugation_praesens FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return row[0] if row else None

GEHEN_CONJ = "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen"


class ParseReviewTest(unittest.TestCase):
    def test_valid_and_issues(self):
        doc = {
            "words": [
                {"type": "verb", "infinitive": "gehen", "russian": "идти", "_review": {"query": "gehen"}},
                {"type": "nomen", "german": "Haus", "russian": "дом"},
                {"type": "", "german": "mit", "_review": {"query": "mit", "flags": ["no_card"]}},
                {"type": "verb", "infinitive": "x", "russian": "", "_review": {}},
            ]
        }
        entries, issues = parse_review_document(json.dumps(doc))
        self.assertEqual(len(entries), 2)
        self.assertNotIn("_review", entries[0])  # meta stripped
        reasons = {i["word"]: i["reason"] for i in issues}
        self.assertIn("mit", reasons)  # no type
        self.assertIn("x", reasons)  # missing russian

    def test_not_a_review_file(self):
        self.assertEqual(parse_review_document("not json"), (None, None))
        self.assertEqual(parse_review_document('{"foo": 1}'), (None, None))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    def __init__(self, text=None):
        self.text = text
        self.document = None
        self.replies = []
        self.docs = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)

    async def reply_document(self, document=None, filename=None, **kwargs):
        self.docs.append(filename)


class _FakeUpdate:
    def __init__(self, text=None, uid=7):
        self.message = _FakeMsg(text)
        self.effective_user = _FakeUser(uid)


class _Ctx:
    def __init__(self):
        self.user_data = {}


class ApplyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.dump = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DB_PATH", "DUMP_DIR")}
        os.environ["DB_PATH"] = self.db
        os.environ["DUMP_DIR"] = self.dump
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_apply_batch_writes_words_and_conjugation(self):
        entries = [
            {"type": "verb", "infinitive": "gehen", "russian": "идти", "topic": "1", "conjugation_praesens": GEHEN_CONJ},
            {"type": "nomen", "article": "das", "german": "Haus", "plural": "Häuser", "russian": "дом", "topic": "1"},
        ]
        added, updated, dups, errors = AF.apply_batch(self.db, entries)
        self.assertEqual((added, updated, dups, errors), (2, 0, 0, 0))
        words = list_all_words(self.db)
        self.assertEqual(len(words), 2)
        verb = next(w for w in words if w.get("type") == "verb")
        self.assertEqual(_conjugation_of(self.db, verb["id"]), GEHEN_CONJ)
        # applying again -> both duplicates
        added2, updated2, dups2, _ = AF.apply_batch(self.db, entries)
        self.assertEqual((added2, updated2, dups2), (0, 0, 2))

    def test_backup_with_event_creates_tagged_file(self):
        path = backup_db_with_event(self.db, self.dump, "vfadd_apply")
        self.assertIsNotNone(path)
        self.assertTrue(glob.glob(os.path.join(self.dump, "words-preapply-vfadd_apply-*.db")))

    def test_confirm_yes_applies_and_backs_up(self):
        ctx = _Ctx()
        ctx.user_data["vf_apply_entries"] = [
            {"type": "nomen", "article": "das", "german": "Haus", "plural": "Häuser", "russian": "дом", "topic": "1"},
        ]
        state = asyncio.run(AF.vf_apply_confirm(_FakeUpdate(text="yes"), ctx))
        self.assertEqual(state, MENU)
        self.assertEqual(len(list_all_words(self.db)), 1)
        self.assertTrue(glob.glob(os.path.join(self.dump, "words-preapply-vfadd_apply-*.db")))
        self.assertNotIn("vf_apply_entries", ctx.user_data)

    def test_confirm_back_cancels(self):
        from bot.i18n import t
        from bot.common.lang import _user_interface_lang
        ctx = _Ctx()
        ctx.user_data["vf_apply_entries"] = [{"type": "nomen", "german": "Haus", "russian": "дом"}]
        state = asyncio.run(AF.vf_apply_confirm(_FakeUpdate(text=t(_user_interface_lang(7), "back")), ctx))
        self.assertEqual(state, MENU)
        self.assertEqual(list_all_words(self.db), [])  # nothing applied
        self.assertNotIn("vf_apply_entries", ctx.user_data)


if __name__ == "__main__":
    unittest.main()
