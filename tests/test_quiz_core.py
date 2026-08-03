"""The quiz core stays callable without Telegram (miniapp.md §5.1, §5.3).

The point of `bot/common/quiz_core.py` is that the mini app reaches its verdict by
calling the bot's own function rather than a copy of it. That only holds while the
core imports nothing from PTB or from the handlers — the day someone reaches for a
keyboard or a `context` in there, the app can no longer use it and a second
implementation starts growing. This test is the guard on that.

It runs in a subprocess deliberately: within one pytest session another test has
usually installed the telegram stub already, so an in-process import would prove
nothing about what the core actually needs.
"""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PROBE = textwrap.dedent(
    """
    import sys

    from bot.common.quiz_core import (
        _check_ru_de_strict,
        _plural_matches,
        _verb_answer_candidates,
    )

    assert "telegram" not in sys.modules, "quiz_core pulled in PTB"

    # No "id" on the entries: synonym lookup is the only DB touch in this path,
    # and it is skipped for an entry that has no word id.
    nomen = {"type": "nomen", "article": "der", "german": "Tisch", "plural": "Tische"}
    ok, checks = _check_ru_de_strict(nomen, "der Tisch Tische")
    assert ok and checks == {"article": True, "plural": True}, (ok, checks)
    ok, checks = _check_ru_de_strict(nomen, "die Tisch Tische")
    assert not ok and checks["article"] is False, (ok, checks)

    verb = {"type": "verb", "infinitive": "gehen", "prateritum": "ging",
            "partizip2": "gegangen", "aux": "ist"}
    short, full = _verb_answer_candidates(verb)
    assert short and full and short != full, (short, full)
    assert _check_ru_de_strict(verb, "gehen ging ist gegangen")[0]

    assert _plural_matches("Tische", "Tische")
    assert _plural_matches("die Tische", "Tische")
    assert not _plural_matches("Tischen", "Tische")

    print("ok")
    """
)


class QuizCoreWithoutTelegramTest(unittest.TestCase):
    def test_core_answers_without_ptb_installed(self):
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
