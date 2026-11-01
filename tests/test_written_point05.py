"""Point 5 acceptance: arithmetic, atomic SQL, replay, undo and live delivery."""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from _telegram_stub import install
install()

from bot.storage import _connect, init_db, set_progress, update_fsrs, default_srs
from bot.written import repository as repo, service as W, frame as F
from bot.written.logic import MODES, interval_divisor, reduce_answer

TODAY = date(2026, 11, 1)
UID = 900005
TABLES = ("progress", "quiz_events", "transaction_log", "study_actions", "study_deliveries")


def question(word="A", mode="ru_de", token="q1", retry=False):
    return {"user_id": UID, "flow_id": "flow", "revision": 1,
            "question_token": token, "mode": mode, "word_id": word,
            "retry": retry, "send_state": "confirmed"}


class ArithmeticTests(unittest.TestCase):
    def test_three_exact_examples_and_single_wrong(self):
        for interval, repetitions, ease, expected in (
                (30, 5, 2.5, (2, 2, 1.75)), (1, 0, 2.5, (1, 0, 1.75)),
                (10, 5, 2.0, (1, 2, 1.3))):
            before = dict(interval=interval, repetitions=repetitions, ease=ease, due="old")
            after = reduce_answer(before, "idk", "ru_de", TODAY)
            self.assertEqual(tuple(after[k] for k in ("interval", "repetitions", "ease")), expected)
            self.assertEqual(after["due"], date.fromordinal(TODAY.toordinal() + expected[0]).isoformat())
        self.assertEqual(reduce_answer(before, "wrong", "ru_de", TODAY),
                         update_fsrs(before, 2, today=TODAY))

    def test_exact_modes_divisors_and_no_article(self):
        for mode in MODES:
            expected = 1.65 if mode.removeprefix("phrase_") in {"ru_de", "uk_de", "en_de"} else 2.0
            self.assertEqual(interval_divisor(mode), expected)
            before = dict(interval=30, repetitions=5, ease=2.5, due="old")
            self.assertEqual(reduce_answer(before, "correct", mode, TODAY),
                             update_fsrs(before, 5, TODAY, interval_divisor=expected))
        with self.assertRaises(ValueError):
            interval_divisor("article")

    def test_new_learning_word_and_long_daily_plateau_preserved(self):
        state = reduce_answer(dict(interval=1, repetitions=0, ease=2.5),
                              "idk", "ru_de", TODAY)
        state = reduce_answer(state, "correct", "ru_de", TODAY)
        self.assertEqual(state["interval"], 1)
        self.assertEqual(reduce_answer(state, "correct", "ru_de", TODAY)["interval"], 3)
        for mode, daily in (("ru_de", 11), ("de_ru", 16)):
            state = reduce_answer(dict(interval=10, repetitions=5, ease=2.0), "idk", mode, TODAY)
            count = 0
            while state["interval"] == 1:
                state = reduce_answer(state, "correct", mode, TODAY)
                count += 1
            self.assertEqual(count, daily)


class StoreTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.db = handle.name
        handle.close()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        with _connect(self.db) as conn:
            for user in (UID, UID + 1):
                conn.execute("INSERT INTO users(user_id,status) VALUES(?,'approved')", (user,))
            for word in ("A", "B", "C"):
                conn.execute("""INSERT INTO words(id,type,article,german,plural,russian,ukrainian,english)
                    VALUES(?,'nomen','das',?,'Häuser','дом','дім','house')""", (word, word))

    def snapshot(self):
        with _connect(self.db) as conn:
            return {table: [tuple(r) for r in conn.execute("SELECT * FROM " + table + " ORDER BY 1,2")]
                    for table in TABLES}

    def answer(self, frame=None, update_id=1, kind="idk", mode="ru_de", uid=UID):
        frame = frame or question(mode=mode)
        return W.apply_original_answer(self.db, uid, frame["word_id"], mode, kind,
                                       frame, update_id, TODAY)

    def progress(self, mode="ru_de", uid=UID):
        with _connect(self.db) as conn:
            return repo.progress(conn, uid, "A", mode)

    def test_one_answer_authoritative_before_and_exact_event(self):
        before = dict(interval=30, repetitions=5, ease=2.5, due="2026-01-01")
        set_progress(self.db, UID, "A", "ru_de", before)
        result = self.answer()
        self.assertEqual(result["receipt"]["before"]["interval"], 30)
        self.assertEqual(self.progress()["interval"], 2)
        with _connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_replay_update_before_frame_and_kind_independent_action_key(self):
        first = self.answer()
        snapshot = self.snapshot()
        replay = W.apply_original_answer(self.db, UID, "nonexistent", "bad_mode", "wrong", None, 1, TODAY)
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(self.snapshot(), snapshot)
        replay = self.answer(update_id=2, kind="correct")
        self.assertEqual(replay["receipt"], first["receipt"])
        self.assertEqual(self.progress()["ease"], 1.75)
        with _connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_deliveries").fetchone()[0], 2)

    def test_correction_then_two_undos_restore_absence_and_exact_event(self):
        original = self.answer()["receipt"]
        frame = question(word="B", token="q2")
        frame["last_original_key"] = original["action_key"]
        correction = W.correct_last_answer(self.db, UID, "ru_de", original["action_key"], frame, 2, TODAY)["receipt"]
        self.assertEqual(correction["event_id"], original["event_id"])
        self.assertEqual(self.progress()["ease"], 2.6)
        self.assertEqual(W.undo_written_log(self.db, UID, correction["tx_id"])["status"], "restored")
        self.assertEqual(self.progress(), original["after"])
        self.assertEqual(W.undo_written_log(self.db, UID, original["tx_id"])["status"], "restored")
        self.assertIsNone(self.progress())
        self.assertEqual(W.find_delivery(self.db, UID, 1)["status"], "undone")
        self.assertEqual(self.answer(update_id=3)["status"], "undone")
        self.assertIsNone(self.progress())

    def test_correction_manual_same_mode_conflict_other_mode_allowed(self):
        original = self.answer()["receipt"]
        frame = question(word="B", token="q2")
        frame["last_original_key"] = original["action_key"]
        set_progress(self.db, UID, "A", "de_ru", dict(interval=40, repetitions=8, ease=2.5, due="2027-01-01"))
        same = dict(interval=50, repetitions=8, ease=2.5, due="2027-01-02")
        set_progress(self.db, UID, "A", "ru_de", same)
        snapshot = self.snapshot()
        result = W.correct_last_answer(self.db, UID, "ru_de", original["action_key"], frame, 2, TODAY)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.progress()["due"], same["due"])
        for table in ("progress", "quiz_events", "transaction_log", "study_actions"):
            self.assertEqual(self.snapshot()[table], snapshot[table])
        # Restoring the exact after-state removes the same-mode conflict only.
        with _connect(self.db) as conn:
            repo.write_progress(conn, UID, "A", "ru_de", original["after"])
        result = W.correct_last_answer(self.db, UID, "ru_de", original["action_key"], frame, 3, TODAY)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.progress(mode="de_ru")["interval"], 40)

    def test_retry_has_no_srs_event_tx_and_deduplicates(self):
        frame = question(retry=True)
        before, after = {"retry_queue": {"ru_de": ["A"]}}, {"retry_queue": {"ru_de": []}}
        result = W.apply_retry(self.db, UID, "A", "ru_de", "idk", frame, 1, before, after, TODAY)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(W.apply_retry(self.db, UID, "A", "ru_de", "wrong", frame, 2,
                                      before, after, TODAY)["status"], "replayed")
        for table in ("progress", "quiz_events", "transaction_log"):
            self.assertEqual(self.snapshot()[table], [])
        self.assertEqual(result["receipt"]["live_after"], after)

    def test_modes_and_users_isolated_prev_correct_scoped(self):
        for mode in MODES:
            self.answer(frame=question(mode=mode, token=mode), mode=mode,
                        update_id=100 + sorted(MODES).index(mode))
        other = question()
        other["user_id"] = UID + 1
        self.answer(frame=other, update_id=1, uid=UID + 1, kind="correct")
        original = self.answer(frame=question(token="q2"), update_id=2)["receipt"]
        self.assertEqual(original["event_after"]["prev_is_correct"], 0)
        self.assertEqual(self.progress(uid=UID + 1)["ease"], 2.6)
        with _connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0], len(MODES) + 1)

    def test_invalid_frame_unapproved_deleted_word_and_stale_undo(self):
        for frame in (question(mode="de_ru"), dict(question(), send_state="uncertain"),
                      dict(question(), consumed=True), dict(question(), user_id=UID + 1)):
            with self.assertRaises(W.StaleWrittenAction):
                self.answer(frame=frame)
        with _connect(self.db) as conn:
            conn.execute("UPDATE users SET status='pending' WHERE user_id=?", (UID,))
        with self.assertRaises(PermissionError):
            self.answer()
        with _connect(self.db) as conn:
            conn.execute("UPDATE users SET status='approved' WHERE user_id=?", (UID,))
        receipt = self.answer()["receipt"]
        with _connect(self.db) as conn:
            conn.execute("DELETE FROM words WHERE id='A'")
        self.assertEqual(W.undo_written_log(self.db, UID, receipt["tx_id"])["status"], "skipped")
        with _connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM words WHERE id='A'").fetchone())

    def test_pinned_undo_plan_replay_and_resume_new_update(self):
        a = self.answer()["receipt"]
        b = self.answer(frame=question("B", token="q2"), update_id=2)["receipt"]
        plan = W.start_undo_plan(self.db, UID, 10, 1)
        self.assertEqual(plan["result"]["log_ids"], [b["tx_id"]])
        self.answer(frame=question("C", token="q3"), update_id=3)
        alias = W.start_undo_plan(self.db, UID, 11, 20)
        self.assertEqual(alias["transport_update_id"], 10)
        W.undo_written_log(self.db, UID, b["tx_id"], 10)
        self.assertEqual(W.start_undo_plan(self.db, UID, 10, 20)["state"], "done")
        self.assertEqual(W.start_undo_plan(self.db, UID, 11, 20)["state"], "done")
        with _connect(self.db) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM transaction_log")]
        self.assertIn(a["tx_id"], ids)
        self.assertEqual(len(ids), 2)

    def test_historical_article_and_unchanged_state_numbers(self):
        from bot import app_config as C
        self.assertFalse(hasattr(C, "QUIZ_ARTICLE"))
        self.assertEqual((C.QUIZ_RU_DE, C.QUIZ_DE_RU, C.QUIZ_PL_SG, C.SELECT_UI_LANG,
                          C.LEARNING_QUIZ, C.VF_APPLY_CONFIRM), (13, 14, 15, 17, 30, 49))
        set_progress(self.db, UID, "A", "article", default_srs(TODAY))
        with _connect(self.db) as conn:
            conn.execute("INSERT INTO quiz_events(user_id,word_id,mode,is_correct) VALUES(?,'A','article',0)", (UID,))
            conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES(?,'quiz:article','{}')", (UID,))
            historical_event = tuple(conn.execute("SELECT * FROM quiz_events WHERE mode='article'").fetchone())
            historical_log = tuple(conn.execute("SELECT * FROM transaction_log WHERE action='quiz:article'").fetchone())
        self.answer()
        with _connect(self.db) as conn:
            self.assertEqual(tuple(conn.execute("SELECT * FROM quiz_events WHERE mode='article'").fetchone()), historical_event)
            self.assertEqual(tuple(conn.execute("SELECT * FROM transaction_log WHERE action='quiz:article'").fetchone()), historical_log)
        self.assertIsNotNone(self.progress("article"))
        with _connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT article FROM words WHERE id='A'").fetchone()[0], "das")

    def test_existing_v18_copy_keeps_schema_and_old_due_unchanged(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        copied = handle.name
        handle.close()
        self.addCleanup(lambda: os.unlink(copied))
        set_progress(self.db, UID, "B", "de_uk", dict(interval=77, repetitions=9, ease=2.2, due="2027-03-01"))
        from bot.common.db_safety import set_user_version
        set_user_version(self.db, 18)
        with _connect(self.db) as source_conn, _connect(copied) as target_conn:
            source_conn.backup(target_conn)
        with _connect(copied) as conn:
            old_progress = repo.progress(conn, UID, "B", "de_uk")
            old_schema = [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
        result = W.apply_original_answer(copied, UID, "A", "ru_de", "idk", question(), 1, TODAY)
        self.assertEqual(result["status"], "applied")
        with _connect(copied) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(repo.progress(conn, UID, "B", "de_uk"), old_progress)
            self.assertEqual([tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")], old_schema)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_fault_after_every_sql_step_original_correction_retry_undo(self):
        real_transaction = repo.transaction

        @contextmanager
        def injecting(fail_at, count):
            @contextmanager
            def wrapped(path):
                with real_transaction(path) as conn:
                    class Connection:
                        def execute(self, *args, **kwargs):
                            result = conn.execute(*args, **kwargs)
                            count[0] += 1
                            if count[0] == fail_at:
                                raise sqlite3.OperationalError("injected SQL failure")
                            return result
                    yield Connection()
            with patch.object(repo, "transaction", wrapped):
                yield

        for operation in ("original", "correction", "retry", "undo", "undo_existing", "undo_correction", "planned_undo"):
            with self.subTest(operation=operation):
                # Each injected attempt starts from exactly the same baseline.
                with _connect(self.db) as conn:
                    for table in reversed(TABLES):
                        conn.execute("DELETE FROM " + table)
                original = None
                if operation == "undo_existing":
                    set_progress(self.db, UID, "A", "ru_de", dict(interval=30, repetitions=5, ease=2.5, due="2000-01-01"))
                if operation in {"correction", "undo", "undo_existing", "undo_correction", "planned_undo"}:
                    original = self.answer()["receipt"]
                frame = question(word="B", token="q2")
                frame["last_original_key"] = original["action_key"] if original else None
                if operation == "undo_correction":
                    original = W.correct_last_answer(self.db, UID, "ru_de", original["action_key"], frame, 2, TODAY)["receipt"]
                if operation == "planned_undo":
                    W.start_undo_plan(self.db, UID, 10, 1)

                def perform():
                    if operation == "original":
                        return self.answer(update_id=9)
                    if operation == "correction":
                        return W.correct_last_answer(self.db, UID, "ru_de", original["action_key"], frame, 9, TODAY)
                    if operation == "retry":
                        return W.apply_retry(self.db, UID, "A", "ru_de", "idk", question(retry=True),
                                             9, {"queue": ["A"]}, {"queue": []}, TODAY)
                    return W.undo_written_log(self.db, UID, original["tx_id"], 10 if operation == "planned_undo" else None)

                baseline = self.snapshot()
                # Count a successful path while deliberately rolling its writes back.
                count = [0]
                @contextmanager
                def rollback_transaction(path):
                    with real_transaction(path) as conn:
                        class Connection:
                            def execute(self, *args, **kwargs):
                                result = conn.execute(*args, **kwargs)
                                count[0] += 1
                                return result
                        yield Connection()
                        raise RuntimeError("probe rollback")
                with self.assertRaises(RuntimeError):
                    with patch.object(repo, "transaction", rollback_transaction):
                        perform()
                self.assertEqual(self.snapshot(), baseline)
                for index in range(1, count[0] + 1):
                    with self.subTest(sql_step=index):
                        with self.assertRaises(sqlite3.OperationalError):
                            with injecting(index, [0]):
                                perform()
                        self.assertEqual(self.snapshot(), baseline)


class LiveTests(unittest.TestCase):
    """Run actual adapters and queue logic against SQLite; only Telegram is fake."""

    def setUp(self):
        StoreTests.setUp(self)
        self.environment = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": "999"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with _connect(self.db) as conn:
            conn.execute("UPDATE words SET german='Haus' WHERE id='A'")
            conn.execute("UPDATE words SET german='Buch',plural='Bücher' WHERE id='B'")
            conn.execute("UPDATE users SET interface_lang='en',dict_lang='ru' WHERE user_id=?", (UID,))
        self.ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(set_message_reaction=self.reaction))
        self.sends = []
        self.outcomes = []
        self.pick = []

    snapshot = StoreTests.snapshot
    answer = StoreTests.answer
    progress = StoreTests.progress

    async def reaction(self, **kwargs):
        pass

    def update(self, update_id, text="idk ;("):
        async def reply(text, **kwargs):
            self.sends.append((text, kwargs))
            result = self.outcomes.pop(0) if self.outcomes else SimpleNamespace(message_id=len(self.sends))
            if isinstance(result, Exception):
                raise result
            return result
        return SimpleNamespace(update_id=update_id,
                               effective_user=SimpleNamespace(id=UID),
                               effective_chat=SimpleNamespace(id=UID),
                               message=SimpleNamespace(text=text, message_id=update_id, reply_text=reply))

    async def open(self, direction="ru_de"):
        from bot.handlers import quiz as Q
        from bot.common.session import _reset_session
        mode = {"ru_de": "ru_de", "de_ru": "de_ru", "pl_sg": "pl_sg_ru"}[direction]
        _reset_session(self.ctx, mode)
        with _connect(self.db) as conn:
            from bot.storage import _row_to_entry
            self.pick = [_row_to_entry(r) for r in conn.execute("SELECT * FROM words WHERE id IN ('A','B') ORDER BY id")]
        def picker(*args, **kwargs):
            return self.pick.pop(0) if self.pick else None
        self.picker = patch.object(Q, "_pick_due_entry", picker)
        self.picker.start()
        self.addCleanup(self.picker.stop)
        await getattr(Q, "_send_" + direction + "_prompt")(self.update(100, ""), self.ctx, None)

    def test_uncertain_send_repeats_same_prepared_question_without_grade(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open()
            self.outcomes = [None]
            await Q.quiz_ru_de(self.update(1), self.ctx)
            frame = deepcopy(self.ctx.user_data["written_frame"])
            self.assertEqual(frame["word_id"], "B")
            self.assertEqual(frame["send_state"], "uncertain")
            await Q.quiz_ru_de(self.update(2, "das Buch Bücher"), self.ctx)
            self.assertEqual(self.ctx.user_data["written_frame"]["question_token"], frame["question_token"])
            self.assertEqual(self.ctx.user_data["written_frame"]["send_state"], "confirmed")
            self.assertEqual(self.sends[-1][0], frame["text"])
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
            await Q.quiz_ru_de(self.update(2, "das Buch Bücher"), self.ctx)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
            await Q.quiz_ru_de(self.update(3, "das Buch Bücher"), self.ctx)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 2)
        asyncio.run(run())

    def test_failed_send_resend_and_sql_failure_preserve_question(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open()
            frame = deepcopy(self.ctx.user_data["written_frame"])
            data = deepcopy(self.ctx.user_data)
            with patch.object(repo, "save_delivery", side_effect=sqlite3.OperationalError("failure")):
                with self.assertRaises(sqlite3.OperationalError):
                    await Q.quiz_ru_de(self.update(1), self.ctx)
            self.assertEqual(self.ctx.user_data["written_frame"]["question_token"], frame["question_token"])
            for key in ("retry_queue", "retry_counts", "session_wrong_ru_de", "session_seen_ru_de"):
                self.assertEqual(self.ctx.user_data.get(key), data.get(key))
            self.outcomes = [RuntimeError("known pre-send failure")]
            with self.assertRaises(RuntimeError):
                await Q.quiz_ru_de(self.update(2), self.ctx)
            self.assertEqual(self.ctx.user_data["written_frame"]["send_state"], "failed")
            self.assertNotEqual(self.ctx.user_data["written_frame"]["question_token"], frame["question_token"])
            await Q.quiz_ru_de(self.update(3, "input_err"), self.ctx)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
        asyncio.run(run())

    def test_input_err_preserves_other_B_and_cancels_retry_A(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open()
            await Q.quiz_ru_de(self.update(1), self.ctx)
            token = self.ctx.user_data["written_frame"]["question_token"]
            await Q.quiz_ru_de(self.update(2, "input_err"), self.ctx)
            self.assertEqual(self.ctx.user_data["written_frame"]["word_id"], "B")
            self.assertEqual(self.ctx.user_data["written_frame"]["question_token"], token)
            await Q.quiz_ru_de(self.update(2, "input_err"), self.ctx)
            self.assertEqual(self.ctx.user_data["written_frame"]["question_token"], token)
            self.assertEqual(self.ctx.user_data["session_correct_ru_de"], 1)
            self.assertEqual(self.ctx.user_data["session_wrong_ru_de"], 0)
            self.assertEqual(self.ctx.user_data["retry_queue"]["ru_de"], [])
            self.assertEqual(self.progress()["ease"], 2.6)
            await Q.quiz_ru_de(self.update(3), self.ctx)
            self.assertEqual(self.ctx.user_data["written_frame"]["word_id"], "B")
            self.assertTrue(self.ctx.user_data["written_frame"]["retry"])
            await Q.quiz_ru_de(self.update(4, "input_err"), self.ctx)
            self.assertIsNone(self.ctx.user_data["written_frame"]["word_id"])
            self.assertEqual(self.ctx.user_data["retry_queue"]["ru_de"], [])
        asyncio.run(run())

    def test_pl_sg_tail_retry_does_not_write_srs_and_replay(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open("pl_sg")
            self.pick.clear()
            await Q.quiz_pl_sg(self.update(1), self.ctx)
            self.assertTrue(self.ctx.user_data["written_frame"]["retry"])
            count = len(self.snapshot()["quiz_events"])
            token = self.ctx.user_data["written_frame"]["question_token"]
            await Q.quiz_pl_sg(self.update(2), self.ctx)
            self.assertEqual(len(self.snapshot()["quiz_events"]), count)
            new_token = self.ctx.user_data["written_frame"]["question_token"]
            self.assertNotEqual(token, new_token)
            counts = deepcopy(self.ctx.user_data["retry_counts"])
            await Q.quiz_pl_sg(self.update(2), self.ctx)
            self.assertEqual(self.ctx.user_data["retry_counts"], counts)
        asyncio.run(run())

    def test_uncertain_final_summary_repeats_and_stop_closes(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open()
            self.pick.clear()
            self.outcomes = [None]
            await Q.quiz_ru_de(self.update(1, "das Haus Häuser"), self.ctx)
            frame = self.ctx.user_data["written_frame"]
            self.assertIsNone(frame["word_id"])
            summary = frame["text"]
            await Q.quiz_ru_de(self.update(2), self.ctx)
            self.assertEqual(self.sends[-1][0], summary)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
            await Q.quiz_ru_de(self.update(3, "Stop"), self.ctx)
            self.assertNotIn("written_frame", self.ctx.user_data)
            self.assertNotIn("written_last_original", self.ctx.user_data)
        asyncio.run(run())

    def test_late_confirmation_cannot_confirm_new_frame(self):
        async def run():
            F.prepare(self.ctx, UID, "ru_de", {"id": "A"}, "A", {})
            async def send(message, text, **kwargs):
                F.prepare(self.ctx, UID, "ru_de", {"id": "B"}, "B", {})
                return SimpleNamespace(message_id=1)
            await F.deliver(self.update(1), self.ctx, send)
            self.assertEqual(self.ctx.user_data["written_frame"]["word_id"], "B")
            self.assertEqual(self.ctx.user_data["written_frame"]["send_state"], "planned")
        asyncio.run(run())

    def test_restart_old_text_rescues_menu_without_restoring_receipt(self):
        from bot.handlers.learning import learning_resume_or_rescue
        from bot.app_config import MENU
        self.answer()
        async def run():
            for n, text in enumerate(("old answer", "idk ;(", "input_err"), 10):
                self.ctx.user_data.clear()
                state = await learning_resume_or_rescue(self.update(n, text), self.ctx)
                self.assertEqual(state, MENU)
                self.assertNotIn("written_frame", self.ctx.user_data)
                self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
        asyncio.run(run())


    def test_manual_add_int_conflict_message_preserves_queue_and_event(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open()
            await Q.quiz_ru_de(self.update(1), self.ctx)
            before_queue = deepcopy(self.ctx.user_data["retry_queue"])
            before_event = self.snapshot()["quiz_events"]
            from bot.handlers import clarify_fields as CF
            from bot.app_config import QUIZ_RU_DE
            from bot.labels import SYN_CONFIRM_TEXT
            self.ctx.user_data.update(edit_source="quiz", edit_stage="pick_field",
                quiz_edit_entry=self.ctx.user_data["last_answered_entry"], edit_return_state=QUIZ_RU_DE)
            await CF.clarify_fields_input(self.update(20, "add_int"), self.ctx)
            await CF.clarify_fields_input(self.update(21, "5"), self.ctx)
            await CF.clarify_fields_input(self.update(22, SYN_CONFIRM_TEXT), self.ctx)
            manual = self.progress()
            await CF._resume_after_clarify(self.update(23, "back"), self.ctx, None)
            await Q.quiz_ru_de(self.update(24, "input_err"), self.ctx)
            self.assertIn("changed manually", self.sends[-1][0])
            self.assertEqual(self.progress(), manual)
            self.assertEqual(self.ctx.user_data["retry_queue"], before_queue)
            self.assertEqual(self.snapshot()["quiz_events"], before_event)
        asyncio.run(run())

    def test_concurrent_updates_share_lock_and_uncertain_question_is_ungraded(self):
        from bot.handlers import quiz as Q
        async def run():
            await self.open()
            self.outcomes = [None]
            await asyncio.gather(Q.quiz_ru_de(self.update(1), self.ctx),
                                 Q.quiz_ru_de(self.update(2), self.ctx))
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
            self.assertEqual(self.ctx.user_data["written_frame"]["word_id"], "B")
            self.assertEqual(self.ctx.user_data["written_frame"]["send_state"], "confirmed")
            await Q.quiz_ru_de(self.update(3), self.ctx)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 2)
        asyncio.run(run())

    def test_send_learn_is_blocked_until_question_is_confirmed(self):
        from bot.handlers import quiz as Q, learning as L
        async def run():
            await self.open()
            self.ctx.user_data["written_frame"]["send_state"] = "uncertain"
            with patch.object(L, "_send_entry_to_learning") as send_learn:
                await Q.quiz_ru_de(self.update(1, "send_learn"), self.ctx)
            send_learn.assert_not_called()
            self.assertEqual(self.snapshot()["quiz_events"], [])
            self.assertEqual(self.ctx.user_data["written_frame"]["word_id"], "A")
        asyncio.run(run())

    def test_postcommit_publication_failure_safely_closes_without_regrade(self):
        from bot.handlers import quiz as Q
        from bot.app_config import MENU
        async def run():
            await self.open()
            with patch.object(Q, "_remember_last_answer", side_effect=RuntimeError("publication failed")):
                with self.assertRaises(RuntimeError):
                    await Q.quiz_ru_de(self.update(1), self.ctx)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
            self.assertEqual(await Q.quiz_ru_de(self.update(1), self.ctx), MENU)
            self.assertNotIn("written_frame", self.ctx.user_data)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
        asyncio.run(run())

    def test_start_back_and_new_flow_drop_correction_right(self):
        from bot.handlers import quiz as Q
        from bot.handlers.menu import start
        async def run():
            await self.open()
            await Q.quiz_ru_de(self.update(1), self.ctx)
            old_flow = self.ctx.user_data["written_frame"]["flow_id"]
            await start(self.update(2, "/start"), self.ctx)
            self.assertNotIn("written_frame", self.ctx.user_data)
            self.assertNotIn("written_last_original", self.ctx.user_data)
            await Q._send_ru_de_prompt(self.update(3), self.ctx, None)
            self.assertNotEqual(self.ctx.user_data["written_frame"]["flow_id"], old_flow)
            await Q.quiz_ru_de(self.update(4, "back"), self.ctx)
            self.assertNotIn("written_frame", self.ctx.user_data)
        asyncio.run(run())

    def test_mixed_undo_N_and_duplicate_update_do_not_consume_older_log(self):
        import bot.main as M
        from bot.storage import (snapshot_user_word_state, update_progress,
                                 add_transaction_log)
        from bot import learning_store as L
        snap = snapshot_user_word_state(self.db, UID, "C", ["ru_de"])
        update_progress(self.db, UID, "C", "ru_de", 5)
        add_transaction_log(self.db, UID, "quiz:ru_de", {"snapshot": snap})
        with _connect(self.db) as conn:
            old_id = conn.execute("SELECT MAX(id) FROM transaction_log WHERE user_id=?", (UID,)).fetchone()[0]
        self.answer()
        with _connect(self.db) as conn:
            conn.execute("INSERT INTO learning_batches(id,user_id) VALUES('mix',?)", (UID,))
            conn.execute("""INSERT INTO learning_items
                (user_id,word_id,batch_id,step_index,preview_done,next_due,status,cycle_token)
                VALUES(?,'B','mix',3,1,'2000-01-01T00:00:00Z','active','cycle')""", (UID,))
        display = L.start_display(self.db, UID)
        L.mark_sent(self.db, UID, display, "confirmed", 1)
        L.apply_action(self.db, UID, 2, display["action_key"], "wrong",
                       [0, 0, 0, 60, 120], "wrong")
        snap = snapshot_user_word_state(self.db, UID, "C", ["ru_de"])
        update_progress(self.db, UID, "C", "ru_de", 5)
        add_transaction_log(self.db, UID, "add_int:ru_de", {"snapshot": snap})
        self.ctx.args = ["3"]
        async def run():
            await M.undo_steps(self.update(10, "/undo 3"), self.ctx)
            with _connect(self.db) as conn:
                logs = [r[0] for r in conn.execute("SELECT id FROM transaction_log")]
                recovery = conn.execute("SELECT recovery_remaining FROM learning_items WHERE word_id='B'").fetchone()[0]
            self.assertEqual(logs, [old_id])
            self.assertEqual(recovery, 0)
            self.assertIsNone(self.progress())
            await M.undo_steps(self.update(10, "/undo 3"), self.ctx)
            with _connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 1)
            self.ctx.args = []
            await M.undo_steps(self.update(11, "/undo"), self.ctx)
            with _connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_log").fetchone()[0], 0)
        asyncio.run(run())

    def test_failed_undo_plan_resumes_pinned_id_before_newer_action(self):
        import bot.main as M
        self.answer()
        self.ctx.args = []
        async def run():
            with patch.object(W, "undo_written_log", side_effect=sqlite3.OperationalError("undo failed")):
                await M.undo_steps(self.update(10, "/undo"), self.ctx)
            self.answer(frame=question("B", token="q2"), update_id=2)
            await M.undo_steps(self.update(11, "/undo"), self.ctx)
            self.assertIsNone(self.progress())
            with _connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT word_id FROM progress").fetchone()[0], "B")
            await M.undo_steps(self.update(11, "/undo"), self.ctx)
            with _connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM progress").fetchone()[0], 1)
        asyncio.run(run())


    def test_correction_publication_failure_closes_before_new_answer(self):
        from bot.handlers import quiz as Q
        from bot.app_config import MENU
        async def run():
            await self.open()
            await Q.quiz_ru_de(self.update(1), self.ctx)
            with patch.object(Q, "_flip_session_result_to_correct", side_effect=RuntimeError("publication failed")):
                with self.assertRaises(RuntimeError):
                    await Q.quiz_ru_de(self.update(2, "input_err"), self.ctx)
            self.assertEqual(self.progress()["ease"], 2.6)
            self.assertEqual(await Q.quiz_ru_de(self.update(3, "das Buch Bücher"), self.ctx), MENU)
            self.assertEqual(len(self.snapshot()["quiz_events"]), 1)
        asyncio.run(run())

    def test_dictionary_language_selects_mode_independently_of_interface(self):
        from bot.handlers import quiz as Q
        from bot.common.session import _reset_session
        from bot.storage import get_word_by_id, set_user_dict_lang, set_user_interface_lang
        async def run():
            update_id = 1
            for lang, ui in (("ru", "uk"), ("uk", "en"), ("en", "ru")):
                set_user_dict_lang(self.db, UID, lang)
                set_user_interface_lang(self.db, UID, ui)
                for base in (lang + "_de", "de_" + lang, "pl_sg_" + lang):
                    modes = [base] if base.startswith("pl_") else [base, "phrase_" + base]
                    for mode in modes:
                        direction = "pl_sg" if mode.startswith("pl_") else ("de_ru" if base.startswith("de_") else "ru_de")
                        self.ctx.user_data.clear()
                        self.ctx.user_data["quiz_mode"] = mode
                        _reset_session(self.ctx, mode)
                        set_progress(self.db, UID, "A", mode, dict(interval=30, repetitions=5, ease=2.5, due="2000-01-01"))
                        entry = get_word_by_id(self.db, "A")
                        with patch.object(Q, "_pick_due_entry", side_effect=[entry, None]):
                            await getattr(Q, "_send_" + direction + "_prompt")(self.update(1000 + update_id), self.ctx, None)
                            await getattr(Q, "quiz_" + direction)(self.update(update_id), self.ctx)
                        self.assertEqual(self.ctx.user_data["session_wrong_" + mode], 1)
                        self.assertEqual(self.progress(mode)["interval"], 2)
                        self.assertEqual(self.progress(mode)["ease"], 1.75)
                        update_id += 1
            self.assertEqual(len(self.snapshot()["quiz_events"]), 15)
        asyncio.run(run())

    def test_completed_undo_replay_retains_original_summary_after_later_undo(self):
        import bot.main as M
        self.answer()
        self.answer(frame=question("B", token="q2"), update_id=2)
        self.ctx.args = []
        async def run():
            await M.undo_steps(self.update(10, "/undo"), self.ctx)
            first_summary = self.sends[-1][0]
            await M.undo_steps(self.update(11, "/undo"), self.ctx)
            self.assertIn("2 step", self.sends[-1][0])
            await M.undo_steps(self.update(10, "/undo"), self.ctx)
            self.assertEqual(self.sends[-1][0], first_summary)
            self.assertEqual(self.snapshot()["transaction_log"], [])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
