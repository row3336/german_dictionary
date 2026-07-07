"""Conjugation active-recall extras: 🎲 dice + inline "hide forms" toggle.

Covers the pure logic (dice pick, masked block render) and the two handlers'
edit-in-place behaviour (dice re-press edits the same message; the hide inline
toggle flips the card and its button label in place), plus button routing for
ru/uk so no raw English label leaks.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import CONJUGATION_MENU  # noqa: E402
from bot.common.conjugation import (  # noqa: E402
    DICE_PRONOUNS,
    format_conjugation_block,
    pick_dice_pronoun,
)
from bot.common.navigation import _action_key_from_text  # noqa: E402
from bot.handlers.conjugation import (  # noqa: E402
    conjugation_card_callback,
    conjugation_input,
    start_conjugation,
)
from bot.i18n import t  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    register_or_update_user,
    set_user_dict_lang,
    set_user_interface_lang,
    set_word_conjugation,
)

OWNER, GUEST = 999, 111
GOOD = "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen"


def _run(coro):
    return asyncio.run(coro)


def _kb_texts(markup):
    rows = (
        getattr(markup, "keyboard", None)
        or getattr(markup, "rows", None)
        or getattr(markup, "inline_keyboard", None)
        or []
    )
    return [getattr(b, "text", b) for row in rows for b in row]


class _SentMessage:
    """What reply_text returns — carries a message_id so edit-in-place can target it."""

    def __init__(self, mid, text):
        self.message_id = mid
        self.text = text


class _Msg:
    _next_id = 1

    def __init__(self, text):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))
        mid = _Msg._next_id
        _Msg._next_id += 1
        return _SentMessage(mid, text)


class _Bot:
    """Records edit_message_text (dice edit-in-place) and edit_message_reply_markup
    (stripping a worked-through card's inline button) calls."""

    def __init__(self):
        self.edits = []  # list of (message_id, text)
        self.markup_edits = []  # list of (message_id, reply_markup)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self.edits.append((message_id, text))

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kw):
        self.markup_edits.append((message_id, reply_markup))


class _Update:
    def __init__(self, text, uid):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}
        self.bot = _Bot()


class _CardMsg:
    """The card message a callback edits in place (query.edit_message_text)."""

    def __init__(self, mid):
        self.message_id = mid
        self.edits = []  # list of (text, reply_markup)


class _Query:
    def __init__(self, data, uid, mid):
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.message = _CardMsg(mid)
        self.answered = 0

    async def answer(self, *a, **k):
        self.answered += 1

    async def edit_message_text(self, text, **kw):
        self.message.edits.append((text, kw.get("reply_markup")))


class _CbUpdate:
    def __init__(self, data, uid, mid):
        self.callback_query = _Query(data, uid, mid)


class _Base(unittest.TestCase):
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
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_interface_lang(self.db, GUEST, "ru")
        set_user_dict_lang(self.db, GUEST, "ru")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _add_eligible_verb(self, wid, infinitive, russian="идти", reps=2, mode="ru_de"):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO words (id, type, german, infinitive, russian, topic) "
            "VALUES (?, 'verb', ?, ?, ?, '1')",
            (wid, infinitive, infinitive, russian),
        )
        conn.execute(
            "INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due) "
            "VALUES (?, ?, ?, 3, ?, 2.5, '2020-01-01')",
            (GUEST, wid, mode, reps),
        )
        conn.commit()
        conn.close()
        set_word_conjugation(self.db, wid, GOOD)


class DicePickTest(unittest.TestCase):
    def test_pick_returns_one_of_the_six(self):
        for _ in range(200):
            self.assertIn(pick_dice_pronoun(None), DICE_PRONOUNS)

    def test_pick_avoids_immediate_repeat(self):
        for prev in DICE_PRONOUNS:
            for _ in range(50):
                self.assertNotEqual(pick_dice_pronoun(prev), prev)

    def test_dice_pronoun_set(self):
        self.assertEqual(
            DICE_PRONOUNS, ("ich", "du", "er/sie/es", "wir", "ihr", "Sie/sie")
        )


class MaskRenderTest(unittest.TestCase):
    def test_mask_hides_every_form_keeps_pronouns_and_lines(self):
        plain = format_conjugation_block(GOOD)
        masked = format_conjugation_block(GOOD, mask=True)
        p_lines, m_lines = plain.splitlines(), masked.splitlines()
        self.assertEqual(len(m_lines), 6)
        self.assertEqual(len(m_lines), len(p_lines))
        for pronoun, pl, ml in zip(
            ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie"), p_lines, m_lines
        ):
            self.assertTrue(ml.startswith(pronoun))  # pronoun column intact
            self.assertIn("***", ml)
            self.assertNotIn("gehe", ml)
            # alignment preserved: form starts in the same column as the plain render
            self.assertEqual(ml.index("***"), pl.index(pl.split()[1]))
        # no leftover German form leaks
        self.assertNotIn("gehst", masked)


class DiceHandlerTest(_Base):
    def test_second_press_edits_same_message(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))

        dice = "🎲"
        u1 = _Update(dice, GUEST)
        state = _run(conjugation_input(u1, ctx))
        self.assertEqual(state, CONJUGATION_MENU)
        # first press SENDS a new message, no edits yet
        self.assertEqual(len(u1.message.replies), 1)
        self.assertEqual(ctx.bot.edits, [])
        first_mid = ctx.user_data["conjugation_dice_msg_id"]
        self.assertIsNotNone(first_mid)

        u2 = _Update(dice, GUEST)
        _run(conjugation_input(u2, ctx))
        # second press EDITS the stored message and does NOT send a new one
        self.assertEqual(u2.message.replies, [])
        self.assertEqual(len(ctx.bot.edits), 1)
        edited_mid, _ = ctx.bot.edits[0]
        self.assertEqual(edited_mid, first_mid)
        # stored id unchanged (still the same live message)
        self.assertEqual(ctx.user_data["conjugation_dice_msg_id"], first_mid)

    def test_dice_message_shows_a_pronoun(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))
        u = _Update("🎲", GUEST)
        _run(conjugation_input(u, ctx))
        shown = u.message.replies[0]
        self.assertTrue(any(p in shown for p in DICE_PRONOUNS))

    def test_next_card_resets_dice(self):
        self._add_eligible_verb("w1", "gehen")
        self._add_eligible_verb("w2", "sein")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))
        _run(conjugation_input(_Update("🎲", GUEST), ctx))
        self.assertIsNotNone(ctx.user_data.get("conjugation_dice_msg_id"))
        _run(conjugation_input(_Update("далее", GUEST), ctx))
        # advancing forgets the live dice message so the next roll starts fresh
        self.assertIsNone(ctx.user_data.get("conjugation_dice_msg_id"))


class HideToggleTest(_Base):
    def _start_and_card_mid(self, ctx):
        upd = _Update("", GUEST)
        _run(start_conjugation(upd, ctx))
        entries = ctx.user_data["conjugation_card_entries"]
        return next(iter(entries.keys()))

    def test_hide_masks_card_and_flips_label_to_show(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        mid = self._start_and_card_mid(ctx)

        cb = _CbUpdate("conj:hide", GUEST, mid)
        _run(conjugation_card_callback(cb, ctx))
        text, markup = cb.callback_query.message.edits[-1]
        self.assertIn("***", text)
        self.assertNotIn("gehe\n", text)
        # button now offers "show" (показать), not "hide"
        self.assertEqual(_kb_texts(markup), [t("ru", "conjugation_show")])
        self.assertEqual(_kb_texts(markup), ["показать"])

    def test_show_reveals_card_and_flips_label_to_hide(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        mid = self._start_and_card_mid(ctx)

        cb = _CbUpdate("conj:show", GUEST, mid)
        _run(conjugation_card_callback(cb, ctx))
        text, markup = cb.callback_query.message.edits[-1]
        self.assertNotIn("***", text)
        self.assertIn("gehe", text)
        self.assertEqual(_kb_texts(markup), [t("ru", "conjugation_hide")])
        self.assertEqual(_kb_texts(markup), ["скрыть"])

    def test_stale_card_tap_is_noop(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        self._start_and_card_mid(ctx)
        cb = _CbUpdate("conj:hide", GUEST, 999999)  # unknown message id
        _run(conjugation_card_callback(cb, ctx))
        self.assertEqual(cb.callback_query.message.edits, [])
        self.assertEqual(cb.callback_query.answered, 1)

    def test_next_strips_previous_card_button(self):
        # owner #4: advancing must REMOVE the inline button from the worked-through
        # card, not just make it a no-op — only the current card stays interactive.
        self._add_eligible_verb("w1", "gehen")
        self._add_eligible_verb("w2", "sein")
        ctx = _Ctx()
        first_mid = self._start_and_card_mid(ctx)
        _run(conjugation_input(_Update("далее", GUEST), ctx))
        self.assertIn((first_mid, None), ctx.bot.markup_edits)  # button stripped
        entries = ctx.user_data["conjugation_card_entries"]
        self.assertNotIn(first_mid, entries)  # old card no longer toggle-able

    def test_stop_strips_current_card_button(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        mid = self._start_and_card_mid(ctx)
        _run(conjugation_input(_Update("стоп", GUEST), ctx))
        self.assertIn((mid, None), ctx.bot.markup_edits)


class RoutingTest(_Base):
    def test_dice_button_routes_for_all_langs(self):
        for lang in ("ru", "uk", "en"):
            set_user_interface_lang(self.db, GUEST, lang)
            label = t(lang, "conjugation_dice")
            self.assertEqual(label, "🎲")
            self.assertEqual(_action_key_from_text(label, GUEST), "conjugation_dice")

    def test_hide_show_labels_localized_no_raw_english(self):
        for lang, hide, show in (("ru", "скрыть", "показать"), ("uk", "сховати", "показати")):
            self.assertEqual(t(lang, "conjugation_hide"), hide)
            self.assertEqual(t(lang, "conjugation_show"), show)
            self.assertNotEqual(t(lang, "conjugation_hide"), t("en", "conjugation_hide"))
            self.assertNotEqual(t(lang, "conjugation_show"), t("en", "conjugation_show"))


if __name__ == "__main__":
    unittest.main()
