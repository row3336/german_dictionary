"""Mini App M8 — the app shell route and its static assets (miniapp.md §3.2, §6.2).

`/app` and `/static/*` sit outside the access gate on purpose: a user with no
access at all still has to load the shell so it can show them the "no access"
screen (§6.2). This pins down that both are reachable with no Authorization header
at all, and that a broken or missing static file is not silently served as 200.

The second half guards the two contracts the frontend has with this package and
cannot check for itself, because vanilla JS is not importable from here:

  * **every string it renders comes from /me** (§7.1) — so a `t("…")` naming a key
    the table does not hold would print the key itself to the user;
  * **the wire words it branches on are the server's** — `answer_kind`, the phase,
    the empty and fill reasons. Renaming one of those constants in Python while
    the client still tests for the old spelling is a silent break, and a substring
    check here is what a typed client would have got from the compiler.
"""

import pathlib
import re
import unittest

from aiohttp.test_utils import AioHTTPTestCase

from bot.common.learning_core import FILL_BATCH_FULL, FILL_NO_NEW
from bot.common.word_filter import MODE_CEFR
from bot.webapp.i18n_strings import APP_TEXTS, app_strings
from bot.webapp.learning_flow import PHASE_PREVIEW
from bot.webapp.outcomes import EMPTY_NO_WORDS
from bot.webapp.overview import MODES
from bot.webapp.serializers import ANSWER_ARTICLE_CHOICE, ANSWER_NONE, ANSWER_TEXT
from bot.webapp.server import create_app
from bot.webapp.topics_api import EMPTY_TOPICS, UNKNOWN_TOPICS

TOKEN = "123456:AAH-token"

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot" / "webapp" / "static"
SCRIPTS = ("app.js", "quiz.js", "topics.js")
# The screen files that take their helpers from window.App instead of importing.
SATELLITES = ("quiz.js", "topics.js")

# `t(...)` as the frontend writes it, including the branching forms — a ternary or
# an `|| "fallback"` inside the call still names keys that have to exist.
T_CALL = re.compile(r"(?<![\w.])t\(([^()]*)\)")
STRING_LITERAL = re.compile(r'"([^"]*)"')


def script_text(name):
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def string_keys_used(source):
    """Every literal key the frontend passes to `t()`."""
    keys = set()
    for call in T_CALL.findall(source):
        keys.update(STRING_LITERAL.findall(call))
    return keys


class StaticAppTest(AioHTTPTestCase):
    async def get_application(self):
        return create_app(TOKEN)

    async def test_app_shell_serves_the_index_page_without_auth(self):
        resp = await self.client.get("/app")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        body = await resp.text()
        self.assertIn('id="app"', body)
        self.assertIn("/static/app.js", body)
        self.assertIn("/static/app.css", body)

    async def test_stylesheet_is_served_without_auth(self):
        resp = await self.client.get("/static/app.css")
        self.assertEqual(resp.status, 200)
        self.assertIn("css", resp.headers.get("Content-Type", ""))

    async def test_script_is_served_without_auth(self):
        resp = await self.client.get("/static/app.js")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertIn("Telegram", body)

    async def test_quiz_script_is_served_and_loaded_by_the_shell(self):
        # M8 part 2: the quiz screens are a second file (miniapp.md §8.1), and the
        # shell has to pull it in or every mode tile leads nowhere.
        page = await (await self.client.get("/app")).text()
        self.assertIn("/static/quiz.js", page)
        resp = await self.client.get("/static/quiz.js")
        self.assertEqual(resp.status, 200)

    async def test_topics_script_is_served_and_loaded_by_the_shell(self):
        # §7.6 part 2: same contract for the topic screen - the chip on the main
        # screen calls into it, so an unloaded file is a dead tap.
        page = await (await self.client.get("/app")).text()
        self.assertIn("/static/topics.js", page)
        resp = await self.client.get("/static/topics.js")
        self.assertEqual(resp.status, 200)

    async def test_directory_listing_is_not_exposed(self):
        # §9.2 п.7: no directory listing on prod. show_index=False in static_app.py.
        resp = await self.client.get("/static/")
        self.assertNotEqual(resp.status, 200)

    async def test_unknown_static_file_is_a_plain_404(self):
        resp = await self.client.get("/static/does-not-exist.js")
        self.assertEqual(resp.status, 404)


class FrontendStringsTest(unittest.TestCase):
    """Every key the frontend renders exists in the table /me sends (§7.1)."""

    def test_every_key_the_frontend_asks_for_is_translated(self):
        table = app_strings("en")
        for name in SCRIPTS:
            used = string_keys_used(script_text(name))
            self.assertTrue(used, f"{name}: no t() calls found - has the regex gone stale?")
            for key in sorted(used):
                with self.subTest(script=name, key=key):
                    self.assertIn(key, table)

    def test_all_three_languages_carry_the_same_keys(self):
        # ru/uk fall back to en key by key, so a missing key is not an error - a
        # key present ONLY in ru or uk is, because it can never reach a fallback.
        for lang in ("ru", "uk"):
            with self.subTest(lang=lang):
                self.assertEqual(set(APP_TEXTS[lang]) - set(APP_TEXTS["en"]), set())

    def test_mode_tiles_cover_every_mode_the_server_offers(self):
        source = script_text("app.js")
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertIn(f'"{mode}"', source)
                self.assertIn(f"mode_{mode}" if mode != "learning" else "mode_learning", source)


class FrontendModuleContractTest(unittest.TestCase):
    """The satellite screens reach into app.js only through what app.js publishes.

    The files share a `window.App` object instead of a module system (§8.1: no
    build step), so nothing checks the seam at load time — a helper renamed on one
    side of it fails only when the user opens the screen that needs it.
    """

    def exported_names(self):
        source = script_text("app.js")
        block = re.search(r"window\.App = \{(.*?)\n  \};", source, re.S)
        self.assertIsNotNone(block, "app.js no longer exports a window.App literal")
        return set(re.findall(r"^\s*([a-zA-Z_]+)[,:]", block.group(1), re.M))

    def test_every_shared_helper_a_satellite_uses_is_exported(self):
        exported = self.exported_names()
        for name in SATELLITES:
            source = script_text(name)
            used = set(re.findall(r"App\.([a-zA-Z_]+)", source))
            destructured = re.search(r"const \{([^}]*)\} = App;", source)
            self.assertIsNotNone(destructured, f"{name} no longer takes its helpers from App")
            used.update(part.strip() for part in destructured.group(1).split(",") if part.strip())
            for helper in sorted(used):
                with self.subTest(script=name, helper=helper):
                    self.assertIn(helper, exported)

    def test_the_shell_hands_every_mode_over_to_the_quiz_module(self):
        # The M8 part 1 placeholder ("this mode is done in the chat") is gone: with
        # M4-M6 shipped there is no mode the app opens but cannot run.
        source = script_text("app.js")
        self.assertNotIn("renderChatOnly", source)
        self.assertIn("window.App.quiz.start(mode)", source)

    def test_the_topics_chip_opens_the_topics_screen(self):
        # §7.6 is the one setting the app writes, and the chip is its only door.
        # Until part 2 the chip was a static label saying "changed in the chat".
        source = script_text("app.js")
        self.assertNotIn("topics_bot_only", source)
        self.assertIn("data-topics", source)
        self.assertIn("window.App.topics.open()", source)

    def test_applying_a_filter_refetches_the_filtered_numbers(self):
        # Every due count on the main screen is filtered by topic, so the tiles are
        # stale the moment the filter changes - patching only the chip would leave
        # the user looking at counts for the filter they just left.
        self.assertIn("App.refreshMe()", script_text("topics.js"))


class FrontendWireContractTest(unittest.TestCase):
    """The client branches on the server's own wire words, not on lookalikes."""

    def test_answer_kinds_are_the_serializer_s_own(self):
        source = script_text("quiz.js")
        for value in (ANSWER_TEXT, ANSWER_ARTICLE_CHOICE, ANSWER_NONE, PHASE_PREVIEW):
            with self.subTest(value=value):
                self.assertIn(f'"{value}"', source)

    def test_empty_and_fill_reasons_are_the_flow_s_own(self):
        source = script_text("quiz.js")
        self.assertIn(f'"{EMPTY_NO_WORDS}"', source)
        for reason in (FILL_BATCH_FULL, FILL_NO_NEW):
            with self.subTest(reason=reason):
                self.assertIn(reason, source)

    def test_the_topic_screen_uses_the_filter_s_own_words(self):
        # The refusals it recovers from and the branch name it warns about all come
        # from Python: a rename there has to fail here, not in front of a user.
        source = script_text("topics.js")
        for value in (MODE_CEFR, EMPTY_TOPICS, UNKNOWN_TOPICS):
            with self.subTest(value=value):
                self.assertIn(f'"{value}"', source)


if __name__ == "__main__":
    unittest.main()
