"""Spec #14 — vMAJOR.MINOR versioning. APP_VERSION is the single source of
truth (bot/app_config.py); the stat screen surfaces it to every user so a bug
report can name the version without asking the owner. Bumping the constant
and tagging the commit are separate, manual steps (see claude_docs/
start_new_session point 8) - not exercised here, just the plumbing.
"""

import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import APP_VERSION  # noqa: E402
from bot.handlers.owner_stats import _format_progress_text  # noqa: E402
from bot.storage import init_db  # noqa: E402

OWNER = 999


class AppVersionConstantTest(unittest.TestCase):
    def test_matches_major_minor_format(self):
        self.assertRegex(APP_VERSION, r"^\d+\.\d+$")


class StatScreenVersionLineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_progress_text_contains_bot_version_line(self):
        text = _format_progress_text(OWNER)
        self.assertIn(f"bot version = v{APP_VERSION}", text)
        # It's the first thing shown, not buried in the middle of the block.
        self.assertTrue(text.strip().startswith("bot version = v"))


if __name__ == "__main__":
    unittest.main()
