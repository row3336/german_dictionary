"""The owner-only `unit_tests` button must shell out to the running interpreter.

Regression for the 03.08 prod incident: the runner hardcoded "python3", so on the
server it picked the system Python instead of the venv one. Every test module that
imports telegram/httpx then died at import time and the button reported
"465/536 OK, 71 failed" while the suite was in fact green under the venv.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from _telegram_stub import install

install()

from bot.handlers import settings as S  # noqa: E402


def _completed(cmd, returncode=0, stdout="Ran 882 tests in 40s\n\nOK\n"):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")


class UnitTestsRunnerInterpreterTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _fake_run(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return _completed(cmd)

    def test_both_subprocesses_use_sys_executable(self):
        """Discover and the db smoke check both run under the current interpreter."""
        with mock.patch.object(subprocess, "run", self._fake_run):
            S._run_unit_tests_report()

        self.assertEqual(len(self.calls), 2, "expected a unittest run and a smoke check")
        for cmd, _ in self.calls:
            self.assertEqual(
                cmd[0],
                sys.executable,
                f"runner must use sys.executable, got {cmd[0]!r}",
            )

    def test_no_bare_python3_in_commands(self):
        """A bare "python3" would resolve to the system Python on the server."""
        with mock.patch.object(subprocess, "run", self._fake_run):
            S._run_unit_tests_report()

        for cmd, _ in self.calls:
            self.assertNotEqual(cmd[0], "python3")

    def test_discover_targets_the_tests_dir(self):
        with mock.patch.object(subprocess, "run", self._fake_run):
            S._run_unit_tests_report()

        discover_cmd = self.calls[0][0]
        self.assertEqual(discover_cmd[1:5], ["-m", "unittest", "discover", "-s"])
        self.assertEqual(discover_cmd[5], "tests")

    def test_runs_from_project_root(self):
        """cwd must be the project root so that `tests` and `scripts` resolve.

        Derived from the package location, never from the directory name: the
        deploy contours are checked out as german_dictionary_test (prod),
        test_new_api (alpha) and german_dictionary_dev.
        """
        expected_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(S.__file__))))

        with mock.patch.object(subprocess, "run", self._fake_run):
            S._run_unit_tests_report()

        for _, kwargs in self.calls:
            self.assertEqual(kwargs["cwd"], expected_root)

    def test_green_run_is_reported_as_ok(self):
        with mock.patch.object(subprocess, "run", self._fake_run):
            report = S._run_unit_tests_report()

        self.assertIn("✅ unit tests: 882/882 OK", report)
        self.assertIn("✅ db smoke: OK", report)

    def test_import_errors_are_surfaced_with_counts(self):
        """The failure shape seen on prod: modules dying at import time."""
        broken = (
            "ERROR: test_broadcast (unittest.loader._FailedTest)\n"
            "ERROR: test_donate (unittest.loader._FailedTest)\n"
            "Ran 536 tests in 22s\n\nFAILED (errors=2)\n"
        )

        def run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if "unittest" in cmd:
                return _completed(cmd, returncode=1, stdout=broken)
            return _completed(cmd)

        with mock.patch.object(subprocess, "run", run):
            report = S._run_unit_tests_report()

        self.assertIn("❌ unit tests: 534/536 OK, 2 failed", report)
        self.assertIn("test_broadcast", report)


if __name__ == "__main__":
    unittest.main()
