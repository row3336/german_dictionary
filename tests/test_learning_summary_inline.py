"""#35 — Learning summary is one live inline message; mix/Add/Reset edit it in place."""
import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from fsrs_menu_fake import Ctx, buttons, entry_update, press  # noqa: E402
from telegram.error import TimedOut  # noqa: E402

from bot.app_config import LEARNING_MENU, LEARNING_QUIZ, LEARNING_SETTINGS_MENU, TESTS_MENU  # noqa: E402
from bot.common.settings import _learning_mix_mode_enabled  # noqa: E402
from bot.handlers import learning as L  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import (add_word, init_db, learning_add_words, learning_get_or_create_batch,  # noqa: E402
                         register_or_update_user, set_topic_level, set_user_interface_lang,
                         set_user_setting, set_user_status, insert_topic_if_absent)

OWNER, A, B = 999, 111, 222


def u16(s):
    return len(s.encode("utf-16-le")) // 2


class LearningSummaryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda path=self.db: os.unlink(path))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ.update(DB_PATH=self.db, OWNER_ID=str(OWNER))
        self.addCleanup(self._restore)
        for uid in (A, B):
            register_or_update_user(self.db, uid, "u", "U")
            set_user_status(self.db, uid, "approved")
            set_user_interface_lang(self.db, uid, "en")
        set_topic_level(self.db, "1", "A1")
        self.n = 0

    def _restore(self):
        for k, v in self._old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def word(self, german=None, topic="1"):
        self.n += 1
        return add_word(self.db, {"type": "nomen", "article": "der", "german": german or f"Wort{self.n}",
                                  "plural": "x", "russian": f"слово{self.n}", "topic": topic})

    def activate(self, words, uid=A):
        batch = learning_get_or_create_batch(self.db, user_id=uid, daily_qty=10000)
        learning_add_words(self.db, user_id=uid, words=words, batch_id=batch, first_delay_seconds=0)

    def open(self, uid=A, ctx=None):
        ctx = ctx or Ctx()
        upd = entry_update("learning", uid)
        self.assertEqual(asyncio.run(L._send_learning_menu(upd, uid, ctx)), LEARNING_MENU)
        self.assertEqual(len(upd.message.sent), 1)
        return ctx, upd.message.sent[0]

    def press(self, ctx, uid, data, msg, **kw):
        upd_id = getattr(self, "_uid", 0) + 1
        self._uid = upd_id
        return press(L.learning_menu_callback, ctx, uid, data, msg, **kw)

    # --- entry ---------------------------------------------------------------
    def test_entry_sends_one_message_with_inline_buttons_and_correct_layout(self):
        self.activate([self.word("Apfel")])
        ctx, msg = self.open()
        self.assertEqual(buttons(msg), ["lrn:start", "lrn:mix", "lrn:add", "lrn:reset", "lrn:sett", "lrn:back"])
        lines = msg.text.splitlines()
        mix = next(i for i, ln in enumerate(lines) if "mix_mode" in ln)
        self.assertEqual(lines[mix], "⚪️ [OFF] mix_mode")
        self.assertLess(lines.index(next(l for l in lines if "🟥" in l)), mix)
        self.assertLess(mix, next(i for i, l in enumerate(lines) if l.startswith(t("en", "learning_daily_qty"))))
        self.assertIn("active = 1", msg.text)
        self.assertNotIn("*", msg.text)
        self.assertIsNone(msg.kw["parse_mode"])
        kinds = {e.type: e for e in msg.entities}
        pre = kinds["pre"]
        queue = msg.text[len(msg.text) - len("[A1:1] - [der Apfel]"):]
        self.assertEqual(queue, "[A1:1] - [der Apfel]")
        self.assertEqual(msg.text.encode("utf-16-le")[2 * pre.offset:2 * (pre.offset + pre.length)].decode("utf-16-le"), queue)
        bold = kinds["bold"]
        self.assertTrue(msg.text.encode("utf-16-le")[2 * bold.offset:2 * (bold.offset + bold.length)].decode("utf-16-le"))
        self.assertEqual(ctx.user_data["learning_menu"]["msg"], msg.message_id)

    def test_empty_queue_has_no_code_block(self):
        _, msg = self.open()
        self.assertTrue(msg.text.endswith("active words:\n-"))
        self.assertEqual([e.type for e in msg.entities], ["bold"])

    # --- mix / add / reset: same message, no sends -----------------------------
    def test_mix_toggles_twice_by_editing_same_message(self):
        ctx, msg = self.open()
        mid = msg.message_id
        for expected in (True, False):
            state, q = self.press(ctx, A, "lrn:mix", msg)
            self.assertEqual(state, LEARNING_MENU)
            self.assertEqual(_learning_mix_mode_enabled(A), expected)
            self.assertEqual((len(q.edits), msg.sent, msg.message_id), (1, [], mid))
            self.assertIn(("🟢 [ON] mix_mode" if expected else "⚪️ [OFF] mix_mode"), msg.text)
        self.assertFalse(_learning_mix_mode_enabled(B))

    def test_add_respects_filter_qty_and_refreshes_queue_in_place(self):
        words = [self.word(f"Add{i}") for i in range(3)]
        self.word("Other", topic="9")  # outside the user's topic filter
        set_user_setting(self.db, A, "learning_daily_qty", "2")
        ctx, msg = self.open()
        state, q = self.press(ctx, A, "lrn:add", msg)
        self.assertEqual(state, LEARNING_MENU)
        self.assertEqual((len(q.edits), msg.sent), (1, []))
        self.assertIn("active = 2", msg.text)
        self.assertEqual(msg.text.count("] - ["), 2)
        self.assertNotIn("Other", msg.text)
        self.assertNotIn(t("en", "learning_added").split(":")[0], msg.text)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM learning_items WHERE user_id=?", (B,)).fetchone()[0], 0)
        # a full batch is just shown again
        self.press(ctx, A, "lrn:add", msg)
        self.assertIn("active = 2", msg.text)

    def test_reset_clears_queue_and_counters_without_message(self):
        self.activate([self.word("Eins"), self.word("Zwei")])
        ctx, msg = self.open()
        self.assertIn("active = 2", msg.text)
        state, q = self.press(ctx, A, "lrn:reset", msg)
        self.assertEqual(state, LEARNING_MENU)
        self.assertEqual((len(q.edits), msg.sent), (1, []))
        self.assertIn("active = 0", msg.text)
        self.assertTrue(msg.text.endswith("active words:\n-"))
        self.assertNotIn(t("en", "learning_session_reset"), msg.text)

    # --- Start / Settings / Back, re-entry, stale ----------------------------
    def test_settings_and_back_keep_working_and_reentry_kills_old_buttons(self):
        ctx, msg = self.open()
        state, _ = self.press(ctx, A, "lrn:sett", msg)
        self.assertEqual(state, LEARNING_SETTINGS_MENU)
        self.assertEqual(len(msg.sent), 1)
        ctx2, newer = self.open(A, ctx)
        state, q = self.press(ctx, A, "lrn:mix", msg)  # old summary
        self.assertIsNone(state)
        self.assertEqual((q.edits, q.acks, _learning_mix_mode_enabled(A)), ([], 1, False))
        state, _ = self.press(ctx, A, "lrn:back", newer)
        self.assertEqual(state, TESTS_MENU)
        self.assertIn(t("en", "tests"), newer.sent[-1].text)

    def test_start_with_empty_queue_keeps_summary_state(self):
        ctx, msg = self.open()
        state, _ = self.press(ctx, A, "lrn:start", msg)
        self.assertEqual(state, LEARNING_MENU)
        self.assertEqual(len(msg.sent), 1)  # the "nothing to learn" notice only

    def test_foreign_revoked_forged_and_unknown_callbacks_change_nothing(self):
        ctx, msg = self.open()
        _, other = self.open(B, Ctx())
        state, q = self.press(Ctx(), B, "lrn:mix", msg)  # B pressing A's message with own context
        self.assertIsNone(state)
        self.assertFalse(_learning_mix_mode_enabled(B))
        forged = Ctx()
        forged.user_data["learning_menu"] = {"uid": A, "chat": msg.chat_id, "msg": msg.message_id}
        self.assertIsNone(self.press(forged, B, "lrn:mix", msg)[0])  # actor differs from stored owner
        self.assertIsNone(self.press(ctx, A, "lrn:bogus", msg)[0])
        set_user_status(self.db, A, "rejected")
        state, q = self.press(ctx, A, "lrn:mix", msg)
        self.assertEqual((state, q.edits, msg.sent), (None, [], []))
        self.assertFalse(_learning_mix_mode_enabled(A))

    def test_edit_failure_sends_nothing_and_next_render_is_truthful(self):
        ctx, msg = self.open()
        before = msg.text
        state, q = self.press(ctx, A, "lrn:mix", msg, fail_edit=TimedOut("down"))
        self.assertEqual((state, msg.sent, msg.text), (LEARNING_MENU, [], before))
        self.assertTrue(_learning_mix_mode_enabled(A))  # DB truth, shown by the next edit
        self.press(ctx, A, "lrn:reset", msg)
        self.assertIn("🟢 [ON] mix_mode", msg.text)

    def test_same_update_id_replay_is_idempotent(self):
        ctx, msg = self.open()
        from fsrs_menu_fake import Query
        for _ in range(2):
            q = Query("lrn:mix", A, msg)
            upd = type("Upd", (), {"callback_query": q, "update_id": 4242,
                                   "effective_user": type("U", (), {"id": A})()})()
            asyncio.run(L.learning_menu_callback(upd, ctx))
        self.assertTrue(_learning_mix_mode_enabled(A))  # toggled once, not back

    # --- queue budget -------------------------------------------------------
    def summary(self, rows_words):
        self.activate(rows_words)
        return L._learning_summary(A)

    def test_forty_and_150_short_rows_all_shown(self):
        for count in (40, 150):
            with self.subTest(count=count):
                self.setUp()
                text, entities, _ = self.summary([self.word(f"W{i}") for i in range(count)])
                self.assertEqual(text.count("] - ["), count)
                self.assertNotIn(" more", text)
                self.assertLessEqual(len(text), 4096)

    def test_overflow_reports_exact_hidden_count_and_fits_limit(self):
        words = [self.word("Ü" * 60 + str(i)) for i in range(120)]
        text, entities, _ = self.summary(words)
        shown = text.count("] - [")
        hidden = int(text.rsplit("... +", 1)[1].split()[0])
        self.assertEqual(shown + hidden, 120)
        self.assertLessEqual(len(text), 4096)
        self.assertGreater(len(text) + len("\n[A1:1] - [der " + "Ü" * 61 + "]"), 4096)  # one more row would not fit
        pre = next(e for e in entities if e.type == "pre")
        self.assertEqual(pre.offset + pre.length, u16(text))
        self.assertTrue(text.endswith(f"... +{hidden} more"))

    def test_exact_4096_fits_and_4097_loses_exactly_what_does_not_fit(self):
        self.activate([self.word("A" + "x")])
        short = L._learning_summary(A)[0]
        pad = 4096 - len(short)
        # one long word that brings the text to exactly 4096 characters (single entry)
        self.setUp()
        self.activate([self.word("B" + "x" * (pad + 1))])
        text, entities, _ = L._learning_summary(A)
        self.assertEqual(len(text), 4096)
        self.assertNotIn(" more", text)
        # one character more cannot fit as a row: it becomes `... +1 more` (entry hidden)
        self.setUp()
        self.activate([self.word("B" + "x" * (pad + 2))])
        text, entities, _ = L._learning_summary(A)
        self.assertLessEqual(len(text), 4096)
        self.assertTrue(text.endswith("... +1 more"))
        self.assertEqual(text.count("] - ["), 0)

    def test_supplementary_codepoints_count_once_for_limit_but_twice_for_offsets(self):
        self.activate([self.word("A")])
        pad = 4096 - len(L._learning_summary(A)[0])
        self.setUp()
        word = "😀" * (pad - 5)  # len() counts each emoji once, UTF-16 twice
        self.activate([self.word(word)])
        text, entities, _ = L._learning_summary(A)
        self.assertLessEqual(len(text), 4096)
        self.assertGreater(u16(text), 4096)  # a UTF-16 budget would have wrongly cut this row
        self.assertNotIn(" more", text)
        self.assertIn(word, text)
        pre = next(e for e in entities if e.type == "pre")
        self.assertEqual(pre.offset + pre.length, u16(text))

    def test_literal_punctuation_backticks_html_unicode_stay_literal(self):
        weird = "A`b\\c<b>&amp;ß😀"
        text, entities, _ = self.summary([self.word(weird)])
        self.assertIn(weird, text)
        self.assertNotIn("&lt;", text)
        pre = next(e for e in entities if e.type == "pre")
        raw = text.encode("utf-16-le")[2 * pre.offset:2 * (pre.offset + pre.length)].decode("utf-16-le")
        self.assertIn(weird, raw)

    def test_labels_are_unambiguous_across_books(self):
        for name, level in (("A2_1", "A2"), ("PONS_7", "PONS"), ("GSV", "B1"), ("1", "A1")):
            insert_topic_if_absent(self.db, name, "")
            set_topic_level(self.db, name, level)
        words = [self.word("eins", "1"), self.word("zwei", "A2_1"), self.word("drei", "PONS_7"), self.word("vier", "GSV")]
        text, _, _ = self.summary(words)
        for label in ("[A1:1]", "[A2:1]", "[PONS:07]", "[B1:reflexive]"):
            self.assertIn(label, text)

    def test_other_users_queue_not_shown_and_localized_title(self):
        self.activate([self.word("Mine")], A)
        self.activate([self.word("Theirs")], B)
        set_user_interface_lang(self.db, A, "ru")
        text, _, markup = L._learning_summary(A)
        self.assertNotIn("Theirs", text)
        self.assertTrue(text.startswith(t("ru", "learning") + ":"))
        self.assertEqual([b.text for row in markup.inline_keyboard for b in row][1], t("ru", "learning_mix_mode"))


if __name__ == "__main__":
    unittest.main()
