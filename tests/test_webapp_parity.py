"""Mini App §5.4 — the app's verdict is the chat's verdict (miniapp.md §11).

This is the contract the whole reuse design exists for: the same word, answered the
same way, must be judged identically and must leave identical FSRS state behind. A
divergence here is the failure nobody would notice — no error, no crash, just an
interval that quietly differs depending on which client the user happened to open.

So both sides are driven for real: the chat through `bot/handlers/quiz.py` with a
fake update, the app through HTTP. Two users answer the same word in the same mode,
and afterwards `progress` and `quiz_events` are compared row for row. Nothing is
mocked between the answer and the database.
"""

import sqlite3
import unittest

from _telegram_stub import install

install()

from _webapp_helpers import TOKEN, TempDbMixin, auth_header  # noqa: E402
from aiohttp.test_utils import AioHTTPTestCase  # noqa: E402

import bot.handlers.quiz as Q  # noqa: E402
import bot.handlers.weak as W  # noqa: E402
from bot.app_config import I_DONT_KNOW_TEXT  # noqa: E402
from bot.storage import (  # noqa: E402
    add_synonym_link,
    add_word,
    apply_user_word_media,
    get_progress,
    log_quiz_event,
    update_form_stats,
)
from bot.webapp import quiz_flow  # noqa: E402
from bot.webapp.access import API_PREFIX  # noqa: E402
from bot.webapp.app_keys import REGISTRY_KEY  # noqa: E402

BOT_USER = 901
APP_USER = 902

NOUN = {"type": "nomen", "article": "der", "german": "Tisch", "plural": "Tische", "russian": "стол"}
NOUN_NO_PLURAL = {"type": "nomen", "article": "das", "german": "Wasser", "plural": "", "russian": "вода"}
VERB = {
    "type": "verb",
    "german": "gehen",
    "infinitive": "gehen",
    "prateritum": "ging",
    "partizip2": "gegangen",
    "russian": "идти",
}
ADJ = {
    "type": "adjektiv",
    "german": "schnell",
    "komparativ": "schneller",
    "superlativ": "am schnellsten",
    "russian": "быстрый",
}

# (label, direction, word, answer template). Each case gets its own word — a word
# already answered is no longer the same question — so the answers are written
# against the stored entry rather than spelled out. The expected verdict is
# deliberately absent: what is asserted is that the two clients agree, not what
# they agree on; that part is already pinned by the checker's own tests.
CASES = [
    ("noun with its plural", quiz_flow.L1_DE, NOUN, "{article} {german} {plural}"),
    ("noun, comma separated", quiz_flow.L1_DE, NOUN, "{article} {german}, {plural}"),
    ("noun, wrong article", quiz_flow.L1_DE, NOUN, "die {german} {plural}"),
    ("noun, plural missing", quiz_flow.L1_DE, NOUN, "{article} {german}"),
    ("noun that has no plural", quiz_flow.L1_DE, NOUN_NO_PLURAL, "{article} {german}"),
    ("verb, short form", quiz_flow.L1_DE, VERB, "{infinitive} {partizip2}"),
    ("verb, all three forms", quiz_flow.L1_DE, VERB, "{infinitive} {prateritum} {partizip2}"),
    ("verb, missing partizip", quiz_flow.L1_DE, VERB, "{infinitive} {prateritum}"),
    ("adjective, base only", quiz_flow.L1_DE, ADJ, "{german}"),
    ("adjective, all degrees", quiz_flow.L1_DE, ADJ, "{german} {komparativ} {superlativ}"),
    ("adjective, wrong degree", quiz_flow.L1_DE, ADJ, "{german} schnellster"),
    ("empty answer", quiz_flow.L1_DE, NOUN, ""),
    ("translation", quiz_flow.DE_L1, NOUN, "{russian}"),
    ("translation, wrong word", quiz_flow.DE_L1, NOUN, "стул"),
    ("singular with article", quiz_flow.PL_SG, NOUN, "{article} {german}"),
    ("singular, wrong article", quiz_flow.PL_SG, NOUN, "die {german}"),
]

HANDLERS = {
    quiz_flow.L1_DE: "quiz_ru_de",
    quiz_flow.DE_L1: "quiz_de_ru",
    quiz_flow.PL_SG: "quiz_pl_sg",
}


async def _silent_prompt(update, context, prefix_text=None):
    """Stand-in for the chat's next-question senders: they talk to Telegram.

    Only the outbound half is replaced. Everything that scores the answer runs
    exactly as it does in production — that is the whole point of the comparison.
    """
    return None


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.message_id = 1
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, user_id, text):
        self.message = _FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


class ParityTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(BOT_USER, "approved")
        self.make_user(APP_USER, "approved")
        from bot.webapp.server import create_app

        return create_app(TOKEN)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name in ("_send_ru_de_prompt", "_send_de_ru_prompt", "_send_pl_sg_prompt"):
            original = getattr(Q, name)
            self.addCleanup(setattr, Q, name, original)
            setattr(Q, name, _silent_prompt)

    # -- fixtures --------------------------------------------------------

    def make_due(self, fields, suffix=""):
        """One word, due right now for both users in every mode.

        The suffix makes each case its own word: the dictionary refuses duplicates,
        and a verb is a duplicate by its infinitive rather than by `german`.
        """
        infinitive = fields.get("infinitive", "")
        word = add_word(
            self.db,
            {
                "id": "unique",
                "article": "",
                "plural": "",
                "infinitive": "",
                "prateritum": "",
                "partizip2": "",
                "ukrainian": "",
                "english": "",
                "topic": "1",
                **fields,
                "german": f"{fields['german']}{suffix}",
                "russian": f"{fields['russian']}{suffix}",
                "infinitive": f"{infinitive}{suffix}" if infinitive else "",
            },
        )
        for direction in quiz_flow.FSRS_DIRECTIONS:
            mode = quiz_flow.mode_for(BOT_USER, direction)
            for user_id in (BOT_USER, APP_USER):
                log_quiz_event(self.db, user_id, word["id"], mode, False)
        return word

    # -- the two clients -------------------------------------------------

    async def answer_in_the_chat(self, direction, word, text):
        """Run the real chat handler over a fake update."""
        entry = apply_user_word_media(self.db, BOT_USER, word)
        update = _FakeUpdate(BOT_USER, text)
        await getattr(Q, HANDLERS[direction])(update, _Ctx({"quiz_entry": entry}))

    async def answer_in_the_app(self, direction, word, text, idk=False):
        """Open a session over HTTP and answer this word through the API."""
        resp = await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(APP_USER), json={"mode": direction}
        )
        token = (await resp.json())["question"]["token"]
        # Pin the word: the picker is free to hand out any due word, and what is
        # being compared here is the verdict, not the pick order.
        session = self.app[REGISTRY_KEY].require(APP_USER)
        session.ctx.user_data["quiz_entry"] = apply_user_word_media(self.db, APP_USER, word)
        route = "idk" if idk else "answer"
        resp = await self.client.post(
            f"{API_PREFIX}session/{route}",
            headers=auth_header(APP_USER),
            json={"question_token": token, "text": text},
        )
        body = await resp.json()
        await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(APP_USER))
        return body

    def assert_same_state(self, direction, word):
        """progress rows for the two users, field for field (§5.4)."""
        mode = quiz_flow.mode_for(BOT_USER, direction)
        self.assertEqual(
            get_progress(self.db, APP_USER, word["id"], mode),
            get_progress(self.db, BOT_USER, word["id"], mode),
        )

    # -- tests -----------------------------------------------------------

    async def test_the_verdict_and_the_srs_step_match_the_chat(self):
        for index, (label, direction, fields, template) in enumerate(CASES):
            with self.subTest(case=label, direction=direction):
                word = self.make_due(fields, suffix=str(index))
                answer = template.format(**word)
                body = await self.answer_in_the_app(direction, word, answer)
                await self.answer_in_the_chat(direction, word, answer)
                self.assert_same_state(direction, word)
                # The chat's verdict is the FSRS quality it wrote: a correct answer
                # advances repetitions, a wrong one does not.
                mode = quiz_flow.mode_for(BOT_USER, direction)
                advanced = get_progress(self.db, BOT_USER, word["id"], mode)["repetitions"] > 0
                self.assertEqual(body["correct"], advanced)

    async def test_a_synonym_answer_is_accepted_by_both(self):
        # The group is shared, so both clients resolve the same alternatives.
        table = self.make_due(NOUN, suffix="syn")
        desk = self.make_due(
            {**NOUN, "article": "das", "german": "Pult", "plural": "Pulte", "russian": "парта"},
            suffix="syn",
        )
        add_synonym_link(self.db, table["id"], desk["id"])
        answer = f"{desk['article']} {desk['german']} {desk['plural']}"

        body = await self.answer_in_the_app(quiz_flow.L1_DE, table, answer)
        await self.answer_in_the_chat(quiz_flow.L1_DE, table, answer)
        self.assert_same_state(quiz_flow.L1_DE, table)
        mode = quiz_flow.mode_for(BOT_USER, quiz_flow.L1_DE)
        self.assertEqual(
            body["correct"], get_progress(self.db, BOT_USER, table["id"], mode)["repetitions"] > 0
        )

    async def test_i_dont_know_is_the_same_wrong_answer(self):
        # The chat types its button's text; the app has a route. Same outcome.
        word = self.make_due(NOUN, suffix="idk")
        body = await self.answer_in_the_app(quiz_flow.L1_DE, word, "", idk=True)
        await self.answer_in_the_chat(quiz_flow.L1_DE, word, I_DONT_KNOW_TEXT)
        self.assertFalse(body["correct"])
        self.assert_same_state(quiz_flow.L1_DE, word)

    async def test_a_ukrainian_dictionary_scores_into_the_same_mode(self):
        # The mode string depends on the dictionary language; picking a different
        # one would score against a history the other client never sees.
        from bot.storage import set_user_dict_lang

        set_user_dict_lang(self.db, BOT_USER, "uk")
        set_user_dict_lang(self.db, APP_USER, "uk")
        word = self.make_due({**NOUN, "ukrainian": "стіл"}, suffix="uk")
        body = await self.answer_in_the_app(quiz_flow.DE_L1, word, "стіл")
        await self.answer_in_the_chat(quiz_flow.DE_L1, word, "стіл")
        self.assertTrue(body["correct"])
        self.assertEqual(quiz_flow.mode_for(APP_USER, quiz_flow.DE_L1), "de_uk")
        self.assert_same_state(quiz_flow.DE_L1, word)


# The same contract for the four weak modes (M5). It needs its own class because
# weak keeps no `progress` row: what has to match is the `form_stats` row — the
# error counters, the streaks, the active flag and the review ladder — plus the
# event that was logged for it.
WEAK_CASES = [
    ("article, right", "article", NOUN, "{article}"),
    ("article, wrong", "article", NOUN, "die"),
    ("plural, right", "plural", NOUN, "{plural}"),
    ("plural, with article", "plural", NOUN, "die {plural}"),
    ("plural, wrong", "plural", NOUN, "Tischen"),
    ("präteritum, right", "prateritum", VERB, "{prateritum}"),
    ("präteritum, wrong", "prateritum", VERB, "gehte"),
    ("partizip II, right", "partizip2", VERB, "{partizip2}"),
    ("partizip II, wrong", "partizip2", VERB, "gegeht"),
]

WEAK_HANDLERS = {
    "article": "quiz_article_weak",
    "plural": "quiz_plural_weak",
    "prateritum": "quiz_prateritum_weak",
    "partizip2": "quiz_partizip2_weak",
}

# Two wrong answers is what flags a form as weak; both users start from there so
# the answer under test is the first one either of them gives in the app/chat.
ERRORS_TO_WEAK = 2


class WeakParityTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(BOT_USER, "approved")
        self.make_user(APP_USER, "approved")
        from bot.webapp.server import create_app

        return create_app(TOKEN)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name in (
            "_send_article_weak_prompt",
            "_send_plural_weak_prompt",
            "_send_prateritum_weak_prompt",
            "_send_partizip2_weak_prompt",
        ):
            original = getattr(W, name)
            self.addCleanup(setattr, W, name, original)
            setattr(W, name, _silent_prompt)

    # -- fixtures --------------------------------------------------------

    def make_weak(self, field, fields, suffix=""):
        """One word, flagged weak in `field` for both users."""
        infinitive = fields.get("infinitive", "")
        word = add_word(
            self.db,
            {
                "id": "unique",
                "article": "",
                "plural": "",
                "infinitive": "",
                "prateritum": "",
                "partizip2": "",
                "ukrainian": "",
                "english": "",
                "topic": "1",
                **fields,
                "german": f"{fields['german']}{suffix}",
                "russian": f"{fields['russian']}{suffix}",
                "infinitive": f"{infinitive}{suffix}" if infinitive else "",
            },
        )
        for user_id in (BOT_USER, APP_USER):
            for _ in range(ERRORS_TO_WEAK):
                update_form_stats(self.db, user_id, word["id"], {field: False})
        return word

    # -- the two clients -------------------------------------------------

    async def answer_in_the_chat(self, field, word, text):
        """Run the real chat handler over a fake update; hand back its session.

        The context is returned rather than thrown away because input_err reads it:
        what may be rolled back is whatever this answer stored in it.
        """
        entry = apply_user_word_media(self.db, BOT_USER, word)
        update = _FakeUpdate(BOT_USER, text)
        context = _Ctx({"quiz_entry": entry})
        await getattr(W, WEAK_HANDLERS[field])(update, context)
        return update, context

    async def answer_in_the_app(self, field, word, text, idk=False, stop=True):
        """Open a session over HTTP and answer this word through the API."""
        mode = f"weak_{field}"
        resp = await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(APP_USER), json={"mode": mode}
        )
        token = (await resp.json())["question"]["token"]
        # Pin the word: the picker is free to hand out any weak word, and what is
        # being compared here is the verdict, not the pick order.
        session = self.app[REGISTRY_KEY].require(APP_USER)
        session.ctx.user_data["quiz_entry"] = apply_user_word_media(self.db, APP_USER, word)
        route = "idk" if idk else "answer"
        resp = await self.client.post(
            f"{API_PREFIX}session/{route}",
            headers=auth_header(APP_USER),
            json={"question_token": token, "text": text},
        )
        body = await resp.json()
        if stop:
            await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(APP_USER))
        return body

    # -- comparison ------------------------------------------------------

    def form_row(self, user_id, word_id):
        """The whole form_stats row, minus the keys that name whose it is."""
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM form_stats WHERE user_id = ? AND word_id = ?",
                (user_id, word_id),
            ).fetchone()
        return {k: v for k, v in dict(row).items() if k not in ("user_id", "word_id")}

    def logged_verdicts(self, user_id, word_id, mode):
        return [
            bool(row[0])
            for row in sqlite3.connect(self.db).execute(
                "SELECT is_correct FROM quiz_events "
                "WHERE user_id = ? AND word_id = ? AND mode = ? ORDER BY id",
                (user_id, word_id, mode),
            )
        ]

    def assert_same_state(self, field, word):
        """form_stats and the logged verdicts, field for field (§5.4)."""
        mode = f"weak_{field}"
        self.assertEqual(
            self.form_row(APP_USER, word["id"]), self.form_row(BOT_USER, word["id"])
        )
        self.assertEqual(
            self.logged_verdicts(APP_USER, word["id"], mode),
            self.logged_verdicts(BOT_USER, word["id"], mode),
        )

    # -- tests -----------------------------------------------------------

    async def test_the_verdict_and_the_weak_state_match_the_chat(self):
        for index, (label, field, fields, template) in enumerate(WEAK_CASES):
            with self.subTest(case=label, field=field):
                word = self.make_weak(field, fields, suffix=str(index))
                answer = template.format(**word)
                body = await self.answer_in_the_app(field, word, answer)
                await self.answer_in_the_chat(field, word, answer)
                self.assert_same_state(field, word)
                # The chat's verdict is the event it logged for this answer.
                self.assertEqual(
                    body["correct"],
                    self.logged_verdicts(BOT_USER, word["id"], f"weak_{field}")[0],
                )

    async def test_i_dont_know_is_the_same_wrong_answer(self):
        # The chat types its button's text; the app has a route. Same outcome.
        word = self.make_weak("plural", NOUN, suffix="idk")
        body = await self.answer_in_the_app("plural", word, "", idk=True)
        await self.answer_in_the_chat("plural", word, I_DONT_KNOW_TEXT)
        self.assertFalse(body["correct"])
        self.assert_same_state("plural", word)

    async def test_taking_back_a_typo_lands_in_the_same_state(self):
        # input_err is the one step that reaches backwards, so a divergence here
        # would quietly undo different things in the two clients. Both sessions stay
        # open across the two calls, because the rollback reads the session the
        # answer was given in.
        word = self.make_weak("prateritum", VERB, suffix="err")

        await self.answer_in_the_app("prateritum", word, "gnig", stop=False)
        applied = await (
            await self.client.post(f"{API_PREFIX}session/input_err", headers=auth_header(APP_USER))
        ).json()
        await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(APP_USER))

        update, context = await self.answer_in_the_chat("prateritum", word, "gnig")
        self.assertTrue(applied["applied"])
        self.assertTrue(W._apply_input_err_weak(update, context, "prateritum"))
        self.assert_same_state("prateritum", word)


if __name__ == "__main__":
    unittest.main()
