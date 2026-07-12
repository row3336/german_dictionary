"""Off-site DB backup (spec #4, phase 1) — snapshot integrity, caption, and the
send pipeline (success / retry+alert / fatal), driven with a fake HTTP client.

Token hygiene is asserted explicitly: a failing Bot API call must never surface
the token-bearing URL in the raised error or in the alert text.
"""

import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from bot.offsite_backup import backup as B
from bot.storage import add_word, init_db

TOKEN = "123456789:AA-SECRET-TOKEN-VALUE-do-not-leak-0000"


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True, "result": {"message_id": 1}}

    def json(self):
        return self._payload


class FakeClient:
    """Records every POST. `handler(method, n_calls_for_method)` returns a FakeResp
    or an Exception to raise; default is a 200 ok."""

    def __init__(self, handler=None):
        self.calls = []
        self._handler = handler
        self.closed = False

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        prior = sum(1 for c in self.calls if c["method"] == method)
        self.calls.append({"method": method, "url": url, "data": dict(data or {}), "has_file": bool(files)})
        result = self._handler(method, prior) if self._handler else FakeResp()
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True

    def methods(self):
        return [c["method"] for c in self.calls]


def _mk_db(words=3):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    init_db(tmp.name)
    for i in range(words):
        add_word(tmp.name, {"type": "nomen", "article": "das", "german": f"Wort{i}",
                            "plural": f"Wort{i}e", "russian": f"слово{i}", "topic": "1"})
    return tmp.name


def _config(db_path, **kw):
    return B.BackupConfig(token=TOKEN, channel_id="-100999", alert_chat_id="42",
                          db_path=db_path, retries=kw.get("retries", 3),
                          backoff_seconds=kw.get("backoff_seconds", 0.0))


def _gunzip_to(gz_path, out_path):
    with gzip.open(gz_path, "rb") as fin, open(out_path, "wb") as fout:
        shutil.copyfileobj(fin, fout)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db(words=3)
        self.work = tempfile.mkdtemp()
        self.addCleanup(lambda: os.unlink(self.db))
        self.addCleanup(lambda: shutil.rmtree(self.work, ignore_errors=True))

    def test_snapshot_is_valid_gzip_with_expected_meta(self):
        now = datetime(2026, 7, 12, 3, 0, tzinfo=timezone.utc)
        gz, meta = B.build_snapshot(self.db, self.work, now=now)
        self.assertTrue(gz.name.endswith("words-20260712-0300.db.gz"), gz.name)
        # gunzip -> a real SQLite DB that passes integrity_check
        restored = os.path.join(self.work, "restored.db")
        _gunzip_to(gz, restored)
        with sqlite3.connect(restored) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM words").fetchone()[0], 3)
        self.assertEqual(meta["counts"]["words"], 3)
        self.assertEqual(len(meta["sha256"]), 64)
        self.assertIn("user_version", meta)
        # the plaintext snapshot must not linger next to the .gz
        self.assertFalse(os.path.exists(os.path.join(self.work, "words-20260712-0300.db")))

    def test_missing_db_raises(self):
        with self.assertRaises(FileNotFoundError):
            B.build_snapshot(os.path.join(self.work, "nope.db"), self.work)

    def test_snapshot_consistent_under_concurrent_writes(self):
        # WAL + a writer thread: the online-backup API must still yield a
        # transactionally-consistent, integrity-clean image (not a torn copy).
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        stop = threading.Event()

        def writer():
            conn = sqlite3.connect(self.db, timeout=30)
            i = 0
            while not stop.is_set():
                try:
                    conn.execute(
                        "INSERT INTO quiz_events(user_id, word_id, mode, is_correct) VALUES (1, ?, 'ru_de', 1)",
                        (f"w{i}",),
                    )
                    conn.commit()
                    i += 1
                except sqlite3.OperationalError:
                    pass
            conn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(3):
                gz, _meta = B.build_snapshot(self.db, self.work)
                restored = os.path.join(self.work, "r.db")
                _gunzip_to(gz, restored)
                with sqlite3.connect(restored) as conn:
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                gz.unlink()
        finally:
            stop.set()
            thread.join()


class CaptionTest(unittest.TestCase):
    def test_caption_has_all_fields(self):
        meta = {"date": "2026-07-12 03:00 UTC", "user_version": 11,
                "sha256": "a" * 64, "counts": {"words": 737, "progress": 1096}, "size": 1234}
        caption = B.format_caption(meta, "words-20260712.db.gz")
        for needle in ("words-20260712.db.gz", "2026-07-12", "v11", "1234 B", "a" * 64, "words=737", "progress=1096"):
            self.assertIn(needle, caption)
        # rendered as a MarkdownV2 fenced code block (monospace, tap-to-copy)
        self.assertTrue(caption.startswith("```\n"))
        self.assertTrue(caption.endswith("\n```"))


class SendPipelineTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db(words=2)
        self.work = tempfile.mkdtemp()
        self.addCleanup(lambda: os.unlink(self.db))
        self.addCleanup(lambda: shutil.rmtree(self.work, ignore_errors=True))

    def test_success_sends_document_and_cleans_up(self):
        client = FakeClient()
        code = B.run_backup(_config(self.db), client=client, work_dir=self.work)
        self.assertEqual(code, 0)
        self.assertEqual(client.methods(), ["sendDocument"])
        self.assertEqual(client.calls[0]["data"]["chat_id"], "-100999")
        self.assertEqual(client.calls[0]["data"]["parse_mode"], "MarkdownV2")
        self.assertTrue(client.calls[0]["has_file"])
        # no leftover .gz in the work dir
        self.assertEqual([f for f in os.listdir(self.work) if f.endswith(".gz")], [])

    def test_retries_then_alerts_and_returns_1(self):
        slept = []
        client = FakeClient(lambda method, n: FakeResp(status=500) if method == "sendDocument" else FakeResp())
        code = B.run_backup(_config(self.db, retries=3), client=client,
                            sleep=slept.append, work_dir=self.work)
        self.assertEqual(code, 1)
        self.assertEqual(client.methods().count("sendDocument"), 3)
        self.assertEqual(client.methods().count("sendMessage"), 1)  # one alert
        self.assertEqual(len(slept), 2)  # backoff between the 3 attempts

    def test_fatal_missing_db_returns_2_and_alerts(self):
        client = FakeClient()
        code = B.run_backup(_config(os.path.join(self.work, "gone.db")), client=client, work_dir=self.work)
        self.assertEqual(code, 2)
        self.assertIn("sendMessage", client.methods())

    def test_token_never_leaks_in_error_or_alert(self):
        # a 401 (bad token) must not echo the token in the raised error or alert
        client = FakeClient(lambda method, n: FakeResp(status=401) if method == "sendDocument" else FakeResp())
        with self.assertRaises(B.BackupSendError) as ctx:
            B.send_document(client, TOKEN, "-100999", _mk_gz(self.work), "caption")
        self.assertNotIn(TOKEN, str(ctx.exception))

        client2 = FakeClient(lambda method, n: FakeResp(status=401) if method == "sendDocument" else FakeResp())
        B.run_backup(_config(self.db, retries=1), client=client2, work_dir=self.work)
        alert = next(c for c in client2.calls if c["method"] == "sendMessage")
        self.assertNotIn(TOKEN, alert["data"]["text"])

    def test_transport_error_is_scrubbed(self):
        import httpx

        def boom(method, n):
            return httpx.ConnectError("boom")

        client = FakeClient(boom)
        with self.assertRaises(B.BackupSendError) as ctx:
            B.send_message(client, TOKEN, "42", "hi")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("transport error", str(ctx.exception))


def _mk_gz(work_dir):
    path = os.path.join(work_dir, "x.db.gz")
    with gzip.open(path, "wb") as f:
        f.write(b"not-a-real-db")
    return path


if __name__ == "__main__":
    unittest.main()
