#!/usr/bin/env python3
"""Off-site DB backup entrypoint (spec #4, phase 1).

Loads ``.env``, runs the backup pipeline, exits non-zero on failure so a systemd
timer (or an OnFailure unit) can react. Meant to be launched from a timer, NOT
from inside the main bot:

    python3 -m scripts.offsite_backup

Required env (server only, never in git): BACKUP_BOT_TOKEN, BACKUP_CHANNEL_ID.
Optional: BACKUP_ALERT_CHAT_ID (defaults to the channel), BACKUP_RETRIES, DB_PATH.
"""
import logging
import sys

from dotenv import load_dotenv

from bot.offsite_backup.backup import load_config, run_backup


def configure_logging():
    """Keep token-bearing Bot API URLs out of the systemd journal."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s offsite_backup %(levelname)s %(message)s",
    )
    # Telegram embeds the bot token in the request path. httpx logs the complete
    # URL at INFO after every request, so the dedicated backup process must
    # suppress transport access logs even when the application logs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main():
    configure_logging()
    load_dotenv()
    return run_backup(load_config())


if __name__ == "__main__":
    sys.exit(main())
