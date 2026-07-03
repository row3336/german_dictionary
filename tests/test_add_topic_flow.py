"""spec #8 block 6 — owner add-flow topic selection (name + description, text-only).

_process_next_ai_word (the AI step that runs after a topic is chosen) is stubbed so
these tests exercise only the topic parse/create/select logic, not the network.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.add_words as AW  # noqa: E402
from bot.common.settings import _topic_filter_value  # noqa: E402
from bot.storage import (  # noqa: E402
    get_topic_description,
    init_db,
    insert_topic_if_absent,
    list_topics_with_desc,
)

OWNER = 777
_SENTINEL = "PROCESSED"


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text, uid):
        self.message = _FakeMsg(text)
        self.effective_user = _FakeUser(uid)


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data if user_data is not None else {}


async def _fake_process(update, context):
    return _SENTINEL


class AddTopicFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)
        # stub the AI step that finalize calls
        self._real_process = AW._process_next_ai_word
        AW._process_next_ai_word = _fake_process
        self.addCleanup(lambda: setattr(AW, "_process_next_ai_word", self._real_process))

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def _call(self, text, ctx):
        upd = _FakeUpdate(text, OWNER)
        result = asyncio.run(AW.handle_add_topic_input(upd, ctx, text))
        return upd, result

    def _fresh_ctx(self):
        return _Ctx({"topic_return": "add_queue", "pending_add_queue": [{"x": 1}]})

    def test_new_topic_with_description_inserts_and_proceeds(self):
        ctx = self._fresh_ctx()
        _upd, result = self._call("05 Essen und Trinken", ctx)
        self.assertEqual(result, _SENTINEL)                       # proceeded to AI step
        self.assertEqual(get_topic_description(self.db, "05"), "Essen und Trinken")
        self.assertEqual(_topic_filter_value(OWNER), "05")        # topic chosen for batch
        self.assertEqual(ctx.user_data.get("add_queue"), [{"x": 1}])

    def test_bare_new_topic_prompts_then_inserts(self):
        ctx = self._fresh_ctx()
        upd, result = self._call("06", ctx)
        self.assertNotEqual(result, _SENTINEL)                    # waits, does not proceed
        self.assertEqual(ctx.user_data.get("pending_new_topic"), "06")
        self.assertTrue(any("description for new topic 06" in r for r in upd.message.replies))
        self.assertEqual(get_topic_description(self.db, "06"), "")  # not inserted yet

        upd2, result2 = self._call("Beschreibung sechs", ctx)
        self.assertEqual(result2, _SENTINEL)
        self.assertEqual(get_topic_description(self.db, "06"), "Beschreibung sechs")
        self.assertEqual(_topic_filter_value(OWNER), "06")
        self.assertIsNone(ctx.user_data.get("pending_new_topic"))

    def test_existing_topic_tail_ignored_description_unchanged(self):
        insert_topic_if_absent(self.db, "01", "Zahlen")
        ctx = self._fresh_ctx()
        _upd, result = self._call("01 some random tail", ctx)
        self.assertEqual(result, _SENTINEL)
        self.assertEqual(_topic_filter_value(OWNER), "01")
        self.assertEqual(get_topic_description(self.db, "01"), "Zahlen")  # NOT edited

    def test_separators_comma_dash_dot(self):
        ctx = self._fresh_ctx()
        self._call("07, Essen", ctx)
        self.assertEqual(get_topic_description(self.db, "07"), "Essen")
        names = {n for n, _ in list_topics_with_desc(self.db)}
        self.assertIn("07", names)


if __name__ == "__main__":
    unittest.main()
