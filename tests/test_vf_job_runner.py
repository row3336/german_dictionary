"""spec #2 step 3c — VF-add job runner (fetch/sleep injected; no network/waits)."""

import asyncio
import os
import tempfile
import unittest

from bot.storage import init_db
from bot.verb_forms_parsing import job_store as JS
from bot.verb_forms_parsing.job_runner import run_job

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "verbformen_de")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


async def _noop_delay():
    return None


class JobRunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def _run(self, jid, **kw):
        kw.setdefault("delay", _noop_delay)
        return asyncio.run(run_job(self.db, jid, **kw))

    def test_completes_and_parses(self):
        jid = JS.create_job(self.db, 7, 100, "05 Essen", ["gehen", "Haus"])
        progress = []

        def fetcher(word, type_hint=""):
            conj = load("gehen_conj.html") if word == "gehen" else ""
            return load(f"{word}_summary.html"), conj

        async def on_progress(job, counts, done):
            progress.append((counts["done"], done))

        status = self._run(jid, fetcher=fetcher, on_progress=on_progress)
        self.assertEqual(status, "done")
        c = JS.job_counts(self.db, jid)
        self.assertEqual((c["done"], c["error"], c["pending"]), (2, 0, 0))
        items = JS.list_items(self.db, jid)
        by_word = {it["word"]: it for it in items}
        self.assertEqual(by_word["gehen"]["result"]["type"], "verb")
        self.assertTrue(by_word["gehen"]["result"]["conjugation_praesens"])
        self.assertEqual(by_word["Haus"]["result"]["type"], "nomen")
        self.assertEqual(progress[-1], (2, True))  # final progress marks done

    def test_ban_streak_emergency_stop(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["a", "b", "c", "d", "e"])

        def fetcher(word, type_hint=""):
            raise RuntimeError("boom")

        status = self._run(jid, fetcher=fetcher, ban_limit=3)
        self.assertEqual(status, "error")
        c = JS.job_counts(self.db, jid)
        self.assertEqual(c["error"], 3)
        self.assertEqual(c["pending"], 2)  # stopped early, remainder untouched

    def test_stopped_job_does_nothing(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen", "Haus"])
        JS.set_job_status(self.db, jid, "stopped")
        status = self._run(jid, fetcher=lambda w, h="": ("", ""))
        self.assertEqual(status, "stopped")
        self.assertEqual(JS.job_counts(self.db, jid)["pending"], 2)

    def test_stop_fires_progress_done(self):
        # a stopped job still notifies the UI (done=True) so the review file is sent
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        JS.set_job_status(self.db, jid, "stopped")
        seen = []

        async def on_progress(job, counts, done):
            seen.append(done)

        self._run(jid, fetcher=lambda w, h="": ("", ""), on_progress=on_progress)
        self.assertEqual(seen, [True])

    def test_stop_takes_effect_next_iteration(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen", "Haus"])

        def fetcher(word, type_hint=""):
            JS.set_job_status(self.db, jid, "stopped")  # stop requested during first word
            return load("gehen_summary.html"), ""

        status = self._run(jid, fetcher=fetcher)
        self.assertEqual(status, "stopped")
        c = JS.job_counts(self.db, jid)
        self.assertEqual((c["done"], c["pending"]), (1, 1))  # first done, second left pending

    def test_transient_proxy_error_retries_then_succeeds(self):
        # http=0 (proxy/transport) is transient -> retried, then parses on attempt 2
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        calls = {"n": 0}

        def fetcher(word, type_hint=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("summary fetch failed (http=0) for 'gehen'")
            return load("gehen_summary.html"), load("gehen_conj.html")

        drips = {"n": 0}

        async def delay():
            drips["n"] += 1

        status = self._run(jid, fetcher=fetcher, delay=delay)
        self.assertEqual(status, "done")
        c = JS.job_counts(self.db, jid)
        self.assertEqual((c["done"], c["error"]), (1, 0))
        self.assertEqual(calls["n"], 2)          # retried once
        self.assertGreaterEqual(drips["n"], 1)   # slow-drip waited before the retry

    def test_transient_retries_exhausted_marks_error(self):
        # a persistently failing transient error gives up after fetch_retries
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        calls = {"n": 0}

        def fetcher(word, type_hint=""):
            calls["n"] += 1
            raise RuntimeError("summary fetch failed (http=0) for 'gehen'")

        status = self._run(jid, fetcher=fetcher, fetch_retries=2)
        self.assertEqual(status, "done")            # job itself completes
        self.assertEqual(calls["n"], 3)             # 1 initial + 2 retries
        self.assertEqual(JS.job_counts(self.db, jid)["error"], 1)

    def test_404_is_not_retried(self):
        # a real 404 (word absent) must fail fast, no wasted retries
        jid = JS.create_job(self.db, 7, 100, "t", ["nixgibtsnicht"])
        calls = {"n": 0}

        def fetcher(word, type_hint=""):
            calls["n"] += 1
            raise RuntimeError("summary fetch failed (http=404) for 'nixgibtsnicht'")

        status = self._run(jid, fetcher=fetcher)
        self.assertEqual(status, "done")
        self.assertEqual(calls["n"], 1)             # no retry on 404
        self.assertEqual(JS.job_counts(self.db, jid)["error"], 1)


if __name__ == "__main__":
    unittest.main()
