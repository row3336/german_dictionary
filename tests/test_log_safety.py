import io
import logging
import unittest

from telegram.error import InvalidToken

from bot.common.log_safety import (
    SecretRedactingFormatter,
    run_polling_safely,
)


FAKE_TOKEN = "123456789:AA_TEST_TOKEN_VALUE_123456789012345"


class SecretRedactingFormatterTest(unittest.TestCase):
    def test_redacts_configured_secret_from_message_and_traceback(self):
        formatter = SecretRedactingFormatter(
            "%(levelname)s %(message)s",
            secrets=(FAKE_TOKEN,),
        )
        try:
            raise InvalidToken(f"The token `{FAKE_TOKEN}` was rejected")
        except InvalidToken:
            record = logging.LogRecord(
                name="telegram.ext.Application",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="Polling failed for %s",
                args=(FAKE_TOKEN,),
                exc_info=__import__("sys").exc_info(),
            )

        rendered = formatter.format(record)

        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertIn("<REDACTED>", rendered)
        self.assertIn("InvalidToken", rendered)

    def test_redacts_token_shape_without_configured_secret(self):
        formatter = SecretRedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Rejected token: %s",
            args=(FAKE_TOKEN,),
            exc_info=None,
        )

        rendered = formatter.format(record)

        self.assertEqual(rendered, "Rejected token: <REDACTED>")


class RunPollingSafelyTest(unittest.TestCase):
    def test_invalid_token_is_logged_without_propagating(self):
        class FakeApplication:
            def run_polling(self):
                raise InvalidToken(f"The token `{FAKE_TOKEN}` was rejected")

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(SecretRedactingFormatter("%(message)s", secrets=(FAKE_TOKEN,)))
        test_logger = logging.getLogger("test.log_safety.polling")
        old_handlers = test_logger.handlers[:]
        old_propagate = test_logger.propagate
        test_logger.handlers = [handler]
        test_logger.propagate = False
        self.addCleanup(setattr, test_logger, "handlers", old_handlers)
        self.addCleanup(setattr, test_logger, "propagate", old_propagate)

        result = run_polling_safely(FakeApplication(), test_logger)

        rendered = stream.getvalue()
        self.assertFalse(result)
        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertIn("BOT_TOKEN", rendered)


if __name__ == "__main__":
    unittest.main()
