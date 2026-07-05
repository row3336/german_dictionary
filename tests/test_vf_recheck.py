"""spec #2 step 6 — VF recheck of existing words: reset, recheck job, diff file,
update apply, and the recheck UI entry."""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.verb_forms_parsing.add_flow as AF  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    get_word_by_id,
    init_db,
    list_all_words,
    list_words_for_vf_recheck,
    reset_vf_checks,
    save_vf_check_result,
)
from bot.verb_forms_parsing import job_store as JS  # noqa: E402
from bot.verb_forms_parsing.vf_file import build_review_document, parse_review_document  # noqa: E402


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMsg:
    def __init__(self, text=None):
        self.text = text
        self.document = None
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text=None, uid=7, cid=100):
        self.message = _FakeMsg(text)
        self.effective_user = _FakeUser(uid)
        self.effective_chat = _FakeChat(cid)


class _FakeBot:
    async def send_message(self, chat_id, text, reply_markup=None):
        class _M:
            message_id = 1
        return _M()


class _Ctx:
    def __init__(self):
        self.user_data = {}
        self.bot = _FakeBot()


async def _fake_run(db_path, job_id, on_progress=None):
    return "done"


class RecheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(lambda: os.environ.__setitem__("DB_PATH", self._old) if self._old else os.environ.pop("DB_PATH", None))
        self.gehen = add_word(self.db, {"type": "verb", "infinitive": "gehen", "russian": "идти", "topic": "1"})["id"]
        self.haus = add_word(self.db, {"type": "nomen", "article": "das", "german": "Haus", "russian": "дом", "topic": "1"})["id"]
        add_word(self.db, {"type": "praeposition", "german": "mit", "russian": "с", "topic": "1"})

    def test_list_words_for_recheck_filters_types(self):
        cand = list_words_for_vf_recheck(self.db)
        lookups = {lookup for _wid, lookup, _t in cand}
        self.assertEqual(lookups, {"gehen", "Haus"})  # praeposition excluded

    def test_reset_vf_checks(self):
        save_vf_check_result(self.db, 7, self.gehen, "verb", "gehen", status="match")
        conn = sqlite3.connect(self.db)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM vf_checks").fetchone()[0], 0)
        conn.close()
        reset_vf_checks(self.db)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM vf_checks").fetchone()[0], 0)
        conn.close()

    def test_recheck_job_items_carry_word_id(self):
        jid = JS.create_recheck_job(self.db, 7, 100, [("gehen", self.gehen), ("Haus", self.haus)])
        items = JS.list_items(self.db, jid)
        self.assertEqual({it["word_id"] for it in items}, {self.gehen, self.haus})

    def test_diff_document_and_apply_update(self):
        jid = JS.create_recheck_job(self.db, 7, 100, [("gehen", self.gehen)])
        item = JS.list_items(self.db, jid)[0]
        # VF proposes a richer russian than what's in the DB
        JS.save_item_result(
            self.db, item["id"], "done",
            result={"type": "verb", "infinitive": "gehen", "russian": "идти, уходить",
                    "_headword": "gehen", "_flags": []},
        )
        doc = build_review_document(self.db, jid)
        card = doc["words"][0]
        self.assertEqual(card["_review"]["word_id"], self.gehen)
        self.assertIn("russian", card["_review"]["diff"])
        self.assertEqual(card["_review"]["diff"]["russian"]["current"], "идти")
        self.assertEqual(card["_review"]["diff"]["russian"]["vf"], "идти, уходить")
        # round-trip: parse keeps the id, apply updates the existing word (no new row)
        import json
        entries, _issues = parse_review_document(json.dumps(doc))
        self.assertEqual(entries[0]["id"], self.gehen)
        before = len(list_all_words(self.db))
        added, updated, dups, errors = AF.apply_batch(self.db, entries)
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(get_word_by_id(self.db, self.gehen)["russian"], "идти, уходить")
        self.assertEqual(before, len(list_all_words(self.db)))  # updated in place, not inserted

    def test_start_recheck_all_launches_job(self):
        real = AF.run_job
        AF.run_job = _fake_run
        self.addCleanup(lambda: setattr(AF, "run_job", real))
        AF.VF_ADD_TASKS.clear()
        self.addCleanup(AF.VF_ADD_TASKS.clear)
        ctx = _Ctx()
        state = asyncio.run(AF._start_recheck(_FakeUpdate(text="all"), ctx, "all"))
        # a recheck job over both candidates was created and a task launched
        jobs = JS.active_jobs(self.db) or []
        self.assertTrue(jobs)
        jid = jobs[0]["id"]
        self.assertEqual(JS.job_counts(self.db, jid)["total"], 2)
        self.assertIn(jid, AF.VF_ADD_TASKS)


if __name__ == "__main__":
    unittest.main()
