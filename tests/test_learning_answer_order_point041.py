"""Point 4.1 acceptance: one answer per word in preview order, and the closed Add.

A wrong answer must not cut into the words still waiting for their first answer
(bug #48). These checks drive the real Learning handlers on a temporary database;
only Telegram and the store clock are replaced. Words in one Add share created_at,
so the assertions compare with the order actually shown on preview.
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from _telegram_stub import install

install()

from bot import learning_store as store  # noqa: E402
from bot.app_config import I_DONT_KNOW_TEXT, LEARNING_MENU  # noqa: E402
from bot.common.settings import _set_learning_mix_mode  # noqa: E402
from bot.handlers import learning as L  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.learning_logic import stamp  # noqa: E402
from bot.storage import (  # noqa: E402
    _connect,
    add_word,
    init_db,
    learning_add_words,
    learning_get_or_create_batch,
    learning_reset_session,
    register_or_update_user,
    set_user_status,
)
from bot.ui.keyboards import _learning_menu_keys  # noqa: E402

UID = 111
START = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3)
LETTERS = "ABCDEFGHIJ"


class _Clock(datetime):
    current = START

    @classmethod
    def now(cls, tz=None):
        return cls.current


class _Message:
    def __init__(self, text):
        self.text = text
        self.message_id = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=1)


class _Base(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        self.db = handle.name
        self.addCleanup(lambda: os.unlink(self.db))
        environment = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": "999"})
        environment.start()
        self.addCleanup(environment.stop)
        _Clock.current = START
        store_clock = patch.object(store, "datetime", _Clock)
        store_clock.start()
        self.addCleanup(store_clock.stop)
        init_db(self.db)
        register_or_update_user(self.db, UID, "u", "U")
        set_user_status(self.db, UID, "approved")
        self.set_lang("en")
        self.update_id = 1000
        self.tick = 5
        self.ctx = self.new_context()
        self.words = {letter: self.word(letter) for letter in LETTERS}
        self.letter_of = {entry["id"]: letter for letter, entry in self.words.items()}

    def new_context(self):
        async def reaction(**kwargs):
            pass

        return SimpleNamespace(user_data={}, bot=SimpleNamespace(set_message_reaction=reaction))

    def set_lang(self, lang):
        with _connect(self.db) as conn:
            conn.execute("UPDATE users SET interface_lang=?,dict_lang='ru' WHERE user_id=?", (lang, UID))

    def word(self, letter):
        german = letter * 3
        return add_word(self.db, {"type": "nomen", "article": "das", "german": german.capitalize(),
                                  "plural": german.capitalize() + "s", "russian": "ru" + letter,
                                  "topic": "1"})

    def add(self, letters):
        batch = learning_get_or_create_batch(self.db, UID, 20)
        learning_add_words(self.db, user_id=UID, batch_id=batch, first_delay_seconds=0,
                           words=[self.words[letter] for letter in letters])

    def update(self, text):
        self.update_id += 1
        return SimpleNamespace(update_id=self.update_id, message=_Message(text),
                               effective_user=SimpleNamespace(id=UID, username="u", first_name="U"),
                               effective_chat=SimpleNamespace(id=UID))

    def press(self, text):
        _Clock.current = _Clock.current + timedelta(seconds=self.tick)
        update = self.update(text)
        asyncio.run(L.learning_quiz(update, self.ctx))
        return update

    def open(self):
        update = self.update("start")
        asyncio.run(L._send_learning_prompt(update, self.ctx, None))
        return update

    def current(self):
        display = store.get_display(self.db, UID)
        return self.letter_of.get(display["word_id"]) if display else None

    def preview(self, count):
        shown = []
        for _ in range(count):
            shown.append(self.current())
            self.press(t("en", "learning_next"))
        return shown

    def answer(self, ok=True):
        letter = self.current()
        german = letter * 3
        self.press(f"das {german.capitalize()} {german.capitalize()}s" if ok else "zzz")
        return letter

    def answers(self, wrong=(), count=0):
        asked = []
        for _ in range(count):
            asked.append(self.answer(self.current() not in wrong or self.current() in asked))
        return asked

    def rows(self):
        with _connect(self.db) as conn:
            return {
                "items": [tuple(row) for row in conn.execute(
                    "SELECT * FROM learning_items ORDER BY word_id")],
                "batches": [tuple(row) for row in conn.execute(
                    "SELECT * FROM learning_batches ORDER BY id")],
            }

    def item(self, letter):
        with _connect(self.db) as conn:
            return dict(conn.execute("SELECT * FROM learning_items WHERE user_id=? AND word_id=?",
                                     (UID, self.words[letter]["id"])).fetchone())

    def set_item(self, letter, **fields):
        names = ",".join(name + "=?" for name in fields)
        with _connect(self.db) as conn:
            conn.execute(f"UPDATE learning_items SET {names} WHERE user_id=? AND word_id=?",
                         (*fields.values(), UID, self.words[letter]["id"]))

    def last_log(self):
        with _connect(self.db) as conn:
            return conn.execute("SELECT MAX(id) FROM transaction_log WHERE user_id=?", (UID,)).fetchone()[0]


class AnswerOrderTests(_Base):
    def start_group(self):
        self.add("ABCDE")
        self.open()
        return self.preview(5)

    def test_wrong_answers_wait_until_every_word_was_asked(self):
        shown = self.start_group()
        wrong = shown[1], shown[3]
        self.assertEqual(self.answers(wrong, 7), shown + list(wrong))

    def test_idk_keeps_the_same_order(self):
        shown = self.start_group()
        self.answer()
        self.assertEqual(self.current(), shown[1])
        self.press(I_DONT_KNOW_TEXT)
        self.answer()
        self.assertEqual(self.current(), shown[3])
        self.press(I_DONT_KNOW_TEXT)
        self.assertEqual(self.answers((), 3), [shown[4], shown[1], shown[3]])

    def test_wrong_answer_in_the_same_second_as_preview_end(self):
        self.tick = 0
        shown = self.start_group()
        self.answer(False)
        self.assertEqual(self.answers((), 4), shown[1:])
        self.assertEqual(self.current(), shown[0])

    def test_order_with_mix_mode_on_is_the_same(self):
        with _connect(self.db) as conn:
            conn.execute("UPDATE words SET topic='2' WHERE russian IN ('ruA','ruC','ruE')")
        _set_learning_mix_mode(UID, True)
        asyncio.run(L._learning_add_words(self.update("add"), self.ctx))
        self.open()
        shown = self.preview(len(LETTERS))
        self.assertEqual(self.answers((shown[1],), len(LETTERS) + 1), shown + [shown[1]])

    def test_equal_deadlines_in_an_active_queue_keep_the_wrong_word_behind(self):
        self.add("ABCD")
        with _connect(self.db) as conn:
            conn.execute("""UPDATE learning_items SET preview_done=1,first_due_pending=0,
                step_index=0,next_due='2020-01-01T00:00:00Z' WHERE user_id=?""", (UID,))
        self.open()
        first = self.answer(False)
        others = self.answers((), 3)
        self.assertNotIn(first, others)
        self.assertEqual(self.current(), first)
        self.assertEqual({self.item(letter)["step_index"] for letter in others}, {1})
        self.assertEqual(self.item(first)["step_index"], 0)

    def test_preview_ended_by_skip_also_keeps_a_same_second_error_behind(self):
        self.tick = 0
        self.add("ABCD")
        self.open()
        shown = self.preview(3)
        self.press(t("en", "learning_skip"))
        self.answer(False)
        self.assertEqual(self.answers((), 2), shown[1:])
        self.assertEqual(self.current(), shown[0])

    def test_later_rounds_are_really_random(self):
        self.start_group()
        self.answers((), 5)
        with _connect(self.db) as conn:
            picks = {store._choose(conn, UID, [], _Clock.current)[1]["word_id"] for _ in range(60)}
        self.assertGreater(len(picks), 1)

    def test_words_added_before_preview_ends_join_the_same_round(self):
        self.add("ABC")
        with _connect(self.db) as conn:
            conn.execute("UPDATE learning_items SET created_at='2026-01-01 00:00:00' WHERE user_id=?", (UID,))
        self.open()
        shown = self.preview(1)
        self.press(t("en", "back"))
        self.add("DE")
        self.open()
        shown += self.preview(4)
        self.answer(False)
        self.assertEqual(self.answers((), 4), shown[1:])
        self.assertEqual(self.current(), shown[0])

    def test_exit_restart_and_next_day_continue_from_the_same_place(self):
        shown = self.start_group()
        self.answers((shown[1],), 3)
        before = self.rows()["items"]
        self.press(t("en", "back"))
        _Clock.current = _Clock.current + timedelta(days=1)
        self.ctx = self.new_context()
        self.open()
        self.assertEqual(self.current(), shown[3])
        self.assertEqual(self.rows()["items"], before)
        self.assertEqual(self.answers((), 3), [shown[3], shown[4], shown[1]])

    def test_input_err_does_not_return_the_word_to_the_round(self):
        shown = self.start_group()
        self.answer()
        self.answer(False)
        self.assertEqual(self.current(), shown[2])
        self.press(t("en", "input_err"))
        self.assertEqual(self.current(), shown[2])
        self.assertEqual(self.item(shown[1])["step_index"], 1)
        self.assertEqual(self.answers((), 3), shown[2:])

    def test_undo_of_correct_and_wrong_answer_asks_the_word_again_in_place(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                learning_reset_session(self.db, UID)
                store.close_display(self.db, UID)
                self.ctx = self.new_context()
                shown = self.start_group()
                self.answer()
                self.answer(ok)
                status = store.undo_learning_log(self.db, UID, self.last_log())["status"]
                self.assertEqual(status, "restored")
                self.open()
                self.assertEqual(self.answers((), 4), shown[1:])

    def test_skip_continues_with_the_next_word(self):
        shown = self.start_group()
        self.answer()
        self.answer()
        self.press(t("en", "learning_skip"))
        self.assertEqual(self.current(), shown[3])
        self.assertEqual(self.item(shown[2])["status"], "skipped")

    def test_single_word_is_asked_again_right_after_an_error(self):
        self.add("A")
        self.open()
        self.preview(1)
        self.answer(False)
        self.assertEqual(self.current(), "A")

    def test_error_after_the_round_returns_ahead_of_others_but_not_in_a_row(self):
        self.start_group()
        self.answers((), 5)
        letter = self.answer(False)
        self.assertNotEqual(self.current(), letter)
        self.answer()
        self.assertEqual(self.current(), letter)

    def test_later_steps_wait_and_an_older_error_goes_first_in_an_active_queue(self):
        self.add("ABF")
        old = "2020-01-01T00:00:00Z"
        self.set_item("F", preview_done=1, first_due_pending=0, step_index=3,
                      recovery_remaining=2, next_due=old)
        self.set_item("A", preview_done=1, first_due_pending=0, step_index=0, next_due=old)
        self.set_item("B", preview_done=1, first_due_pending=0, step_index=1, next_due=old)
        self.add("CDE")
        self.open()
        shown = self.preview(3)
        self.assertEqual(self.answers((), 4), ["A"] + shown)
        self.assertEqual(self.item("B")["next_due"], old)
        self.assertEqual(self.item("B")["step_index"], 1)
        self.assertEqual((self.item("F")["step_index"], self.item("F")["recovery_remaining"],
                          self.item("F")["next_due"]), (3, 2, old))

    def test_first_deadline_precedes_the_preview_end_by_one_second(self):
        self.add("AB")
        self.set_item("B", preview_done=1, first_due_pending=0, step_index=4,
                      next_due="2999-01-01T00:00:00Z")
        self.open()
        self.preview(1)
        self.assertEqual(self.item("A")["next_due"], stamp(_Clock.current - timedelta(seconds=1)))
        self.assertEqual(self.item("A")["updated_at"], stamp(_Clock.current))
        self.assertEqual(self.item("B")["next_due"], "2999-01-01T00:00:00Z")


class ClosedAddTests(_Base):
    TEXTS = {
        "en": "All words in the queue have passed preview. Learn them, or reset the queue to add new ones.",
        "ru": "Все слова очереди уже прошли просмотр. Учи их или сбрось очередь, чтобы добавить новые.",
        "uk": "Усі слова черги вже пройшли перегляд. Вчи їх або скинь чергу, щоб додати нові.",
    }

    def press_add(self):
        update = self.update("add")
        state = asyncio.run(L._learning_add_words(update, self.ctx))
        return state, update.message.replies

    def all_previewed(self, letters="ABC"):
        self.add(letters)
        with _connect(self.db) as conn:
            conn.execute("""UPDATE learning_items SET preview_done=1,first_due_pending=0
                WHERE user_id=?""", (UID,))

    def assert_added(self):
        before = len(self.rows()["items"])
        state, replies = self.press_add()
        self.assertEqual(state, LEARNING_MENU)
        self.assertGreater(len(self.rows()["items"]), before)
        self.assertNotEqual(replies[-1][0], t("en", "learning_add_closed"))

    def test_add_is_closed_when_every_active_word_passed_preview(self):
        self.all_previewed()
        for lang in ("en", "ru", "uk"):
            with self.subTest(lang=lang):
                self.set_lang(lang)
                before = self.rows()
                state, replies = self.press_add()
                self.assertEqual(state, LEARNING_MENU)
                self.assertEqual(len(replies), 1)
                self.assertEqual(replies[0][0], self.TEXTS[lang])
                self.assertEqual(replies[0][1]["reply_markup"].rows, _learning_menu_keys(UID))
                self.assertEqual(self.rows(), before)

    def test_closed_add_does_not_create_a_batch(self):
        self.all_previewed()
        with patch.object(L, "learning_get_or_create_batch") as batch:
            self.press_add()
        batch.assert_not_called()

    def test_add_is_open_with_an_unseen_word(self):
        self.all_previewed("AB")
        self.set_item("B", preview_done=0, first_due_pending=1)
        self.assert_added()

    def test_unseen_word_in_an_older_batch_keeps_add_open(self):
        self.all_previewed("AB")
        with _connect(self.db) as conn:
            conn.execute("""INSERT INTO learning_batches(id,user_id,target_qty,status,created_at,updated_at)
                VALUES('older',?,20,'active','2020-01-01 00:00:00','2020-01-01 00:00:00')""", (UID,))
        self.set_item("B", batch_id="older", preview_done=0, first_due_pending=1)
        self.assert_added()

    def test_unseen_word_of_another_user_does_not_open_add(self):
        self.all_previewed("AB")
        register_or_update_user(self.db, UID + 1, "o", "O")
        batch = learning_get_or_create_batch(self.db, UID + 1, 20)
        learning_add_words(self.db, user_id=UID + 1, batch_id=batch, first_delay_seconds=0,
                           words=[self.words["C"]])
        self.assertEqual(self.press_add()[1][0][0], self.TEXTS["en"])

    def test_add_is_open_without_any_learning_word(self):
        self.assert_added()

    def test_add_is_open_when_all_words_are_completed(self):
        self.all_previewed()
        with _connect(self.db) as conn:
            conn.execute("UPDATE learning_items SET status='completed' WHERE user_id=?", (UID,))
        self.assert_added()

    def test_add_is_open_after_reset(self):
        self.all_previewed()
        self.assertEqual(self.press_add()[1][0][0], self.TEXTS["en"])
        reset = self.update(t("en", "learning_reset"))
        self.assertEqual(asyncio.run(L.learning_menu_choice(reset, self.ctx)), LEARNING_MENU)
        self.assert_added()

    def test_closed_text_wins_over_a_full_batch(self):
        words = [self.word(f"Z{i}") for i in range(20)]
        batch = learning_get_or_create_batch(self.db, UID, 20)
        learning_add_words(self.db, user_id=UID, batch_id=batch, first_delay_seconds=0, words=words)
        state, replies = self.press_add()
        self.assertIn("20/20", replies[0][0])
        with _connect(self.db) as conn:
            conn.execute("UPDATE learning_items SET preview_done=1,first_due_pending=0 WHERE user_id=?", (UID,))
        state, replies = self.press_add()
        self.assertEqual(replies[0][0], t("en", "learning_add_closed"))
        self.assertEqual(state, LEARNING_MENU)


if __name__ == "__main__":
    unittest.main()
