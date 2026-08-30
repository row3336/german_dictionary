"""Keep Telegram credentials out of application logs."""

import logging
import re

from telegram.error import InvalidToken


REDACTED = "<REDACTED>"
_TELEGRAM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])\d{6,15}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)


def redact_sensitive_text(text, secrets=()):
    """Return text with configured secrets and Telegram-shaped tokens removed."""
    redacted = str(text)
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        redacted = redacted.replace(str(secret), REDACTED)
    return _TELEGRAM_TOKEN_RE.sub(REDACTED, redacted)


class SecretRedactingFormatter(logging.Formatter):
    """Redact secrets after formatting, including any exception traceback."""

    def __init__(self, *args, secrets=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record):
        return redact_sensitive_text(super().format(record), self._secrets)


def run_polling_safely(application, logger):
    """Run polling without allowing an invalid token traceback to escape."""
    try:
        application.run_polling()
    except InvalidToken:
        logger.error("Telegram rejected BOT_TOKEN; service stopped.")
        return False
    return True
