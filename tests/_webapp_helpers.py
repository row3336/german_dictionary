"""Shared fixtures for the Mini App tests (miniapp.md §11).

Not named test_* on purpose, so unittest discovery imports it only when a test asks
for it — the same trick `_telegram_stub` uses, and it works because the suite runs
with PYTHONPATH=tests.

Signing initData here rather than in each test file matters: every webapp test that
touches an authenticated route needs a valid signature, and a second hand-rolled
copy of the signing scheme would be the thing that silently stops matching
bot/webapp/auth.py.
"""

import hashlib
import hmac
import json
import os
import tempfile
import time
from urllib.parse import urlencode

from bot.storage import add_word, init_db, register_or_update_user, set_user_status

TOKEN = "123456:AAH-real-bot-token"
OTHER_TOKEN = "654321:AAH-other-bot-token"


def sign(fields, token=TOKEN):
    """Build a valid initData query string the way a Telegram client would."""
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def make_fields(user_id=4242, auth_date=None, **extra):
    """The signed payload Telegram puts in window.Telegram.WebApp.initData."""
    return {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAEtest",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test", "language_code": "ru"},
            separators=(",", ":"),
        ),
        **extra,
    }


def auth_header(user_id=4242, token=TOKEN, **field_overrides):
    """Ready-to-send `Authorization: tma <initData>` header for one user."""
    return {"Authorization": "tma " + sign(make_fields(user_id, **field_overrides), token)}


class TempDbMixin:
    """Temp database wired into the env, the way the bot reads it in production.

    The webapp resolves the database through get_db_path() on every call rather
    than holding a handle, so pointing DB_PATH at a temp file is all it takes.
    """

    OWNER_ID = 999

    def setup_temp_db(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._saved_env = {key: os.environ.get(key) for key in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(self.OWNER_ID)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def make_user(self, user_id, status="approved"):
        """Create a users row in a given access state."""
        register_or_update_user(self.db, user_id, username=f"u{user_id}")
        set_user_status(self.db, user_id, status)
        return user_id

    def add_noun(self, german, russian, topic="1", article="der", plural=""):
        return add_word(
            self.db,
            {
                "id": "unique",
                "type": "nomen",
                "article": article,
                "german": german,
                "plural": plural,
                "infinitive": "",
                "prateritum": "",
                "partizip2": "",
                "russian": russian,
                "ukrainian": "",
                "english": "",
                "topic": topic,
            },
        )
