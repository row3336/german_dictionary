"""spec #1 — preposition category (preposition_type) + governed case (case_type).

Covers the non-migration surface (migration itself is in test_migrations.py
::PrepositionColumnsMigrationTest):
  - quiz question hints (L1>De + De>L1) and the word card show the two fields;
  - storage dedup: a second meaning of the same preposition is a distinct word,
    an exact (german, preposition_type, case_type) triple is a duplicate;
  - controlled-vocabulary validation on every write path (AI normalize,
    _set_entry_field text path, update_word_fields storage guard);
  - owner-only clarify gating of the two fields + the preset cascade;
  - db_search match-pairs never pairs two different-meaning prepositions;
  - every entry-returning storage query carries the two columns, so the hint
    survives outside FSRS as well (learning/weak/menu lists).
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import QUIZ_RU_DE  # noqa: E402
from bot.handlers.add_words import _exists_word_for_type, _normalize_ai_entry  # noqa: E402
from bot.handlers.clarify_fields import (  # noqa: E402
    _allowed_clarify_fields,
    _set_entry_field,
    clarify_fields_input,
)
from bot.handlers.db_search import _list_match_pairs  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    learning_add_words,
    learning_due_entries,
    learning_get_or_create_batch,
    learning_mark_preview_next,
    learning_pick_new_words,
    learning_preview_entry,
    list_all_words,
    list_learning_active_words,
    list_learning_queue_words,
    list_weak_form_words,
    update_word_fields,
)
from bot.ui.formatting import (  # noqa: E402
    _format_de_ru_question,
    _format_ru_question,
    _format_word_card,
)

OWNER, GUEST = 999, 111


def _prep(german="in", ptype="temporal", case="dativ", **kw):
    e = {"type": "praeposition", "german": german, "russian": "в",
         "preposition_type": ptype, "case_type": case}
    e.update(kw)
    return e


class FormattingTest(unittest.TestCase):
    def test_l1_de_hint_folds_type_and_case(self):
        q = _format_ru_question(_prep(), "ru")
        self.assertIn("(praeposition, temporal, +Dativ)", q)

    def test_l1_de_hint_omits_missing_case(self):
        q = _format_ru_question(_prep(case=""), "ru")
        self.assertIn("(praeposition, temporal)", q)
        self.assertNotIn("+", q)

    def test_de_l1_hint_is_present_for_preposition(self):
        q = _format_de_ru_question(_prep(ptype="local", case="wechsel"))
        self.assertIn("(local, +D/A (Wechsel))", q)

    def test_de_l1_no_hint_when_unannotated(self):
        q = _format_de_ru_question(_prep(ptype="", case=""))
        # bare word, no empty parentheses
        self.assertNotIn("()", q)

    def test_case_labels(self):
        for case, label in (("dativ", "+Dativ"), ("akkusativ", "+Akkusativ"),
                            ("genitiv", "+Genitiv"), ("wechsel", "+D/A (Wechsel)")):
            self.assertIn(label, _format_ru_question(_prep(case=case), "ru"))

    def test_card_shows_both_fields_for_preposition(self):
        card = _format_word_card(_prep(ptype="local", case="wechsel"), user_id=None)
        self.assertIn("preposition_type: local", card)
        self.assertIn("case_type: wechsel", card)

    def test_card_hides_fields_for_non_preposition(self):
        card = _format_word_card({"type": "nomen", "german": "Haus", "russian": "дом"}, user_id=None)
        self.assertNotIn("preposition_type:", card)
        self.assertNotIn("case_type:", card)


class _DBTest(unittest.TestCase):
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

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _rows(self, german):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT preposition_type, case_type FROM words "
                "WHERE type='praeposition' AND german=? ORDER BY preposition_type",
                (german,),
            ).fetchall()
        finally:
            conn.close()


class DedupTest(_DBTest):
    def test_second_meaning_of_same_preposition_is_added(self):
        add_word(self.db, _prep("in", "local", "wechsel", russian="в, внутрь"))
        add_word(self.db, _prep("in", "temporal", "dativ", russian="через"))
        self.assertEqual(sorted(self._rows("in")), [("local", "wechsel"), ("temporal", "dativ")])

    def test_exact_triple_duplicate_is_rejected(self):
        add_word(self.db, _prep("mit", "modal", "dativ"))
        with self.assertRaises(ValueError):
            add_word(self.db, _prep("mit", "modal", "dativ"))

    def test_row_to_entry_carries_the_two_fields(self):
        add_word(self.db, _prep("um", "temporal", "akkusativ"))
        entry = next(e for e in list_all_words(self.db) if e.get("german") == "um")
        self.assertEqual(entry["preposition_type"], "temporal")
        self.assertEqual(entry["case_type"], "akkusativ")


class StorageValidationTest(_DBTest):
    def test_update_word_fields_accepts_known_vocab(self):
        add_word(self.db, _prep("zu", "", ""))
        wid = next(e for e in list_all_words(self.db) if e.get("german") == "zu")["id"]
        update_word_fields(self.db, wid, {"preposition_type": "local", "case_type": "dativ"})
        self.assertEqual(self._rows("zu"), [("local", "dativ")])

    def test_update_word_fields_drops_garbage(self):
        add_word(self.db, _prep("zu", "modal", "dativ"))
        wid = next(e for e in list_all_words(self.db) if e.get("german") == "zu")["id"]
        # garbage must not overwrite the curated value
        update_word_fields(self.db, wid, {"preposition_type": "bogus", "case_type": "nonsense"})
        self.assertEqual(self._rows("zu"), [("modal", "dativ")])

    def test_update_word_fields_empty_clears(self):
        add_word(self.db, _prep("zu", "modal", "dativ"))
        wid = next(e for e in list_all_words(self.db) if e.get("german") == "zu")["id"]
        update_word_fields(self.db, wid, {"preposition_type": "", "case_type": ""})
        self.assertEqual(self._rows("zu"), [("", "")])


class AINormalizeTest(unittest.TestCase):
    def test_valid_vocab_kept(self):
        out = _normalize_ai_entry(
            {"type": "praeposition", "german": "mit", "russian": "с",
             "preposition_type": "Modal", "case_type": "Dativ"}, "1")
        self.assertEqual(out["preposition_type"], "modal")
        self.assertEqual(out["case_type"], "dativ")

    def test_garbage_vocab_blanked(self):
        out = _normalize_ai_entry(
            {"type": "praeposition", "german": "x", "russian": "y",
             "preposition_type": "spatial", "case_type": "nominativ"}, "1")
        self.assertEqual(out["preposition_type"], "")
        self.assertEqual(out["case_type"], "")


class ExistsCheckTest(_DBTest):
    def test_preposition_not_pre_blocked(self):
        add_word(self.db, _prep("in", "local", "wechsel"))
        # a second meaning must not be pre-blocked by the add pipeline
        self.assertFalse(_exists_word_for_type("praeposition", "in"))


class MatchPairsTest(unittest.TestCase):
    def test_different_meanings_do_not_pair(self):
        entries = [
            _prep("in", "local", "wechsel", id="a", russian="в"),
            _prep("in", "temporal", "dativ", id="b", russian="через"),
        ]
        self.assertEqual(_list_match_pairs(entries, "ru"), [])

    def test_exact_triple_still_pairs(self):
        entries = [
            _prep("in", "local", "wechsel", id="a", russian="в"),
            _prep("in", "local", "wechsel", id="b", russian="в"),
        ]
        # a real duplicate (same triple + same translation) still surfaces
        self.assertGreaterEqual(len(_list_match_pairs(entries, "ru")), 1)


class ClarifyValidationTest(_DBTest):
    def test_set_entry_field_accepts_known(self):
        updated, err = _set_entry_field(_prep(), "preposition_type=local")
        self.assertIsNone(err)
        self.assertEqual(updated["preposition_type"], "local")

    def test_set_entry_field_rejects_unknown(self):
        _, err = _set_entry_field(_prep(), "preposition_type=bogus")
        self.assertIsNotNone(err)

    def test_set_entry_field_case_wechsel_ok(self):
        updated, err = _set_entry_field(_prep(), "case_type=Wechsel")
        self.assertIsNone(err)
        self.assertEqual(updated["case_type"], "wechsel")

    def test_owner_prep_fields_gated(self):
        with_prep = _allowed_clarify_fields("quiz", OWNER, is_prep=True)
        without = _allowed_clarify_fields("quiz", OWNER, is_prep=False)
        self.assertIn("preposition_type", with_prep)
        self.assertIn("case_type", with_prep)
        self.assertNotIn("preposition_type", without)

    def test_guest_never_gets_prep_fields(self):
        fields = _allowed_clarify_fields("quiz", GUEST, is_prep=True)
        self.assertEqual(fields, {"image", "example", "my_translation"})


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _User:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.first_name = ""


class _Update:
    def __init__(self, text, user):
        self.message = _Msg(text)
        self.effective_user = user


class _Bot:
    async def send_message(self, *a, **kw):
        pass


class _Ctx:
    def __init__(self, user_data):
        self.user_data = user_data
        self.bot = _Bot()


class DbSearchPrepKeyboardTest(unittest.TestCase):
    """bug #26: a preposition opened from db_search must offer preposition_type/
    case_type on the FIRST clarify screen — mirrors the db_search.py:558 call-site
    (`_clarify_fields_markup("quiz", ..., in_search=True, is_prep=_is_prep_entry(entry))`)."""

    def _flat(self, markup):
        rows = getattr(markup, "keyboard", None) or getattr(markup, "rows", None) or []
        return [getattr(b, "text", b) for row in rows for b in row]

    def test_prep_first_screen_has_prep_buttons(self):
        old = os.environ.get("OWNER_ID")
        os.environ["OWNER_ID"] = str(OWNER)
        try:
            from bot.handlers.clarify_fields import _is_prep_entry
            from bot.ui.keyboards import _clarify_fields_markup

            prep = self._flat(_clarify_fields_markup(
                "quiz", OWNER, in_search=True, is_prep=_is_prep_entry(_prep())))
            self.assertIn("preposition_type", prep)
            self.assertIn("case_type", prep)

            non_prep = self._flat(_clarify_fields_markup(
                "quiz", OWNER, in_search=True,
                is_prep=_is_prep_entry({"type": "nomen", "german": "Haus"})))
            self.assertNotIn("preposition_type", non_prep)
        finally:
            if old is None:
                os.environ.pop("OWNER_ID", None)
            else:
                os.environ["OWNER_ID"] = old


class ClarifyCascadeTest(_DBTest):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('p1','praeposition','in','в')")
        conn.commit()
        conn.close()

    def _ctx(self):
        return _Ctx({
            "edit_source": "quiz",
            "edit_stage": "enter_value",
            "edit_field": "type",
            "quiz_edit_entry": {"id": "p1", "german": "in", "type": "praeposition"},
            "edit_return_state": QUIZ_RU_DE,  # no quiz_entry -> resume falls back to tests menu
        })

    def _prep_row(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT preposition_type, case_type FROM words WHERE id='p1'"
            ).fetchone()
        finally:
            conn.close()

    def test_cascade_owner_type_to_category_to_case(self):
        ctx = self._ctx()
        # 1) setting type=praeposition arms the cascade and asks for the category
        upd = _Update("Praeposition", _User(OWNER))
        asyncio.run(clarify_fields_input(upd, ctx))
        self.assertTrue(any("category" in r for r in upd.message.replies))
        self.assertEqual(ctx.user_data["edit_field"], "preposition_type")
        self.assertTrue(ctx.user_data.get("prep_cascade"))

        # 2) picking the category chains to the case
        upd = _Update("Temporal", _User(OWNER))
        asyncio.run(clarify_fields_input(upd, ctx))
        self.assertTrue(any("case" in r.lower() for r in upd.message.replies))
        self.assertEqual(ctx.user_data["edit_field"], "case_type")

        # 3) picking the case ends the cascade and persists both fields
        upd = _Update("Dativ", _User(OWNER))
        asyncio.run(clarify_fields_input(upd, ctx))
        self.assertNotIn("prep_cascade", ctx.user_data)
        self.assertEqual(self._prep_row(), ("temporal", "dativ"))

    def test_cascade_rejects_bad_category(self):
        ctx = self._ctx()
        asyncio.run(clarify_fields_input(_Update("Praeposition", _User(OWNER)), ctx))
        upd = _Update("Bogus", _User(OWNER))
        asyncio.run(clarify_fields_input(upd, ctx))
        # stays on the category step, nothing persisted
        self.assertEqual(ctx.user_data["edit_field"], "preposition_type")
        self.assertEqual(self._prep_row(), ("", ""))


class EntryQueryContractTest(_DBTest):
    """The hint is built from the entry, so every query that feeds `_row_to_entry`
    must select both columns. Learning and weak used to omit them, and the hint
    silently collapsed to '(praeposition)' everywhere outside FSRS."""

    def _seed(self):
        add_word(self.db, _prep("aus", "local", "dativ", russian="из, с"))
        return next(e for e in list_all_words(self.db) if e.get("german") == "aus")["id"]

    def _assert_annotated(self, entries, where):
        entry = next((e for e in entries if e.get("type") == "praeposition"), None)
        self.assertIsNotNone(entry, f"{where}: preposition not returned")
        self.assertEqual(entry["preposition_type"], "local", where)
        self.assertEqual(entry["case_type"], "dativ", where)
        self.assertIn("(praeposition, local, +Dativ)", _format_ru_question(entry, "ru"), where)

    def test_learning_pick_preview_and_due_carry_the_fields(self):
        word_id = self._seed()
        picked = learning_pick_new_words(self.db, user_id=GUEST, limit=5)
        self._assert_annotated(picked, "learning_pick_new_words")

        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=5)
        learning_add_words(self.db, user_id=GUEST, words=picked, batch_id=batch_id, first_delay_seconds=0)
        self._assert_annotated([learning_preview_entry(self.db, GUEST)], "learning_preview_entry")
        self._assert_annotated(list_learning_active_words(self.db, GUEST), "list_learning_active_words")

        learning_mark_preview_next(self.db, user_id=GUEST, word_id=word_id, first_interval_seconds=0)
        self._assert_annotated(learning_due_entries(self.db, GUEST), "learning_due_entries")

    def test_learning_queue_list_carries_the_fields(self):
        self._seed()
        self._assert_annotated(list_learning_queue_words(self.db, GUEST), "list_learning_queue_words")

    def test_weak_form_words_carry_the_fields(self):
        word_id = self._seed()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO form_stats (user_id, word_id, article_active) VALUES (?, ?, 1)",
                (GUEST, word_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._assert_annotated(list_weak_form_words(self.db, GUEST), "list_weak_form_words")

    def test_no_entry_query_forgets_the_columns(self):
        """Source-level sweep: a new `_row_to_entry` query must not repeat the bug."""
        source = open(os.path.join(os.path.dirname(__file__), "..", "bot", "storage.py")).read()
        lines = source.split("\n")
        starts = [(i, ln) for i, ln in enumerate(lines) if ln.startswith("def ")]
        missing = []
        for idx, (start, ln) in enumerate(starts):
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
            body = "\n".join(lines[start:end])
            builds_entries = "_row_to_entry" in body and ("JOIN words w" in body or "FROM words w" in body)
            if builds_entries and "preposition_type" not in body:
                missing.append(ln.split("(")[0][4:])
        self.assertEqual(missing, [], f"queries feeding _row_to_entry without the hint columns: {missing}")


if __name__ == "__main__":
    unittest.main()
