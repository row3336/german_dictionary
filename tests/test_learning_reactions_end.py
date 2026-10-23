"""Learning 👍/👎 reactions + the plain end-of-queue service message.

* Learning answers get a flat 👍 (correct) / 👎 (wrong) reaction on the user's
  message (same contract as FSRS quizzes, no streak escalation); the answer
  message id is remembered so input_err can lift the 👎.
* When the running queue has nothing to show now, the trailing feedback (Markdown)
  and the plain service tail are sent as TWO SEPARATE messages. The tail carries
  'next_due', whose underscore would open an unterminated italic entity under
  parse_mode='Markdown' — so the tail MUST be sent plain. Regression guard: the
  merged-Markdown closing message crashed live with "Can't parse entities".
* Empty queue on entry (nothing previewed, nothing due) must not enter the quiz
  state at all — it stays in the learning menu with the service message.
"""
import asyncio
import os
import sqlite3
import tempfile
import types
import unittest
import warnings

from bot.app_config import LEARNING_MENU, LEARNING_QUIZ
from bot.common.lang import _user_interface_lang
from bot.handlers.learning import (
    _apply_learning_reaction,
    _learning_empty_text,
    _send_learning_prompt,
    learning_menu_choice,
    learning_quiz,
)
from bot.i18n import t
from bot.storage import (
    add_word,
    init_db,
    learning_add_words,
    learning_get_or_create_batch,
    register_or_update_user,
    set_user_status,
)

warnings.filterwarnings("ignore", category=ResourceWarning)

GUEST = 111
THUMBS_UP, THUMBS_DOWN = "\U0001F44D", "\U0001F44E"


def _markdown_entities_valid(text):
    """Faithful-enough check of Telegram legacy Markdown entity balance.

    Catches exactly what crashed live: a lone (unterminated) or empty `*`, `_`
    or `` ` `` entity. Honours backslash escaping. Returns True when every
    delimiter pairs up and no pair is empty.
    """
    for delim in ("*", "_", "`"):
        positions = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == delim:
                positions.append(i)
            i += 1
        if len(positions) % 2 != 0:
            return False  # lone/unterminated entity — the live crash
        for open_idx, close_idx in zip(positions[0::2], positions[1::2]):
            if close_idx == open_idx + 1:
                return False  # empty entity, e.g. **
    return True


class _Bot:
    def __init__(self, raise_always=False):
        self.reactions = []
        self.raise_always = raise_always

    async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
        if self.raise_always:
            raise RuntimeError("Bot API down")
        self.reactions.append((chat_id, message_id, reaction))


class _Msg:
    def __init__(self, message_id=1, text=""):
        self.message_id = message_id
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return types.SimpleNamespace(message_id=self.message_id + 1000)


class _Update:
    def __init__(self, uid, message_id=1, text=""):
        self.update_id = 100
        self.message = _Msg(message_id, text)
        self.effective_user = types.SimpleNamespace(id=uid, username="u", first_name="U")
        self.effective_chat = types.SimpleNamespace(id=uid)


class _Ctx:
    def __init__(self, user_data=None, bot=None):
        self.user_data = user_data or {}
        self.bot = bot or _Bot()


def _run(coro):
    return asyncio.run(coro)


class LearningAnswerReactionTest(unittest.TestCase):
    def test_correct_answer_reacts_thumbs_up_and_remembers_message(self):
        update = _Update(GUEST, message_id=42)
        ctx = _Ctx()
        _run(_apply_learning_reaction(update, ctx, True))
        self.assertEqual(ctx.bot.reactions, [(GUEST, 42, [THUMBS_UP])])
        self.assertEqual(ctx.user_data["last_learning_answer_message_id"], 42)

    def test_wrong_answer_reacts_thumbs_down(self):
        update = _Update(GUEST, message_id=43)
        ctx = _Ctx()
        _run(_apply_learning_reaction(update, ctx, False))
        self.assertEqual(ctx.bot.reactions, [(GUEST, 43, [THUMBS_DOWN])])

    def test_reaction_failure_does_not_raise(self):
        update = _Update(GUEST, message_id=44)
        ctx = _Ctx(bot=_Bot(raise_always=True))
        _run(_apply_learning_reaction(update, ctx, True))  # must not raise


class _LearningDbTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = "999"
        self.addCleanup(self._restore)
        self._word = add_word(
            self.db,
            {"id": "unique", "type": "nomen", "article": "der", "german": "Ingenieur",
             "plural": "Ingenieure", "russian": "инженер", "ukrainian": "інженер", "english": "engineer", "topic": "1"},
        )
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='A1' WHERE name='1'")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _enqueue(self, status, next_due, step_index=0):
        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=10)
        learning_add_words(self.db, user_id=GUEST, words=[self._word], batch_id=batch_id, first_delay_seconds=0)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE learning_items SET status=?, preview_done=1, first_due_pending=0, next_due=?, step_index=? WHERE user_id=? AND word_id=?",
                (status, next_due, step_index, GUEST, self._word["id"]),
            )

    def _item(self):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT step_index, next_due, status FROM learning_items WHERE user_id=? AND word_id=?",
                (GUEST, self._word["id"]),
            ).fetchone()
        return {"step": int(row[0]), "next_due": row[1], "status": row[2]}


class LearningEndOfQueueTest(_LearningDbTest):
    def test_cleared_session_sends_feedback_then_plain_tail_no_fire(self):
        # one word completed -> nothing active/due. Feedback (Markdown) and the
        # service tail go as TWO messages; no 🔥 effect, no merge.
        self._enqueue(status="completed", next_due="2000-01-01T00:00:00Z")
        update = _Update(GUEST)
        ctx = _Ctx()
        state = _run(_send_learning_prompt(update, ctx, "feedback *ok*"))
        self.assertEqual(state, LEARNING_QUIZ)
        self.assertEqual(len(update.message.calls), 2)  # feedback + tail, separate
        (fb_text, fb_kw), (tail_text, tail_kw) = update.message.calls
        self.assertEqual(fb_text, "feedback *ok*")
        self.assertEqual(fb_kw.get("parse_mode"), "Markdown")   # feedback keeps Markdown
        self.assertNotIn("message_effect_id", tail_kw)          # no fire
        self.assertNotIn("message_effect_id", fb_kw)
        # completion -> no next_due readout (nothing pending)
        self.assertNotIn("next_due", tail_text)

    def test_pending_tail_carries_next_due_and_is_plain_not_markdown(self):
        # one word still active but not due -> come-back-later readout. This is the
        # exact message that crashed live: 'next_due' under parse_mode='Markdown'.
        self._enqueue(status="active", next_due="2099-01-01T00:00:00Z")
        update = _Update(GUEST)
        ctx = _Ctx()
        _run(_send_learning_prompt(update, ctx, "feedback"))
        tail_text, tail_kw = update.message.calls[-1]
        self.assertIn("next_due", tail_text)
        # REGRESSION GUARD: the tail must NOT be sent as Markdown (its underscore
        # would be an unterminated entity -> BadRequest "Can't parse entities").
        self.assertNotEqual(tail_kw.get("parse_mode"), "Markdown")

    def test_every_markdown_message_is_entity_balanced(self):
        # Whatever we send with parse_mode='Markdown' must be valid legacy Markdown.
        self._enqueue(status="active", next_due="2099-01-01T00:00:00Z")
        update = _Update(GUEST)
        ctx = _Ctx()
        _run(_send_learning_prompt(update, ctx, "feedback *bold* _it_"))
        for text, kw in update.message.calls:
            if kw.get("parse_mode") == "Markdown":
                self.assertTrue(_markdown_entities_valid(text), f"unbalanced Markdown: {text!r}")

    def test_empty_text_would_break_under_markdown(self):
        # Documents WHY the tail is plain: the service text itself is invalid Markdown.
        self._enqueue(status="active", next_due="2099-01-01T00:00:00Z")
        self.assertIn("next_due", _learning_empty_text(GUEST))
        self.assertFalse(_markdown_entities_valid(_learning_empty_text(GUEST)))


class LearningInputErrTest(_LearningDbTest):
    def test_input_err_on_last_interval_word_advances_once_and_ends_cycle(self):
        # last word at an interval step (3), wrong answer -> keep step 3, recovery 3 (+0s)
        # -> re-asked as the already selected next card. Pressing input_err must
        # correct the previous answer (advance 3 -> 4) and discard that stale
        # card because the corrected step is due in the future.
        lang = _user_interface_lang(GUEST)
        self._enqueue(status="active", next_due="2000-01-01T00:00:00Z", step_index=3)
        update = _Update(GUEST)
        ctx = _Ctx()
        # show the due word (quiz_entry carries learning_step = 3)
        _run(_send_learning_prompt(update, ctx, None))
        self.assertEqual(int(ctx.user_data["quiz_entry"].get("learning_step")), 3)

        # answer WRONG -> keep step 3 and immediately re-ask for recovery
        update.message.text = "zzz-wrong"
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        update.message.calls.clear()
        _run(learning_quiz(update, ctx))
        self.assertEqual(self._item()["step"], 3)
        self.assertEqual(ctx.user_data["last_learning_prev_step"], 3)

        # press input_err -> corrected as if right from step 3 -> step 4 (future)
        # and the cycle ends instead of keeping the stale recovery card
        update.message.text = t(lang, "input_err")
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        update.message.calls.clear()
        _run(learning_quiz(update, ctx))
        item = self._item()
        self.assertEqual(item["step"], 4)                 # advanced once (3 -> 4), no double
        self.assertGreater(item["next_due"], "2026")      # rescheduled into the future, not due
        joined = "\n".join(c[0] for c in update.message.calls)
        self.assertIn(t(lang, "input_err_fixed"), joined)
        self.assertIn(t(lang, "learning_no_due").rstrip("."), joined)
        self.assertNotIn("quiz_entry", ctx.user_data)

    def test_input_err_on_last_warmup_word_reloads_current_step(self):
        # Correcting a wrong answer from step 0 advances to zero-delay step 1.
        # The word may be shown again, but the card must carry step 1 so the next
        # correct answer advances exactly once from the current DB state.
        lang = _user_interface_lang(GUEST)
        self._enqueue(status="active", next_due="2000-01-01T00:00:00Z", step_index=0)
        update = _Update(GUEST)
        ctx = _Ctx()
        _run(_send_learning_prompt(update, ctx, None))

        update.message.text = "zzz-wrong"
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        _run(learning_quiz(update, ctx))
        update.message.text = t(lang, "input_err")
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        update.message.calls.clear()
        _run(learning_quiz(update, ctx))

        self.assertEqual(self._item()["step"], 1)
        self.assertEqual(int(ctx.user_data["quiz_entry"].get("learning_step")), 1)

        update.message.text = "der Ingenieur Ingenieure"
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        _run(learning_quiz(update, ctx))
        self.assertEqual(self._item()["step"], 2)

    def test_second_input_err_is_a_noop(self):
        # guard against double-advance via repeated input_err presses
        lang = _user_interface_lang(GUEST)
        self._enqueue(status="active", next_due="2000-01-01T00:00:00Z", step_index=3)
        update = _Update(GUEST)
        ctx = _Ctx()
        _run(_send_learning_prompt(update, ctx, None))
        update.message.text = "zzz-wrong"
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        _run(learning_quiz(update, ctx))
        update.message.text = t(lang, "input_err")
        update.update_id += 1  # A distinct Telegram delivery for this new action.
        _run(learning_quiz(update, ctx))
        self.assertEqual(self._item()["step"], 4)
        # second distinct press: nothing to fix
        update.update_id += 1
        update.message.calls.clear()
        _run(learning_quiz(update, ctx))
        self.assertEqual(self._item()["step"], 4)          # unchanged
        joined = "\n".join(c[0] for c in update.message.calls)
        self.assertIn(t(lang, "learning_no_due").rstrip("."), joined)


class LearningEmptyEntryTest(_LearningDbTest):
    def test_start_on_empty_queue_stays_in_menu(self):
        # Nothing previewed, nothing due -> pressing "learning session" must NOT
        # enter the quiz state; it stays in the menu with the service message.
        lang = _user_interface_lang(GUEST)
        update = _Update(GUEST, text=t(lang, "learning_start"))
        ctx = _Ctx()
        state = _run(learning_menu_choice(update, ctx))
        self.assertEqual(state, LEARNING_MENU)
        self.assertEqual(len(update.message.calls), 1)
        text, kw = update.message.calls[0]
        self.assertIn(t(lang, "learning_no_due").rstrip("."), text)
        self.assertNotEqual(kw.get("parse_mode"), "Markdown")
        self.assertNotIn("message_effect_id", kw)


if __name__ == "__main__":
    unittest.main()
