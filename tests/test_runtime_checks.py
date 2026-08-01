"""Startup guard for the installed python-telegram-bot version.

Prod ran PTB 20.7 against code that needs 22.x and failed only inside handlers
(2026-07-04: 10 TypeErrors surfaced to users). The guard must fail at startup.
"""

import unittest
from unittest import mock

from _telegram_stub import install

install()

from bot.common import runtime_checks  # noqa: E402


class PtbVersionGuardTest(unittest.TestCase):
    def _patch(self, version, info):
        # __version_info__ is preferred; some builds only carry the string.
        patches = [mock.patch.object(runtime_checks.telegram, "__version__", version, create=True)]
        if info is None:
            patches.append(
                mock.patch.object(runtime_checks.telegram, "__version_info__", (), create=True)
            )
        else:
            patches.append(
                mock.patch.object(runtime_checks.telegram, "__version_info__", info, create=True)
            )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_old_version_aborts_startup(self):
        self._patch("20.7", (20, 7, 0, "final", 0))
        with self.assertRaises(SystemExit) as cm:
            runtime_checks.require_supported_ptb()
        msg = str(cm.exception)
        self.assertIn("20.7", msg)
        self.assertIn("22.0", msg)
        self.assertIn("requirements.txt", msg)

    def test_pinned_version_passes(self):
        self._patch("22.8", (22, 8, 0, "final", 0))
        self.assertEqual(runtime_checks.require_supported_ptb(), "22.8")

    def test_exact_minimum_passes(self):
        self._patch("22.0", (22, 0, 0, "final", 0))
        self.assertEqual(runtime_checks.require_supported_ptb(), "22.0")

    def test_newer_major_passes(self):
        self._patch("23.1", (23, 1, 0, "final", 0))
        self.assertEqual(runtime_checks.require_supported_ptb(), "23.1")

    def test_falls_back_to_version_string(self):
        self._patch("21.4", None)
        with self.assertRaises(SystemExit):
            runtime_checks.require_supported_ptb()

    def test_unparsable_version_does_not_block_startup(self):
        # Never let the guard itself take the bot down on an odd build string.
        self._patch("weird-build", None)
        self.assertEqual(runtime_checks.require_supported_ptb(), "weird-build")

    def test_minimum_matches_requirements_pin(self):
        with open("requirements.txt") as fh:
            pin = next(line for line in fh if "python-telegram-bot" in line and "==" in line)
        major = int(pin.split("==")[1].strip().split(".")[0])
        self.assertEqual(major, runtime_checks.MIN_PTB_VERSION[0])


if __name__ == "__main__":
    unittest.main()
