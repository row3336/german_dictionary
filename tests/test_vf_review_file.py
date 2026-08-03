"""spec #2 step 4 — VF review file: build the JSON document and send it."""

import asyncio
import json
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.verb_forms_parsing.add_flow as AF  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.verb_forms_parsing import job_store as JS  # noqa: E402
from bot.verb_forms_parsing.vf_file import build_review_document, review_filename  # noqa: E402


class _FakeBot:
    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, document, filename=None, caption=None):
        self.docs.append({"chat_id": chat_id, "data": document, "filename": filename, "caption": caption})

    async def send_message(self, chat_id, text):
        self.docs.append({"chat_id": chat_id, "text": text})


class ReviewFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(lambda: os.environ.__setitem__("DB_PATH", self._old) if self._old else os.environ.pop("DB_PATH", None))
        self.jid = JS.create_job(self.db, 7, 100, "05 Essen", ["gehen", "weil"])
        items = JS.list_items(self.db, self.jid)
        JS.save_item_result(
            self.db, items[0]["id"], "done",
            result={"type": "verb", "infinitive": "gehen", "russian": "идти, уходить",
                    "_headword": "gehen", "_query": "gehen", "_flags": []},
            flags="",
        )
        JS.save_item_result(self.db, items[1]["id"], "error", error="http 429")

    def test_document_structure(self):
        doc = build_review_document(self.db, self.jid)
        self.assertEqual(doc["job_id"], self.jid)
        self.assertEqual(doc["topic"], "05 Essen")
        self.assertIn("instructions", doc)
        self.assertEqual(len(doc["words"]), 2)
        card = doc["words"][0]
        self.assertEqual(card["infinitive"], "gehen")
        self.assertNotIn("_headword", card)  # underscore meta moved into _review
        self.assertEqual(card["_review"]["headword"], "gehen")
        self.assertEqual(card["_review"]["status"], "done")
        # error item becomes a stub carrying the error
        err = doc["words"][1]
        self.assertEqual(err["german"], "weil")
        self.assertEqual(err["_review"]["status"], "error")
        self.assertEqual(err["_review"]["error"], "http 429")

    def test_send_review_file(self):
        # T8: the review now ships as TWO documents — the apply JSON plus the
        # interactive HTML companion built from the same document.
        bot = _FakeBot()
        counts = JS.job_counts(self.db, self.jid)
        asyncio.run(AF._send_review_file(bot, 100, self.jid, counts))
        self.assertEqual(len(bot.docs), 2)
        sent = bot.docs[0]
        self.assertEqual(sent["filename"], review_filename(self.jid))
        parsed = json.loads(sent["data"].decode("utf-8"))
        self.assertEqual(len(parsed["words"]), 2)
        html = bot.docs[1]
        self.assertEqual(html["filename"], f"vf_review_job{self.jid}.html")
        page = html["data"].decode("utf-8")
        self.assertIn("gehen", page)
        self.assertNotIn("__VF_REVIEW_DATA__", page)


if __name__ == "__main__":
    unittest.main()
