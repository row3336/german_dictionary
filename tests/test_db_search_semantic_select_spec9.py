"""Spec #9 — exact semantic prepositions are selected by saved word id."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from _telegram_stub import install

install()

from bot.app_config import ADD_EDIT, DB_SEARCH  # noqa: E402
from bot.handlers import clarify_fields as C  # noqa: E402
from bot.handlers import db_search as D  # noqa: E402
from bot.i18n import TEXTS, t  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    register_or_update_user,
    set_user_interface_lang,
)


OWNER = 999
GUEST = 111


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return type("Sent", (), {"message_id": len(self.calls)})()


class _Update:
    def __init__(self, text="", uid=GUEST):
        self.message = _Msg(text)
        self.effective_user = type(
            "User", (), {"id": uid, "username": "guest", "first_name": "Guest"}
        )()
        self.effective_chat = type("Chat", (), {"id": uid})()


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

    def _restore_env(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _seed_vor(self):
        local = add_word(self.db, _prep("vor", "local", "wechsel", "перед"))
        temporal = add_word(self.db, _prep("vor", "temporal", "dativ", "до"))
        return local, temporal

    def _show_variants(self, context=None):
        context = context or _Ctx()
        update = _Update("vor")
        state = _run(D.db_search_input(update, context))
        return state, update, context

    def test_two_exact_cards_show_keyboard_then_each_saved_id_and_return_without_query(self):
        local, temporal = self._seed_vor()
        state, update, context = self._show_variants()

        self.assertEqual(state, DB_SEARCH)
        self.assertFalse(any("Word card:" in text for text, _ in update.message.calls))
        prompt, kwargs = update.message.calls[-1]
        self.assertEqual(prompt, t("ru", "db_search_choose_variant"))
        labels = _flat(kwargs["reply_markup"])
        self.assertIn("vor [local, +D/A]", labels)
        self.assertIn("vor [temporal, +Dativ]", labels)

        for label, expected_id in (
            ("vor [local, +D/A]", local["id"]),
            ("vor [temporal, +Dativ]", temporal["id"]),
        ):
            fresh_state, _, fresh_context = self._show_variants()
            self.assertEqual(fresh_state, DB_SEARCH)
            chosen = _Update(label)
            with patch.object(
                D, "_select_db_search_exact", side_effect=AssertionError("selection repeated exact search")
            ), patch.object(D, "search_word", side_effect=AssertionError("selection repeated LIKE search")):
                self.assertEqual(_run(D.db_search_input(chosen, fresh_context)), ADD_EDIT)
            self.assertEqual(fresh_context.user_data["quiz_edit_entry"]["id"], expected_id)
            card_markup = chosen.message.calls[-1][1]["reply_markup"]
            self.assertIn(t("ru", "db_search_back_to_variants"), _flat(card_markup))

        chosen = _Update("vor [local, +D/A]")
        self.assertEqual(_run(D.db_search_input(chosen, context)), ADD_EDIT)
        back = _Update(t("ru", "db_search_back_to_variants"))
        with patch.object(D, "list_all_words", side_effect=AssertionError("return queried DB")), patch.object(
            D, "search_word", side_effect=AssertionError("return repeated search")
        ), patch.object(D, "get_word_by_id", side_effect=AssertionError("return queried id")):
            self.assertEqual(_run(C.clarify_fields_input(back, context)), DB_SEARCH)
        self.assertEqual(back.message.calls[-1][0], t("ru", "db_search_choose_variant"))
        self.assertIn("vor [local, +D/A]", _flat(back.message.calls[-1][1]["reply_markup"]))

    def test_single_exact_keeps_direct_card_flow(self):
        only = add_word(self.db, _prep("aus", "local", "dativ", "из"))
        context = _Ctx()
        update = _Update("aus")

        self.assertEqual(_run(D.db_search_input(update, context)), ADD_EDIT)
        self.assertEqual(context.user_data["quiz_edit_entry"]["id"], only["id"])
        self.assertTrue(any("Word card:" in text for text, _ in update.message.calls))
        self.assertNotIn("db_search_exact_variants", context.user_data)

    def test_invalid_label_and_stale_id_do_not_fall_through_to_search(self):
        self._seed_vor()
        _, _, context = self._show_variants()

        invalid = _Update("not a saved label")
        with patch.object(D, "_select_db_search_exact", side_effect=AssertionError("new exact search")), patch.object(
            D, "search_word", side_effect=AssertionError("LIKE search")
        ), patch.object(D, "get_word_by_id", side_effect=AssertionError("invalid label resolved")):
            self.assertEqual(_run(D.db_search_input(invalid, context)), DB_SEARCH)
        self.assertEqual(invalid.message.calls[-1][0], t("ru", "db_search_variant_unavailable"))
        self.assertFalse(any("Word card:" in text for text, _ in invalid.message.calls))

        changed = _Update("vor [local, +D/A]")
        saved_id = context.user_data["db_search_exact_variants"]["label_to_id"][changed.message.text]
        with patch.object(D, "get_word_by_id", return_value={
            "id": saved_id,
            "type": "nomen",
            "german": "fremd",
        }):
            self.assertEqual(_run(D.db_search_input(changed, context)), DB_SEARCH)
        self.assertEqual(changed.message.calls[-1][0], t("ru", "db_search_variant_unavailable"))
        self.assertFalse(any("Word card:" in text for text, _ in changed.message.calls))

        context.user_data["db_search_exact_variants"]["label_to_id"]["vor [local, +D/A]"] = "missing-id"
        stale = _Update("vor [local, +D/A]")
        self.assertEqual(_run(D.db_search_input(stale, context)), DB_SEARCH)
        self.assertEqual(stale.message.calls[-1][0], t("ru", "db_search_variant_unavailable"))
        self.assertFalse(any("Word card:" in text for text, _ in stale.message.calls))

    def test_duplicate_labels_fail_safely_and_log_ids(self):
        duplicate_rows = [
            {"id": "p1", **_prep("vor", "local", "dativ", "перед")},
            {"id": "p2", **_prep("vor", "local", "dativ", "возле")},
        ]
        context = _Ctx()
        update = _Update("vor")

        with patch.object(D, "_select_db_search_exact", return_value=duplicate_rows), self.assertLogs(
            D.logger.name, level="WARNING"
        ) as logs:
            self.assertEqual(_run(D.db_search_input(update, context)), DB_SEARCH)
        self.assertEqual(update.message.calls[-1][0], t("ru", "db_search_variant_collision"))
        self.assertIn("p1", "\n".join(logs.output))
        self.assertIn("p2", "\n".join(logs.output))
        self.assertNotIn("db_search_exact_variants", context.user_data)
        self.assertFalse(any("Word card:" in text for text, _ in update.message.calls))

    def test_phrase_side_matches_and_like_fallback_stay_direct(self):
        add_word(self.db, {"type": "nomen", "article": "die", "german": "Tafel", "russian": "доска", "topic": "1"})
        add_word(self.db, {"type": "phrase", "german": "die Tafel Schokolade", "russian": "плитка шоколада", "topic": "1"})

        exact = _Update("Tafel")
        self.assertEqual(_run(D.db_search_input(exact, _Ctx())), ADD_EDIT)
        self.assertTrue(any("Phrase matches:" in text for text, _ in exact.message.calls))

        like = _Update("afel")
        self.assertEqual(_run(D.db_search_input(like, _Ctx())), ADD_EDIT)
        self.assertTrue(any("Word card:" in text for text, _ in like.message.calls))

    def test_variant_copy_is_localized_for_all_interface_languages(self):
        keys = (
            "db_search_choose_variant",
            "db_search_back_to_variants",
            "db_search_variant_unavailable",
            "db_search_variant_collision",
        )
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
            self.assertEqual(state, DB_SEARCH)
            self.assertEqual(update.message.calls[-1][0], t(lang, "db_search_choose_variant"))
            chosen = _Update("vor [local, +D/A]")
            self.assertEqual(_run(D.db_search_input(chosen, context)), ADD_EDIT)
            self.assertIn(
                t(lang, "db_search_back_to_variants"),
                _flat(chosen.message.calls[-1][1]["reply_markup"]),
            )

        self.assertEqual(
            D._format_semantic_variant_label(
                {"german": "VOR", "preposition_type": "LOCAL", "case_type": "WECHSEL"}
            ),
            "vor [local, +D/A]",
        )


if __name__ == "__main__":
    unittest.main()
