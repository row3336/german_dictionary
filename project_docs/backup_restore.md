# Off-site DB backup & restore (spec #4)

Nightly, a dedicated **backup bot** (`@rowsbackup_bot`, its own token — server env
only, never git) ships a gzipped, WAL-safe snapshot of `words.db` to a **private
Telegram channel**. The channel history is the store (keep everything). This is
independent of the main bot: if Telegram bans the main bot, the backups survive.

- Pipeline: `scripts/offsite_backup.py` → `bot/offsite_backup/backup.py`
  (`backup_database()` online-backup API → gzip → `sendDocument`).
- Schedule: `deploy/offsite-backup.{service,timer}`, twice daily at 03:00 and
  15:00 UTC.
- Filename: `words-YYYYMMDD-HHMM.db.gz` (UTC). Caption: date, schema `user_version`, sha256
  of the `.gz`, and key row counts (words / progress / learning_items /
  quiz_events / users / user_settings).
- Failure handling: 3 attempts with backoff; on persistent failure the bot posts
  an alert to `BACKUP_ALERT_CHAT_ID` (defaults to the channel) and the process
  exits non-zero (journal-visible). Monitor by silence — no nightly file = check.

## Server env (never commit)
```
BACKUP_BOT_TOKEN=<@rowsbackup_bot token>
BACKUP_CHANNEL_ID=<-100… private channel id>
BACKUP_ALERT_CHAT_ID=<optional; defaults to the channel>
# DB_PATH is already set for the main bot and is reused.
```

## Restore procedure
Assumes the backup `.gz` was downloaded from the channel to the server.

1. **Verify the archive before touching production.**
   ```bash
   gunzip -k words-YYYYMMDD.db.gz                 # -> words-YYYYMMDD.db
   sha256sum words-YYYYMMDD.db.gz                 # compare to the message caption
   sqlite3 words-YYYYMMDD.db 'PRAGMA integrity_check;'    # must print: ok
   venv/bin/python3 -m scripts.smoke_check_db --db words-YYYYMMDD.db
   ```
   Also eyeball the caption's row counts against expectations before proceeding.
2. **Stop the bot** so nothing writes during the swap:
   `systemctl stop deutsch-alphatest`  (prod: the prod service name).
3. **Back up the current (possibly bad) DB** — never overwrite blind:
   `cp data/words.db data/words.before-restore-$(date +%Y%m%d-%H%M%S).db`
   (also remove stale `-wal`/`-shm` sidecars: `rm -f data/words.db-wal data/words.db-shm`).
4. **Swap in** the verified snapshot: `cp words-YYYYMMDD.db data/words.db`.
5. **Start the bot**: `systemctl start deutsch-alphatest`.
6. **Healthcheck**: `venv/bin/python3 -m scripts.smoke_check_db` on the live DB and
   confirm the bot answers `/start`; watch `journalctl -u <service> -n 50`.

## Acceptance (gate — do once, record in log.md)
- [ ] Owner creates `@rowsbackup_bot` (done) + a **private channel** and adds the bot
      as **admin**; capture the channel `chat_id` into `BACKUP_CHANNEL_ID`.
- [ ] `systemctl start offsite-backup.service` once → a `.gz` lands in the channel
      with a correct caption (sha256 matches, counts sane).
- [ ] Timer left enabled 2–3 days → two files a day, at ~03:00 and ~15:00 UTC.
- [ ] **One real restore rehearsal on the test server** following the steps above
      (verify → stop → swap → start → healthcheck). Only then is the gate closed.

## Later (phase 3, not now)
Second, Telegram-independent tier: `rclone copy` the same `.gz` to cloud
(Google Drive / S3 / Backblaze) from a separate cron/timer, so a Telegram outage
or bot ban can't take out both copies.
