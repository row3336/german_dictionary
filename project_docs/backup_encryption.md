# Backup encryption — manual

How the off-site DB backups are encrypted, how to set it up, and how to decrypt a
backup when you need to restore. Companion to `backup_restore.md` (which covers
the full restore procedure and the acceptance gate).

Code: `bot/offsite_backup/crypto.py` · `scripts/backup_keygen.py` ·
`scripts/backup_decrypt.py`.

---

## 1. How it works (in one paragraph)

Each backup is sealed with a **libsodium sealed box** (Curve25519 + XSalsa20-
Poly1305, via PyNaCl) — **asymmetric** encryption. There are two keys:

- **Public key** — lives on the server (`BACKUP_PUBLIC_KEY`). It can **encrypt**
  backups but **cannot decrypt** them. It is *not* secret.
- **Private key** — lives only with you, offline. It is the **only** thing that
  can **decrypt** a backup.

Consequence: someone who gets into the backup channel — or even someone who
compromises the server — sees only ciphertext. There is no decryption secret on
the server to steal. The trade-off is the one rule below.

> ⚠️ **The one rule:** if you lose the private key, **every** backup is
> permanently unrecoverable. Keep it in a password manager **and** one offline
> copy (e.g. a USB stick in a drawer).

---

## 2. One-time setup

### 2.1 Generate the keypair (do this on your own machine)
```bash
python3 -m scripts.backup_keygen            # optional: pass an output path
```
It prints the **public key** and writes the **private key** to a `0600` file
(default `backup_private_key.txt`) — the private key is never printed, so it
can't end up in a terminal log.

- Copy the printed public key → you'll put it in the server env (next step).
- Move the private-key file somewhere safe: password manager + one offline copy.
  **Do not** put it on the server and **do not** commit it.

### 2.2 Configure the server
Add to the server `.env` (never committed):
```
BACKUP_BOT_TOKEN=<backup bot token>
BACKUP_CHANNEL_ID=<-100… channel id>
BACKUP_PUBLIC_KEY=<the public key from 2.1>
BACKUP_ALERT_CHAT_ID=<optional; defaults to the channel>
```
That's it. From the next run on, backups are encrypted.

---

## 3. Everyday behaviour (nothing to do)

- With `BACKUP_PUBLIC_KEY` set, every backup is sealed before upload. The file in
  the channel is `words-YYYYMMDD-HHMM.db.gz.enc` and the caption says
  **`🔒 encrypted`**.
- If the key is **missing/misconfigured**, the script still runs but sends a
  plaintext `.gz` and the caption says **`⚠️ plaintext`** — a misconfig is
  visible, never silent. If you ever see `⚠️ plaintext`, fix `BACKUP_PUBLIC_KEY`.
- The plaintext snapshot is created only for a moment on the server and deleted
  right after sealing — only the encrypted copy leaves the machine.

---

## 4. Decrypting a backup (restore side)

Do this **on your own machine**, where the private key lives — never copy the
private key to the server.

```bash
# 1. (optional) verify the download matches the message caption
sha256sum words-YYYYMMDD-HHMM.db.gz.enc

# 2. decrypt: .enc -> .gz  (needs your private-key file)
python3 -m scripts.backup_decrypt words-YYYYMMDD-HHMM.db.gz.enc backup_private_key.txt
#   -> writes words-YYYYMMDD-HHMM.db.gz  (drop the .enc)
#   or pass an explicit output name as a 3rd argument

# 3. gunzip -> the SQLite DB, then sanity-check it
gunzip words-YYYYMMDD-HHMM.db.gz
sqlite3 words-YYYYMMDD-HHMM.db 'PRAGMA integrity_check;'   # must print: ok
```

Then follow `backup_restore.md` from the "stop the bot → swap → start" steps to
put it back into service.

---

## 5. Key management

- **Safeguard:** private key in a password manager **+** one offline copy. Losing
  it = losing every backup.
- **The public key is not secret.** It can sit in the env, in configs, anywhere.
  It only lets someone *make* backups you can read — not read yours.
- **Rotation** (if you ever suspect the private key leaked): generate a new
  keypair (§2.1), replace `BACKUP_PUBLIC_KEY` on the server. New backups use the
  new key. **Keep the old private key** as long as you might still need to decrypt
  old backups made with the old key — old and new archives need their respective
  private keys.
- **Never** email/paste the private key, and never store it on the server.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Caption shows `⚠️ plaintext` | `BACKUP_PUBLIC_KEY` unset or empty on the server → set it. |
| `backup_decrypt` fails / garbage output | Wrong private key for this file (key rotated?), or a corrupted download — recheck sha256 against the caption. |
| `nacl` import error on the server | `pip install -r requirements.txt` (PyNaCl) in the deploy venv. |
| Lost the private key | Backups are unrecoverable. Generate a new keypair and start fresh; treat old archives as gone. |

---

## 7. Threat model (what this does and doesn't protect)

- **Protects:** the backup **archive at rest** in the channel and off the server.
  Channel access ≠ data access; server compromise ≠ backup-archive access.
- **Does not protect:** the **live** DB on the running server (whoever controls
  the server already has it) — that's out of scope for backup encryption and
  unchanged by this. Backup encryption is specifically about the copies that
  leave the machine.
