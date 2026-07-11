"""todo T2 — duplicate-safe _reply_text_with_retry.

Connect-phase failures (request never left the machine) are retried; read-phase
failures (the message may already be delivered) are swallowed silently — no
resend (a duplicate quiz question is worse), no extra notification (owner
decision 2026-07-11). RetryAfter keeps its wait-and-retry behavior.
"""

import asyncio
import unittest

import httpx

from _telegram_stub import install

install()

import bot.common.navigation as NAV  # noqa: E402
from telegram.error import NetworkError, RetryAfter, TimedOut  # noqa: E402


def _exc_with_cause(exc_cls, cause):
    exc = exc_cls("boom")
    exc.__cause__ = cause
    return exc


class _Msg:
    """reply_text stub failing per the scripted exception list, then succeeding."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    async def reply_text(self, text, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"ok": True, "text": text}


class ReplyRetryTest(unittest.TestCase):
    def setUp(self):
        # no real backoff sleeps in tests
        self._old_sleep = NAV.asyncio.sleep

        async def _instant(_secs):
            return None

        NAV.asyncio.sleep = _instant
        self.addCleanup(lambda: setattr(NAV.asyncio, "sleep", self._old_sleep))

    def _run(self, msg):
        return asyncio.run(NAV._reply_text_with_retry(msg, "hi"))

    def test_connect_failures_are_retried(self):
        msg = _Msg([
            _exc_with_cause(NetworkError, httpx.ConnectError("no route")),
            _exc_with_cause(TimedOut, httpx.ConnectTimeout("slow connect")),
        ])
        result = self._run(msg)
        self.assertEqual(msg.calls, 3)
        self.assertEqual(result["ok"], True)

    def test_read_failure_is_silent_no_resend(self):
        msg = _Msg([_exc_with_cause(TimedOut, httpx.ReadTimeout("lost ack"))])
        result = self._run(msg)
        self.assertIsNone(result)
        self.assertEqual(msg.calls, 1)  # exactly one attempt — never resent

    def test_unknown_cause_is_treated_as_maybe_delivered(self):
        # PTB not chaining a cause -> conservative: do not resend
        msg = _Msg([TimedOut("bare timeout")])
        result = self._run(msg)
        self.assertIsNone(result)
        self.assertEqual(msg.calls, 1)

    def test_retry_after_still_retries(self):
        exc = RetryAfter("flood")
        exc.retry_after = 0
        msg = _Msg([exc])
        result = self._run(msg)
        self.assertEqual(msg.calls, 2)
        self.assertEqual(result["ok"], True)

    def test_connect_failures_exhaust_and_raise(self):
        # 4 connect failures > max_retries=3 -> raise (truly undelivered; the
        # global error handler is the one place that nudges the user)
        failures = [_exc_with_cause(NetworkError, httpx.ConnectError("x")) for _ in range(4)]
        msg = _Msg(failures)
        with self.assertRaises(NetworkError):
            self._run(msg)
        self.assertEqual(msg.calls, 4)


if __name__ == "__main__":
    unittest.main()
