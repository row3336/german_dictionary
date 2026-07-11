"""todo T6 — long db_search listings go out as ONE .txt document.

Owner decision (11.07): the threshold is "doesn't fit one message" — short
lists stay inline exactly as before; anything that used to become a series of
messages becomes a single file with the reply keyboard on it.
"""

import asyncio
import unittest
from datetime import date

from _telegram_stub import install

install()

from bot.handlers.db_search import _list_filename, _send_lines_or_file  # noqa: E402


class _Msg:
    def __init__(self):
        self.texts = []
        self.documents = []

    async def reply_text(self, text, **kw):
        self.texts.append({"text": text, "markup": kw.get("reply_markup")})

    async def reply_document(self, document, filename=None, caption=None, reply_markup=None, **kw):
        self.documents.append({
            "data": document, "filename": filename,
            "caption": caption, "markup": reply_markup,
        })


class SendLinesOrFileTest(unittest.TestCase):
    def test_short_list_stays_inline(self):
        msg = _Msg()
        body = "topic 1: 2 words\n\n- Haus = дом\n- gehen = идти"
        asyncio.run(_send_lines_or_file(msg, body, "topic_1", markup="KB"))
        self.assertEqual(len(msg.texts), 1)
        self.assertEqual(msg.documents, [])
        self.assertIn(body, msg.texts[0]["text"])
        self.assertEqual(msg.texts[0]["markup"], "KB")

    def test_long_list_becomes_one_document(self):
        msg = _Msg()
        lines = ["topic 9: 400 words", ""] + [f"- Wort{i} = слово{i}" for i in range(400)]
        body = "\n".join(lines)
        asyncio.run(_send_lines_or_file(msg, body, "topic_9", markup="KB"))
        self.assertEqual(msg.texts, [])           # no message series anymore
        self.assertEqual(len(msg.documents), 1)   # exactly ONE file
        doc = msg.documents[0]
        self.assertEqual(doc["data"].decode("utf-8"), body)  # nothing truncated
        self.assertEqual(doc["filename"], _list_filename("topic_9"))
        self.assertEqual(doc["caption"], "topic 9: 400 words")  # summary line
        self.assertEqual(doc["markup"], "KB")     # keyboard rides on the file

    def test_explicit_caption_wins(self):
        msg = _Msg()
        body = "\n\n".join(f"pair {i}\npair {i}b" for i in range(600))
        asyncio.run(_send_lines_or_file(msg, body, "matches", markup=None, caption="matches: 600 pairs"))
        self.assertEqual(msg.documents[0]["caption"], "matches: 600 pairs")

    def test_filename_is_safe_and_dated(self):
        name = _list_filename("topic_05 Essen/Trinken")
        self.assertEqual(name, f"db_topic_05_Essen_Trinken_{date.today().isoformat()}.txt")
        self.assertNotIn(" ", name)
        self.assertNotIn("/", name)


if __name__ == "__main__":
    unittest.main()
