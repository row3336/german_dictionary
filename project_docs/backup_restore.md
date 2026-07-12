# Off-site DB backup & restore (spec #4)

Nightly, a dedicated **backup bot** (`@rowsbackup_bot`, its own token — server env
only, never git) ships a gzipped, WAL-safe snapshot of `words.db` to a **private
Telegram channel**. The channel history is the store (keep everything). This is
independent of the main bot: if Telegram bans the main bot, the backups survive.

- Pipeline: `scripts/offsite_backup.py` → `bot/offsite_backup/backup.py`
  (`backup_database()` online-backup API → gzip → `sendDocument`).
- Schedule: `deploy/offsite-backup.{service,timer}`, twice daily at 03:00 and
  15:00 UTC.
- Filename: `words-YYYYMMDD-HHMM.db.gz.enc` (UTC; `.enc` = sealed, see Encryption).
  Caption: date, schema `user_version`, size, encryption state, sha256 of the sent
  artifact, and key row counts (words / progress / learning_items / quiz_events /
  users / user_settings).
- Failure handling: 3 attempts with backoff; on persistent failure the bot posts
  an alert to `BACKUP_ALERT_CHAT_ID` (defaults to the channel) and the process
  exits non-zero (journal-visible). Monitor by silence — no nightly file = check.

## Encryption (asymmetric — spec #4)
Backups are sealed with a libsodium sealed box (`bot/offsite_backup/crypto.py`):
the server holds only the **public** key and can encrypt but **not** decrypt; the
**private** key stays with the owner, offline, and is the only thing that can open
a backup. A leaked channel — or even a compromised server — never exposes backup
contents.

- Generate a keypair once:
  ```bash
  python3 -m scripts.backup_keygen            # prints the public key; writes private key to a 0600 file
  ```
  Put the printed public key in the server env as `BACKUP_PUBLIC_KEY`. Keep the
  private-key file yourself (password manager + one offline copy). **Losing the
  private key makes every backup permanently unrecoverable.** The private key must
  never touch the server or git.
- If `BACKUP_PUBLIC_KEY` is unset the script sends plaintext `.gz` and the caption
  says `⚠️ plaintext` — so a misconfig is visible, not silent.

## Server env (never commit)
```
BACKUP_BOT_TOKEN=<@rowsbackup_bot token>
BACKUP_CHANNEL_ID=<-100… private channel id>
BACKUP_PUBLIC_KEY=<base64 public key from backup_keygen>
BACKUP_ALERT_CHAT_ID=<optional; defaults to the channel>
# DB_PATH is already set for the main bot and is reused.
```

## Restore procedure
Assumes the backup `.gz` was downloaded from the channel to the server.

1. **Decrypt + verify the archive before touching production.** Do the decrypt on
   your own machine (the private key must never reach the server):
   ```bash
   sha256sum words-YYYYMMDD-HHMM.db.gz.enc        # compare to the message caption
   python3 -m scripts.backup_decrypt words-YYYYMMDD-HHMM.db.gz.enc backup_private_key.txt
   gunzip -k words-YYYYMMDD-HHMM.db.gz            # -> words-YYYYMMDD-HHMM.db
   sqlite3 words-YYYYMMDD-HHMM.db 'PRAGMA integrity_check;'   # must print: ok
   venv/bin/python3 -m scripts.smoke_check_db --db words-YYYYMMDD-HHMM.db
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
- [x] `@rowsbackup_bot` created + private channel "Backups" + bot admin; `chat_id`
      captured. Keypair generated (private key on the owner's machine, 0600).
- [x] Encrypted send verified end-to-end locally: sealed `.db.gz.enc` delivered to
      the channel; decrypt → gunzip → `integrity_check ok` with the real keypair.
- [ ] Put `BACKUP_BOT_TOKEN` / `BACKUP_CHANNEL_ID` / `BACKUP_PUBLIC_KEY` in the
      **server** `.env`; install `deploy/offsite-backup.{service,timer}`; `systemctl
      start offsite-backup.service` once → a `.db.gz.enc` lands in the channel.
- [ ] Timer left enabled 2–3 days → two files a day, at ~03:00 and ~15:00 UTC.
- [ ] **One real restore rehearsal on the test server** (decrypt → verify → stop →
      swap → start → healthcheck). Only then is the gate closed.

## Later (phase 3, not now)
Second, Telegram-independent tier: `rclone copy` the same `.gz` to cloud
(Google Drive / S3 / Backblaze) from a separate cron/timer, so a Telegram outage
or bot ban can't take out both copies.
