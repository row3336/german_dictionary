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


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s offsite_backup %(levelname)s %(message)s",
    )
    load_dotenv()
    return run_backup(load_config())


if __name__ == "__main__":
    sys.exit(main())
