"""Spec #9 semantics retained by K14 direct cards and explicit ID actions."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from _telegram_stub import install

install()

from bot.app_config import ADD_EDIT, DB_SEARCH, DB_SEARCH_VIEW  # noqa: E402
from bot.handlers import clarify_fields as C  # noqa: E402
from bot.handlers import db_search as D  # noqa: E402
from bot.common import search_view as V
from bot.i18n import TEXTS, t  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    register_or_update_user,
    set_user_interface_lang,
    set_user_status,
)


OWNER = 999
GUEST = 111


class _Msg:
    counter = 0

    def __init__(self, text="", calls=None, markup=None):
        type(self).counter += 1
        self.message_id = type(self).counter
        self.text = text
        self.calls = [] if calls is None else calls
        self.reply_markup = markup

    async def reply_text(self, text, **kwargs):
        sent = _Msg(text, self.calls, kwargs.get("reply_markup"))
        self.calls.append((text, kwargs, sent))
        return sent


class _Update:
    def __init__(self, text="", uid=GUEST, card=None):
        self.message = None if card else _Msg(text)
        self.effective_user = type("User", (), {"id": uid, "username": "guest", "first_name": "Guest"})()
        self.effective_chat = type("Chat", (), {"id": uid})()
        self.effective_message = card or self.message
        self.callback_query = None
        if card:
            actor = self.effective_user
            class Query:
                from_user = actor
                message = card
                data = card.reply_markup.inline_keyboard[0][0].callback_data
                answers = []
                async def answer(self, *args, **kwargs):
                    self.answers.append((args, kwargs))
            self.callback_query = Query()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _run(coro):
    return asyncio.run(coro)


def _flat(markup):
    rows = getattr(markup, "keyboard", None) or getattr(markup, "rows", None) or []
    return [getattr(button, "text", button) for row in rows for button in row]


def _prep(german, preposition_type, case_type, russian):
    return {
        "type": "praeposition",
        "german": german,
        "russian": russian,
        "preposition_type": preposition_type,
        "case_type": case_type,
        "topic": "1",
    }


class SemanticExactSelectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.old_env = {key: os.environ.get(key) for key in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore_env)
        register_or_update_user(self.db, GUEST, "guest", "Guest")
        set_user_interface_lang(self.db, GUEST, "ru")
        set_user_status(self.db, GUEST, "approved")

    def _restore_env(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _seed_vor(self):
        local = add_word(self.db, {"id": "p-local", **_prep("vor", "local", "wechsel", "перед")})
        temporal = add_word(self.db, {"id": "p-temporal", **_prep("vor", "temporal", "dativ", "до")})
        return local, temporal

    def _show_variants(self, context=None):
        context = context or _Ctx()
        update = _Update("vor")
        state = _run(D.db_search_input(update, context))
        return state, update, context

    def _cards(self, update):
        return [sent for _, kwargs, sent in update.effective_message.calls
                if getattr(kwargs.get("reply_markup"), "inline_keyboard", None)]

    def test_two_exact_cards_show_immediately_each_saved_id_and_return_without_query(self):
        local, temporal = self._seed_vor()
        state, update, context = self._show_variants()
        self.assertEqual(state, DB_SEARCH_VIEW)
        cards = self._cards(update)
        self.assertEqual(len(cards), 2)
        self.assertTrue(all("Word card:" in card.text for card in cards))
        self.assertNotIn("db_search_exact_variants", context.user_data)
        self.assertNotIn("quiz_edit_entry", context.user_data)
        for card, expected in zip(cards, (local, temporal)):
            with patch.object(D, "search_entries", side_effect=AssertionError("callback repeated text lookup")):
                self.assertEqual(_run(V.search_callback(_Update(card=card), context)), ADD_EDIT)
            self.assertEqual(context.user_data["quiz_edit_entry"]["id"], expected["id"])
            self.assertIn(expected["case_type"], card.text)
            back = _Update(t("ru", "back"))
            with patch.object(D, "search_entries", side_effect=AssertionError("return repeated lookup")):
                self.assertEqual(_run(C.clarify_fields_input(back, context)), DB_SEARCH_VIEW)
            self.assertNotIn("quiz_edit_entry", context.user_data)
            self.assertFalse(any("Word card:" in text for text, _, _ in back.message.calls))

    def test_single_exact_keeps_direct_card_and_explicit_clarify_flow(self):
        only = add_word(self.db, _prep("aus", "local", "dativ", "из"))
        context, update = _Ctx(), _Update("aus")
        self.assertEqual(_run(D.db_search_input(update, context)), DB_SEARCH_VIEW)
        self.assertNotIn("quiz_edit_entry", context.user_data)
        self.assertEqual(_run(V.search_callback(_Update(card=self._cards(update)[0]), context)), ADD_EDIT)
        self.assertEqual(context.user_data["quiz_edit_entry"]["id"], only["id"])
        self.assertNotIn("db_search_exact_variants", context.user_data)

    def test_invalid_legacy_label_and_deleted_id_never_fall_through_to_search(self):
        self._seed_vor()
        _, update, context = self._show_variants()
        card = self._cards(update)[0]
        with patch.object(D, "search_entries", side_effect=AssertionError("legacy menu queried DB")):
            context.user_data["db_search_exact_variants"] = {
                "label_to_id": {"vor [local, +D/A]": "arbitrary"}, "word_ids": ["arbitrary"]}
            legacy = _Update("vor [local, +D/A]")
            self.assertIsNone(_run(D.db_search_input(legacy, context)))
            self.assertEqual(context.user_data["db_search_exact_variants"]["word_ids"], ["arbitrary"])
            self.assertIn(t("ru", "search_stale"), legacy.message.calls[-1][0])
        with patch.object(V, "read_search_entry", return_value=None):
            self.assertIsNone(_run(V.search_callback(_Update(card=card), context)))
        self.assertNotIn("quiz_edit_entry", context.user_data)
        invalid = _Update(card=card)
        invalid.callback_query.data = "ds:unknown-token"
        self.assertIsNone(_run(V.search_callback(invalid, context)))
        self.assertNotIn("quiz_edit_entry", context.user_data)

    def test_duplicate_semantic_labels_remain_independent_ids_without_collision_picker(self):
        duplicate_rows = {
            "p1": {"id": "p1", **_prep("vor", "local", "dativ", "перед")},
            "p2": {"id": "p2", **_prep("vor", "local", "dativ", "возле")},
        }
        context, update = _Ctx(), _Update("vor")
        result = {"kind": "exact", "ordered_ids": ["p1", "p2"], "has_more_similar": False}
        with patch.object(D, "search_entries", return_value=result), patch.object(
                V, "read_search_entry", side_effect=lambda db, wid: duplicate_rows[wid]):
            self.assertEqual(_run(D.db_search_input(update, context)), DB_SEARCH_VIEW)
        cards = self._cards(update)
        self.assertEqual(len(cards), 2)
        self.assertIn("id: p1", cards[0].text)
        self.assertIn("id: p2", cards[1].text)
        tokens = [card.reply_markup.inline_keyboard[0][0].callback_data for card in cards]
        self.assertEqual(len(set(tokens)), 2)
        self.assertNotIn("db_search_exact_variants", context.user_data)

    def test_phrase_substring_only_appears_when_no_exact_and_all_similar_are_cards(self):
        add_word(self.db, {"type": "nomen", "article": "die", "german": "Tafel", "russian": "доска", "topic": "1"})
        add_word(self.db, {"type": "phrase", "german": "die Tafel Schokolade", "russian": "плитка шоколада", "topic": "1"})
        exact = _Update("Tafel")
        self.assertEqual(_run(D.db_search_input(exact, _Ctx())), DB_SEARCH_VIEW)
        self.assertEqual(len(self._cards(exact)), 1)
        self.assertFalse(any("Phrase matches:" in text for text, _, _ in exact.message.calls))
        similar = _Update("afel")
        self.assertEqual(_run(D.db_search_input(similar, _Ctx())), DB_SEARCH_VIEW)
        self.assertEqual(len(self._cards(similar)), 2)
        self.assertTrue(all("Word card:" in card.text for card in self._cards(similar)))

    def test_search_page_and_action_copy_localized_for_all_interface_languages(self):
        keys = ("search_exact", "search_page", "search_more", "search_stale", "search_similar_limit")
        for key in keys:
            for lang in ("en", "ru", "uk"):
                self.assertIn(key, TEXTS[lang])
                self.assertTrue(TEXTS[lang][key].strip())
            self.assertNotEqual(TEXTS["en"][key], TEXTS["ru"][key])
            self.assertNotEqual(TEXTS["en"][key], TEXTS["uk"][key])
        self._seed_vor()
        for lang in ("en", "ru", "uk"):
            set_user_interface_lang(self.db, GUEST, lang)
            state, update, context = self._show_variants()
            self.assertEqual(state, DB_SEARCH_VIEW)
            self.assertIn(t(lang, "search_exact"), update.message.calls[0][0])
            card = self._cards(update)[0]
            self.assertEqual(card.reply_markup.inline_keyboard[0][0].text, t(lang, "clarify_fields"))
            self.assertEqual(_run(V.search_callback(_Update(card=card), context)), ADD_EDIT)
            labels = _flat(update.message.calls[-1][1]["reply_markup"])
            self.assertNotIn(t(lang, "db_search_back_to_variants"), labels)


if __name__ == "__main__":
    unittest.main()
