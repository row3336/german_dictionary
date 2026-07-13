"""P4 — add-topic decoupled from the quiz filter + mandatory level for new topics
(spec_topics_levels.md §5). Adding words picks a per-batch topic (session only), never
touches TOPIC_FILTER_KEY; a brand-new topic must get a book-level; VF add routes through
the same topic step.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.add_words as AW  # noqa: E402
import bot.verb_forms_parsing.add_flow as AF  # noqa: E402
from bot.app_config import TOPIC_FILTER_KEY  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    get_topic_level,
    get_user_setting,
    init_db,
    register_or_update_user,
)

U = 909


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **k):
        self.replies.append(text)


class _Upd:
    def __init__(self, text=""):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": U})()
        self.effective_chat = type("C", (), {"id": U})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _run(coro):
    return asyncio.run(coro)


class AddTopicDecoupleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)
        register_or_update_user(self.db, U, "u", "U")
        add_word(self.db, {"german": "Haus", "type": "nomen", "article": "das", "russian": "дом", "topic": "1"})

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_existing_topic_sets_session_not_quiz_filter(self):
        ctx = _Ctx()
        ctx.user_data["pending_add_queue"] = []  # empty -> AI queue finishes immediately
        _run(AW.handle_add_topic_input(_Upd(), ctx, "1"))
        self.assertEqual(ctx.user_data.get("add_topic"), "1")
        # the quiz topic-filter was NOT written (decoupled)
        self.assertIsNone(get_user_setting(self.db, U, TOPIC_FILTER_KEY, None))

    def test_new_topic_requires_description_then_level(self):
        ctx = _Ctx()
        ctx.user_data["pending_add_queue"] = []
        # bare new name -> ask description
        _run(AW.handle_add_topic_input(_Upd(), ctx, "09"))
        self.assertEqual(ctx.user_data.get("pending_new_topic"), "09")
        # description -> ask level
        _run(AW.handle_add_topic_input(_Upd(), ctx, "Politik"))
        self.assertEqual(ctx.user_data.get("pending_topic_level"), "09")
        # level -> commit
        _run(AW.handle_add_topic_input(_Upd(), ctx, "B2"))
        self.assertEqual(get_topic_level(self.db, "09"), "B2")
        self.assertEqual(ctx.user_data.get("add_topic"), "09")
        self.assertIsNone(get_user_setting(self.db, U, TOPIC_FILTER_KEY, None))

    def test_new_topic_inline_description_then_level(self):
        ctx = _Ctx()
        ctx.user_data["pending_add_queue"] = []
        _run(AW.handle_add_topic_input(_Upd(), ctx, "10 Reisen"))  # name+desc in one line
        self.assertEqual(ctx.user_data.get("pending_topic_level"), "10")
        _run(AW.handle_add_topic_input(_Upd(), ctx, "A1"))
        self.assertEqual(get_topic_level(self.db, "10"), "A1")

    def test_invalid_level_reprompts(self):
        ctx = _Ctx()
        ctx.user_data["pending_add_queue"] = []
        _run(AW.handle_add_topic_input(_Upd(), ctx, "11 Kunst"))
        _run(AW.handle_add_topic_input(_Upd(), ctx, "ZZ"))  # not a CEFR level
        self.assertEqual(ctx.user_data.get("pending_topic_level"), "11")  # still pending
        self.assertEqual(get_topic_level(self.db, "11"), "")  # nothing committed

    def test_vf_routes_through_topic_step(self):
        ctx = _Ctx()
        _run(AF._prompt_vf_topic(_Upd(), ctx, ["Haus", "Auto"]))
        self.assertEqual(ctx.user_data.get("pending_vf_words"), ["Haus", "Auto"])
        self.assertEqual(ctx.user_data.get("add_topic_then"), "vf")
        self.assertEqual(ctx.user_data.get("topic_return"), "add_queue")

    def test_vf_finalize_launches_job_with_session_topic(self):
        calls = {}

        async def _fake_start(update, context, words):
            calls["words"] = words
            calls["topic"] = context.user_data.get("add_topic")

        orig = AF.start_vf_add_job
        AF.start_vf_add_job = _fake_start
        try:
            ctx = _Ctx()
            ctx.user_data["pending_vf_words"] = ["Haus"]
            ctx.user_data["add_topic_then"] = "vf"
            _run(AW.handle_add_topic_input(_Upd(), ctx, "1"))  # existing topic
        finally:
            AF.start_vf_add_job = orig
        self.assertEqual(calls.get("words"), ["Haus"])
        self.assertEqual(calls.get("topic"), "1")
        self.assertIsNone(get_user_setting(self.db, U, TOPIC_FILTER_KEY, None))


if __name__ == "__main__":
    unittest.main()
