"""Exact-mode/type statistics, owner counters and role-protected inline views."""

import asyncio
from contextlib import closing
from datetime import date
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from _telegram_stub import install
install()

from bot.common import stats_queries as Q
from bot.handlers import owner_stats as S
from bot.handlers.menu import menu_choice
from bot.i18n import t
from bot.storage import (
    add_word, get_statistics_snapshot, init_db, register_or_update_user,
    set_user_dict_lang, set_user_interface_lang, set_user_status,
)

OWNER, GUEST, OTHER = 999, 101, 202
MODES = ("uk_de", "phrase_uk_de", "de_uk", "phrase_de_uk")


def buttons(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def callback(view, uid=GUEST, claimed_uid=None):
    query = SimpleNamespace(data=f"stat:{view}", from_user=SimpleNamespace(id=claimed_uid or uid),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=uid))


class StatisticsTest(unittest.TestCase):
    def setUp(self):
        # Synthetic fixtures are retained inside the approved stand.
        self.db = str(Path(os.environ["TMPDIR"]) / f"stats-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": str(OWNER)})
        env.start(); self.addCleanup(env.stop)
        init_db(self.db)
        for uid in (GUEST, OTHER):
            register_or_update_user(self.db, uid, f"fixture{uid}", "Fixture")
            set_user_status(self.db, uid, "approved")
            set_user_dict_lang(self.db, uid, "uk")
        self.words = []
        for index in range(1, 5):
            self.words.append(add_word(self.db, {"id": f"w{index}", "type": "nomen",
                "article": "das", "german": f"Wort{index}", "russian": f"слово{index}", "topic": "1"}))
            self.words.append(add_word(self.db, {"id": f"p{index}", "type": "phrase",
                "german": f"Phrase {index}", "russian": f"фраза {index}", "topic": "1"}))
        self.today = date.today()
        with closing(sqlite3.connect(self.db)) as conn:
            # Establish legacy admission without introducing today's completions.
            for word in self.words:
                conn.execute("INSERT INTO quiz_events(user_id, word_id, mode, is_correct, created_at) VALUES(?,?,?,?,?)",
                             (GUEST, word["id"], "uk_de", 0, "2020-01-01T00:00:00Z"))
                for mode in MODES:
                    conn.execute("INSERT INTO progress(user_id, word_id, mode, due, repetitions, interval, ease) VALUES(?,?,?,?,?,0,2.5)",
                                 (GUEST, word["id"], mode, "2099-01-01", 0))
            for mode, ids in zip(MODES, (("w1", "w2"), ("p1",), ("w1", "w2", "w3"), ("p1", "p2"))):
                for wid in ids:
                    conn.execute("UPDATE progress SET due='2020-01-01' WHERE user_id=? AND word_id=? AND mode=?",
                                 (GUEST, wid, mode))
                    # Repeated wrong events are still one completed original card.
                    for _ in range(2):
                        conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES(?,?,?,?,?)",
                                     (GUEST, wid, mode, 0, self.today.isoformat()))
            # Misclassified historical events and unrelated modes/users must not leak.
            for uid, wid, mode in ((GUEST, "p3", "uk_de"), (GUEST, "w4", "phrase_de_uk"),
                                   (GUEST, "w4", "ru_de"), (GUEST, "w4", "conjugation"),
                                   (OTHER, "w4", "uk_de")):
                conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES(?,?,?,?,?)",
                             (uid, wid, mode, 0, self.today.isoformat()))
            conn.commit()

    def test_exact_due_and_distinct_today_have_four_independent_rows(self):
        breakdown = Q.progress_breakdown(self.db, GUEST, MODES, self.today)
        expected = dict(zip(MODES, (2, 1, 3, 2)))
        self.assertEqual(breakdown["due"], expected)
        self.assertEqual(breakdown["today"], expected)

    def test_correct_today_excludes_only_its_exact_due_mode(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct,created_at) VALUES(?,?,?,?,?)",
                         (GUEST, "w1", "uk_de", 1, self.today.isoformat()))
            conn.commit()
        data = Q.progress_breakdown(self.db, GUEST, MODES, self.today)
        self.assertEqual(data["due"]["uk_de"], 1)
        self.assertEqual(data["due"]["de_uk"], 3)
        self.assertEqual(data["today"]["uk_de"], 2)

    def test_progress_format_preserves_other_aggregates_and_requested_indentation(self):
        snapshot = get_statistics_snapshot(self.db, GUEST, "uk_de")
        text = S._format_progress_text(GUEST)
        self.assertIn("words   FSRS Ua > De due = 2", text)
        self.assertIn("phrases FSRS Ua > De due = 1", text)
        self.assertIn("words   FSRS De > Ua due = 3", text)
        self.assertIn("phrases FSRS De > Ua due = 2", text)
        self.assertIn("today completed:\nwords   fsrs Ua > De = 2\nphrases fsrs Ua > De = 1\nwords   fsrs De > Ua = 3\nphrases fsrs De > Ua = 2\n           pl > sing = 0", text)
        for key, value in (("stat_words_in_dict", snapshot["total_words"]),
                           ("stat_never_touched", snapshot["never_answered"]),
                           ("stat_wrong_fsrs", snapshot["wrong_fsrs"])):
            self.assertIn(f"{t('en', key)} = {value}", text)
        self.assertNotIn("word types:", text)
        self.assertNotIn("nomen = ", text)

    def test_total_and_type_counts_are_dynamic_and_separate_from_progress(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO words(id,type,german,russian) VALUES('extra',' Custom ','extra','x')")
            conn.execute("INSERT INTO words(id,type,german,russian) VALUES('blank','','blank','x')")
            conn.commit()
        counts = Q.dictionary_type_counts(self.db)
        self.assertEqual(counts, {"-": 1, "custom": 1, "nomen": 4, "phrase": 4})
        text = S._format_word_types_text("en")
        self.assertTrue(text.startswith("Total = 10\n\n"))
        self.assertEqual(sum(counts.values()), 10)

    def test_users_parenthetical_is_unique_progress_words_and_not_learning_status(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE progress SET repetitions=2 WHERE user_id=? AND word_id='w1'", (GUEST,))
            conn.execute("UPDATE progress SET repetitions=1 WHERE user_id=? AND word_id='p1' AND mode='phrase_uk_de'", (GUEST,))
            conn.execute("INSERT INTO progress(user_id,word_id,mode,repetitions,interval,ease,due) VALUES(?,?,?,?,0,2.5,'2099-01-01')",
                         (GUEST, "w1", "en_de", 3))
            conn.execute("INSERT INTO learning_items(user_id,word_id,batch_id,status,next_due) VALUES(?,?,?,?,'2020-01-01')",
                         (GUEST, "w4", "fixture", "skipped"))
            conn.execute("INSERT INTO learning_items(user_id,word_id,batch_id,status,next_due) VALUES(?,?,?,?,'2020-01-01')",
                         (OTHER, "w3", "fixture", "completed"))
            original_events = conn.execute("SELECT COUNT(*) FROM quiz_events WHERE user_id=?", (GUEST,)).fetchone()[0]
            conn.commit()
        self.assertEqual(Q.learned_word_counts(self.db).get(GUEST), 2)
        self.assertEqual(Q.learned_word_counts(self.db).get(OTHER, 0), 0)
        text = S._format_users_text("en")
        self.assertIn(f"@fixture{GUEST} — {original_events} (2)", text)
        self.assertIn(f"@fixture{OTHER} — 1 (0)", text)

    def test_role_specific_buttons_and_guest_menu_entry(self):
        self.assertEqual(buttons(S._stat_keyboard(OWNER)), ["progress", "users", "word types"])
        self.assertEqual(buttons(S._stat_keyboard(GUEST)), ["progress", "word types in db"])
        msg = SimpleNamespace(text=t("en", "stat"), reply_text=AsyncMock())
        upd = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=GUEST))
        asyncio.run(menu_choice(upd, SimpleNamespace(user_data={})))
        self.assertEqual(buttons(msg.reply_text.call_args.kwargs["reply_markup"]), ["progress", "word types in db"])

    def test_guest_types_and_progress_work_but_forged_users_never_reads_activity(self):
        for view in ("word_types", "progress"):
            upd = callback(view)
            asyncio.run(S.stat_toggle_callback(upd, None))
            upd.callback_query.edit_message_text.assert_awaited_once()
        upd = callback("users", claimed_uid=OWNER)
        with patch.object(S, "_format_users_text", side_effect=AssertionError("private view")):
            asyncio.run(S.stat_toggle_callback(upd, None))
        upd.callback_query.answer.assert_awaited_once()
        upd.callback_query.edit_message_text.assert_not_called()

    def test_unapproved_users_and_unknown_views_do_not_render(self):
        set_user_status(self.db, OTHER, "pending")
        for view, uid in (("word_types", OTHER), ("unexpected", OWNER)):
            upd = callback(view, uid)
            asyncio.run(S.stat_toggle_callback(upd, None))
            upd.callback_query.edit_message_text.assert_not_called()

    def test_conn_scoped_queries_do_not_commit_or_close_the_callers_transaction(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("BEGIN")
            conn.execute("UPDATE progress SET repetitions=1 WHERE user_id=? AND word_id='w1'", (GUEST,))
            self.assertEqual(Q.fsrs_word_counts(conn)[GUEST], 1)
            self.assertEqual(Q.count_today(conn, GUEST, "uk_de", self.today), 2)
            self.assertEqual(Q.word_type_counts(conn)["phrase"], 4)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        self.assertEqual(Q.learned_word_counts(self.db).get(GUEST, 0), 0)

    def test_all_interface_languages_localize_rows_and_leave_directions_on_dict_language(self):
        for lang in ("ru", "uk", "en"):
            set_user_interface_lang(self.db, GUEST, lang)
            text = S._format_progress_text(GUEST)
            self.assertIn(t(lang, "stat_today_heading"), text)
            self.assertIn(t(lang, "stat_words"), text)
            self.assertIn("Ua > De", text)
            self.assertIn("De > Ua", text)
            self.assertTrue(S._format_word_types_text(lang).startswith(t(lang, "stat_total") + " = 8\n\n"))


if __name__ == "__main__":
    unittest.main()
