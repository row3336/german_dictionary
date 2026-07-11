"""todo T1 — casing normalization on manual edits (clarify → shared DB).

Owner rule (2026-07-11): the shared dictionary always stores articles, verbs,
all word forms, translations and everything that is not a German noun in
lowercase; German nouns (and each plural form) capitalized; words with
intentional inner capitals ("KFZ-Mechaniker") are kept as typed.
"""

import asyncio
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

from bot.storage import (  # noqa: E402
    _normalize_word_entry_for_storage,
    add_word,
    get_word_by_id,
    init_db,
    normalize_word_entry,
)
from bot.text_utils import cap_word  # noqa: E402

OWNER = 999


class CapWordTest(unittest.TestCase):
    def test_mono_case_classic(self):
        self.assertEqual(cap_word("haus"), "Haus")
        self.assertEqual(cap_word("HAUS"), "Haus")
        self.assertEqual(cap_word("Haus"), "Haus")

    def test_mixed_case_preserved(self):
        # intentional inner capitals stay as typed (only the first letter raised)
        self.assertEqual(cap_word("KFZ-Mechaniker"), "KFZ-Mechaniker")
        self.assertEqual(cap_word("kFZ-Mechaniker"), "KFZ-Mechaniker")

    def test_empty(self):
        self.assertEqual(cap_word(""), "")
        self.assertEqual(cap_word(None), "")


class NormalizeEntryTest(unittest.TestCase):
    def test_nomen_german_and_each_plural_form_capitalized(self):
        entry = _normalize_word_entry_for_storage(
            {"type": "nomen", "article": "DIE", "german": "pizza", "plural": "pizzas, PIZZEN"}
        )
        self.assertEqual(entry["article"], "die")
        self.assertEqual(entry["german"], "Pizza")
        self.assertEqual(entry["plural"], "Pizzas, Pizzen")

    def test_non_nomen_forms_and_translations_lowercase(self):
        entry = _normalize_word_entry_for_storage(
            {"type": "verb", "infinitive": "GEHEN", "prateritum": "Ging",
             "russian": "ИДТИ, Шагать", "ukrainian": "ЙТИ", "english": "To Go"}
        )
        self.assertEqual(entry["infinitive"], "gehen")
        self.assertEqual(entry["prateritum"], "ging")
        self.assertEqual(entry["russian"], "идти, шагать")
        self.assertEqual(entry["ukrainian"], "йти")
        self.assertEqual(entry["english"], "to go")

    def test_phrase_untouched(self):
        entry = _normalize_word_entry_for_storage(
            {"type": "phrase", "german": "Wie geht es Dir", "russian": "Как Дела"}
        )
        self.assertEqual(entry["german"], "Wie geht es Dir")
        self.assertEqual(entry["russian"], "Как Дела")

    def test_public_alias(self):
        self.assertEqual(normalize_word_entry({"type": "nomen", "german": "haus"})["german"], "Haus")


class _Msg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid=OWNER, text=""):
        self.message = _Msg()
        self.message.text = text
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


class ClarifySaveNormalizesTest(unittest.TestCase):
    """Owner edits a shared word through clarify — the DB row must come out
    normalized (this used to store the text exactly as typed)."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        self.entry = add_word(self.db, {
            "id": "w-norm", "type": "nomen", "article": "das", "german": "Haus",
            "plural": "Häuser", "russian": "дом", "topic": "1",
        })

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _edit(self, field, text):
        from bot.handlers.clarify_fields import clarify_fields_input
        upd, ctx = _Update(text=text), _Ctx()
        ctx.user_data.update({
            "edit_source": "quiz",
            "quiz_edit_entry": dict(self.entry),
            "edit_stage": "enter_value",
            "edit_field": field,
        })
        asyncio.run(clarify_fields_input(upd, ctx))
        return ctx

    def test_translation_uppercase_is_stored_lowercase(self):
        self._edit("russian", "ИДТИ, Шагать")
        row = get_word_by_id(self.db, "w-norm")
        self.assertEqual(row["russian"], "идти, шагать")

    def test_nomen_german_recapitalized(self):
        self._edit("german", "hAUS")
        row = get_word_by_id(self.db, "w-norm")
        self.assertEqual(row["german"], "Haus")

    def test_plural_forms_recapitalized(self):
        self._edit("plural", "häuser, HÄUSERN")
        row = get_word_by_id(self.db, "w-norm")
        self.assertEqual(row["plural"], "Häuser, Häusern")


if __name__ == "__main__":
    unittest.main()
