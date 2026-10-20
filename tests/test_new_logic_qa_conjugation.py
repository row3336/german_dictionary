"""Independent pass fault matrix and real-dispatch reset delivery recovery."""

import ast
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path

from tests import test_new_logic_conjugation as fixture
from bot.conjugation.domain import check_form


def dispatch_script(send_failure=False):
    """Reuse only the real-PTB harness, varying its locale and failure boundary."""
    path = Path(__file__).with_name("test_new_logic_core_conversation.py")
    tree = ast.parse(path.read_text())
    source = next(node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and "class NoNetworkRequest" in node.value)
    script = textwrap.dedent(source).replace("UID = 7", "UID = 17")
    script = script.replace("'ru_de'", "'uk_de'").replace("'ru'", "'uk'")
    # This dispatch-only harness deliberately keeps its FakeService. Give the
    # global guard an actual approved-user/ledger schema instead of a fallback
    # physical DB path; use the same read-only inspector as production.
    setup = (
        "service = FakeService()\n"
        "    from tests.test_new_logic_conjugation import ConjugationTests\n"
        "    from bot.study import transport_guard\n"
        "    guard_fixture = ConjugationTests()\n"
        "    guard_fixture.setUp()\n"
        "    guard_fixture.conn.execute(\"INSERT INTO users VALUES(?, 'approved')\", (UID,))\n"
        "    guard_fixture.conn.commit()")
    assert script.count("service = FakeService()") == 1
    script = script.replace("service = FakeService()", setup, 1)
    needle = "with ExitStack() as stack:\n"
    replacement = (
        needle + "        stack.callback(guard_fixture.doCleanups)\n"
        "        stack.enter_context(patch.object(transport_guard, 'read_delivery',\n"
        "            side_effect=lambda db_path,uid,tid: transport_guard.inspect_delivery(guard_fixture.conn,uid,tid)))\n")
    assert script.count(needle) == 1
    script = script.replace(needle, replacement, 1)
    if send_failure:
        needle = "await app.process_update(reset)\n        assert not errors, errors"
        replacement = (
            "with patch.object(FakeBot, 'send_message', side_effect=RuntimeError('QA send failure')):\n"
            "            await app.process_update(reset)\n"
            "        assert len(errors) == 1 and str(errors[0]) == 'QA send failure', errors\n"
            "        errors.clear()\n"
            "        await app.process_update(reset)\n"
            "        assert not errors, errors")
        assert needle in script, "dispatch fixture changed; review the injection"
        script = script.replace(needle, replacement, 1)
        script = script.replace("bot.answered == ['reset-query']", "bot.answered == ['reset-query', 'reset-query']")
    return script


class IndependentConjugationQA(unittest.TestCase):
    setUp = fixture.ConjugationTests.setUp
    connection = fixture.ConjugationTests.connection
    delivery = fixture.ConjugationTests.delivery
    add = fixture.ConjugationTests.add
    prep = fixture.ConjugationTests.prep
    block = fixture.ConjugationTests.block
    ready = fixture.ConjugationTests.ready
    start = fixture.ConjugationTests.start
    grade = fixture.ConjugationTests.grade
    last_pass = fixture.ConjugationTests.last_pass
    long_interval = fixture.ConjugationTests.long_interval

    def test_each_error_position_applies_once_and_sixth_correction_is_one_success(self):
        for error_position in range(1, 7):
            with self.subTest(error_position=error_position):
                wid = f"qa-{error_position}"
                self.add(wid)
                self.ready(wid)
                self.long_interval(wid)
                view = self.start(session=wid)
                pid = view["question"]["pass_id"]
                for ordinal in range(1, 7):
                    view = self.grade(view, "wrong" if ordinal == error_position else None, session=wid)
                    b = self.block(wid)
                    self.assertEqual(b["repetitions"], 5 if ordinal < error_position else 4)
                before = self.block(wid)
                current = self.last_pass(pid)
                self.assertEqual(current["applied_result"], "wrong")
                self.assertEqual(current["status"], "completed")
                if error_position == 6:
                    fixed = self.service.correct_last(7, "ru_de", view["last_answer"]["id"], wid,
                        self.delivery(), current_question=view["question"], today=fixture.TODAY, now=fixture.NOW)
                    self.assertTrue(fixed["fixed"])
                    self.assertEqual(self.block(wid)["repetitions"], 6)
                    self.assertEqual(self.last_pass(pid)["applied_result"], "correct")
                else:
                    self.assertEqual(self.block(wid), before)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conjugation_answers WHERE pass_id=?", (pid,)).fetchone()[0], 6)

    def test_delivery_commit_failure_rolls_back_whole_form_then_retry_once(self):
        self.add()
        self.ready()
        self.long_interval()
        view = self.start()
        q = view["question"]
        before = self.block()
        delivery = self.delivery()
        self.conn.execute("""CREATE TRIGGER qa_fail_delivery BEFORE UPDATE ON study_deliveries
            WHEN OLD.action_kind='conjugation_answer'
            BEGIN SELECT RAISE(ABORT,'QA delivery failure'); END""")
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "QA delivery failure"):
            self.grade(view, "wrong", transport=delivery, key="qa-form")
        self.assertEqual(self.block(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conjugation_answers").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM study_deliveries WHERE transport_update_id=?", (delivery,)).fetchone())
        self.conn.execute("DROP TRIGGER qa_fail_delivery")
        self.conn.commit()
        result = self.grade(view, "wrong", transport=delivery, key="qa-form")
        replay = self.grade(result, transport=delivery, key="next-key")
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["last_answer"]["pass_id"], q["pass_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conjugation_answers").fetchone()[0], 1)

    def test_all_nine_and_reference_verbs_materialize_only_allowed_times(self):
        nine = ("können", "müssen", "wollen", "sollen", "dürfen", "mögen", "sein", "haben", "werden")
        for index, infinitive in enumerate(nine + ("gehen", "sprechen")):
            self.add(f"verb-{index}", infinitive=infinitive)
        self.prep()
        rows = self.conn.execute("SELECT w.infinitive,b.tense FROM conjugation_blocks b JOIN words w ON w.id=b.word_id").fetchall()
        self.assertEqual(len(rows), 20)
        self.assertEqual({r[0] for r in rows if r[1] == "praeteritum"}, set(nine))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM words WHERE conjugation_praeteritum<>''").fetchone()[0], 11)

    def test_partial_correction_next_day_finishes_once_with_new_effective_day(self):
        self.add()
        self.ready()
        self.long_interval()
        view = self.start()
        for _ in range(2):
            view = self.grade(view)
        view = self.grade(view, "wrong")
        last = view["last_answer"]
        fixed = self.service.correct_last(7, "ru_de", last["id"], "live", self.delivery(),
            current_question=view["question"], today=fixture.TODAY, now=fixture.NOW)
        self.assertTrue(fixed["fixed"])
        self.assertIsNone(self.last_pass(last["pass_id"])["result_day"])
        view = fixed
        for _ in range(3):
            view = self.grade(view, day=fixture.TODAY + timedelta(days=1))
        self.assertEqual(self.last_pass(last["pass_id"])["result_day"], (fixture.TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(self.block()["repetitions"], 6)

    def test_wrong_explicit_person_is_rejected_for_all_groups(self):
        for pronoun, form in fixture.FORMS.items():
            with self.subTest(pronoun=pronoun):
                self.assertTrue(check_form(form, pronoun, form))
                self.assertFalse(check_form("random " + form, pronoun, form))
                self.assertFalse(check_form(("du" if pronoun == "ich" else "ich") + " " + form, pronoun, form))

    def test_real_main_dispatch_state50_reset_then_next_in_other_pair(self):
        result = subprocess.run([sys.executable, "-B", "-c", dispatch_script()],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("network=0", result.stdout)


    def test_real_dispatch_reset_send_failure_replays_into_preparation(self):
        result = subprocess.run([sys.executable, "-B", "-c", dispatch_script(send_failure=True)],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("network=0", result.stdout)


    def test_expired_ack_on_committed_reset_replay_still_restores_dispatch(self):
        script = dispatch_script(send_failure=True)
        needle = "errors.clear()\n        await app.process_update(reset)\n        assert not errors, errors"
        replacement = (
            "errors.clear()\n"
            "        from telegram.error import BadRequest\n"
            "        with patch.object(FakeBot, 'answer_callback_query', side_effect=BadRequest('Query is too old and response timeout expired')):\n"
            "            await app.process_update(reset)\n"
            "        assert not errors, errors")
        self.assertIn(needle, script, "dispatch fixture changed; review the injection")
        script = script.replace(needle, replacement, 1)
        script = script.replace("bot.answered == ['reset-query', 'reset-query']", "bot.answered == ['reset-query']")
        result = subprocess.run([sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("network=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
