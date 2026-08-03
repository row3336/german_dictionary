"""todo T4 — old .com VF pipeline removed; parse_VF is now a proxy mini-menu.

The settings parse_VF button opens a two-button menu (set_proxy / test_proxy).
test_proxy must stay traffic-cheap (the proxy is paid per traffic): a HEAD with
a robots.txt fallback, never a dictionary-page download.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

import bot.verb_forms_parsing.de_fetch as DF  # noqa: E402
from bot.labels import PARSE_VF_TEXT, VF_SET_PROXY_TEXT, VF_TEST_PROXY_TEXT  # noqa: E402
from bot.storage import get_setting, init_db  # noqa: E402
from bot.verb_forms_parsing.parser import VF_PROXY_KEY  # noqa: E402

OWNER = 999
GUEST = 111


class _FakeProc:
    def __init__(self, code):
        self.returncode = 0
        self.stdout = f"\n__HTTP__{code}"
        self.stderr = ""


class TestProxyCheapCheckTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._old_run = DF.subprocess.run

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return self.responses.pop(0)

        DF.subprocess.run = fake_run
        self.addCleanup(lambda: setattr(DF.subprocess, "run", self._old_run))

    def test_head_ok(self):
        self.responses = [_FakeProc(200)]
        ok, code = DF.test_proxy("socks5://u:p@h:1")
        self.assertTrue(ok)
        self.assertEqual(code, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("-I", self.calls[0])          # HEAD, headers only
        self.assertIn("-x", self.calls[0])          # went through the proxy
        self.assertNotIn("?w=", " ".join(self.calls[0]))  # never a page fetch

    def test_head_rejected_falls_back_to_robots(self):
        self.responses = [_FakeProc(405), _FakeProc(200)]
        ok, code = DF.test_proxy("socks5://u:p@h:1")
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 2)
        self.assertIn(f"{DF.BASE}/robots.txt", self.calls[1])
        self.assertIn("/dev/null", " ".join(self.calls[1]))  # body discarded

    def test_transport_failure(self):
        self.responses = [_FakeProc(0), _FakeProc(0)]
        ok, code = DF.test_proxy("socks5://u:p@h:1")
        self.assertFalse(ok)
        self.assertEqual(code, 0)


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, "markup": kw.get("reply_markup")})
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


class VfProxyMenuTest(unittest.TestCase):
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

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _send(self, uid, text, ctx=None):
        from bot.handlers.settings import settings_menu_choice
        ctx = ctx or _Ctx()
        upd = _Update(uid, text)
        asyncio.run(settings_menu_choice(upd, ctx))
        return upd, ctx

    def test_owner_opens_proxy_mini_menu(self):
        upd, ctx = self._send(OWNER, PARSE_VF_TEXT)
        self.assertTrue(ctx.user_data.get("vf_menu"))
        reply = upd.message.replies[-1]["text"]
        self.assertIn("proxy =", reply)
        # the old pipeline buttons are gone from the reply keyboard
        rows = upd.message.replies[-1]["markup"].rows
        flat = [btn for row in rows for btn in row]
        self.assertIn(VF_SET_PROXY_TEXT, flat)
        self.assertIn(VF_TEST_PROXY_TEXT, flat)
        self.assertNotIn("dry_run_new", flat)
        self.assertNotIn("set_sleep", flat)

    def test_guest_is_blocked(self):
        upd, ctx = self._send(GUEST, PARSE_VF_TEXT)
        self.assertFalse(ctx.user_data.get("vf_menu"))

    def test_set_proxy_saves_normalized(self):
        _, ctx = self._send(OWNER, PARSE_VF_TEXT)
        upd, ctx = self._send(OWNER, VF_SET_PROXY_TEXT, ctx)
        self.assertEqual(ctx.user_data.get("vf_waiting"), "proxy")
        upd, ctx = self._send(OWNER, "host:1080:user:pass", ctx)
        self.assertEqual(get_setting(self.db, VF_PROXY_KEY, ""), "socks5://user:pass@host:1080")
        self.assertIn("✅ proxy saved", upd.message.replies[-1]["text"])

    def test_test_proxy_requires_proxy(self):
        _, ctx = self._send(OWNER, PARSE_VF_TEXT)
        upd, _ = self._send(OWNER, VF_TEST_PROXY_TEXT, ctx)
        self.assertIn("❌ proxy is required", upd.message.replies[-1]["text"])

    def test_test_proxy_reports_result(self):
        import bot.handlers.settings as S
        self._old_test = S._run_vf_proxy_test

        async def fake_test(proxy):
            return True, 200

        S._run_vf_proxy_test = fake_test
        self.addCleanup(lambda: setattr(S, "_run_vf_proxy_test", self._old_test))
        _, ctx = self._send(OWNER, PARSE_VF_TEXT)
        self._send(OWNER, VF_SET_PROXY_TEXT, ctx)
        self._send(OWNER, "host:1080:user:pass", ctx)
        upd, _ = self._send(OWNER, VF_TEST_PROXY_TEXT, ctx)
        self.assertIn("✅ proxy ok (http 200)", upd.message.replies[-1]["text"])


if __name__ == "__main__":
    unittest.main()
