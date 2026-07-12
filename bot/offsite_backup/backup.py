"""Off-site DB backup (spec #4, phase 1): a standalone pipeline that ships a
consistent, gzipped snapshot of the SQLite DB to a private Telegram channel via
a DEDICATED backup bot.

Design constraints:
- **Separate bot, separate token.** The backup bot's token lives in the server
  env (``BACKUP_BOT_TOKEN``), never in git and never the main ``BOT_TOKEN``.
- **Runs outside the main bot.** Invoked by a systemd timer, not a PTB job, so a
  dead/banned main bot does not stop backups.
- **PTB-free, import-light.** The only heavy dependency is ``httpx`` (already
  pinned) for the Bot API calls; the snapshot reuses ``db_safety.backup_database``
  (SQLite online-backup API — WAL-safe, not a naked ``cp``).
- **Token hygiene.** httpx errors embed the request URL, which contains the
  token. Every Bot API call is wrapped so only the method name + status/telegram
  description ever reaches a log or an alert — the raw URL/exception never does.

Pure pieces (snapshot, caption, send, retry) are split out for unit testing with
a fake HTTP client.
"""

import gzip
import hashlib
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from bot.common.db_safety import backup_database, get_user_version
from bot.offsite_backup import crypto

_API = "https://api.telegram.org/bot{token}/{method}"

# Captions go out as a MarkdownV2 fenced code block (monospace, tap-to-copy).
CAPTION_PARSE_MODE = "MarkdownV2"
# Inside a code fence only backslash and backtick need escaping (Telegram rule).
_MDV2_CODE_ESCAPE = str.maketrans({"\\": "\\\\", "`": "\\`"})

# Row-count tables reported in the caption (an invariant fingerprint of the
# snapshot). Missing tables are skipped so an older schema still backs up.
_COUNT_TABLES = ("words", "progress", "learning_items", "quiz_events", "users", "user_settings")

log = logging.getLogger("offsite_backup")


class BackupSendError(RuntimeError):
    """A Bot API call failed. Its message is scrubbed of the URL/token — safe to
    log or forward in an alert."""


@dataclass
class BackupConfig:
    token: str
    channel_id: str
    alert_chat_id: str  # where failures are announced (may equal channel_id)
    db_path: Path
    retries: int = 3
    backoff_seconds: float = 5.0
    public_key: object = None  # nacl PublicKey; None -> send plaintext .gz


def load_config(env=None):
    """Build a BackupConfig from the environment. Raises SystemExit with a clear
    message (no secrets) if the required vars are missing. Encryption is on when
    BACKUP_PUBLIC_KEY is set (recommended); the caption always states which."""
    env = env or os.environ
    token = (env.get("BACKUP_BOT_TOKEN") or "").strip()
    channel = (env.get("BACKUP_CHANNEL_ID") or "").strip()
    if not token or not channel:
        raise SystemExit("offsite_backup: BACKUP_BOT_TOKEN and BACKUP_CHANNEL_ID are required")
    alert = (env.get("BACKUP_ALERT_CHAT_ID") or "").strip() or channel
    db_path = Path(env.get("DB_PATH", "./data/words.db"))
    retries = int(env.get("BACKUP_RETRIES", "3"))
    public_key = crypto.load_public_key(env.get("BACKUP_PUBLIC_KEY"))
    if public_key is None:
        log.warning("BACKUP_PUBLIC_KEY not set — backups will be sent UNENCRYPTED")
    return BackupConfig(token=token, channel_id=channel, alert_chat_id=alert,
                        db_path=db_path, retries=retries, public_key=public_key)


def _table_counts(db_path):
    counts = {}
    with sqlite3.connect(db_path) as conn:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _COUNT_TABLES:
            if table in existing:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_snapshot(db_path, work_dir, now=None, public_key=None):
    """WAL-safe consistent snapshot of the DB, gzipped and (if a public key is
    given) sealed. Returns (artifact_path, meta).

    meta carries the fields the caption needs: human date, schema user_version,
    sha256 of the artifact actually sent, per-table row counts, size, and whether
    it is encrypted.
    """
    now = now or datetime.now(timezone.utc)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    # Date + UTC time: two backups a day (03:00 and 15:00 UTC) must not collide.
    stamp = now.strftime("%Y%m%d-%H%M")
    raw = work / f"words-{stamp}.db"
    if backup_database(db_path, raw) is None:
        raise FileNotFoundError(f"no DB to back up at {db_path}")
    try:
        version = get_user_version(raw)
        counts = _table_counts(raw)
        gz = work / f"words-{stamp}.db.gz"
        with open(raw, "rb") as fin, gzip.open(gz, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    finally:
        raw.unlink(missing_ok=True)  # the plaintext snapshot never lingers on disk
    artifact = gz
    encrypted = False
    if public_key is not None:
        enc = work / f"{gz.name}.enc"
        crypto.encrypt_file(public_key, gz, enc)
        gz.unlink(missing_ok=True)  # only the sealed copy leaves this box
        artifact, encrypted = enc, True
    meta = {
        "date": now.strftime("%Y-%m-%d %H:%M UTC"),
        "user_version": version,
        "sha256": _sha256(artifact),
        "counts": counts,
        "size": artifact.stat().st_size,
        "encrypted": encrypted,
    }
    return artifact, meta


def format_caption(meta, filename):
    """Caption as a MarkdownV2 fenced code block — renders monospace and the whole
    block (sha256 included) is tap-to-copy in Telegram. Send with
    parse_mode=CAPTION_PARSE_MODE."""
    counts = "\n".join(f"{k}={v}" for k, v in meta["counts"].items())
    lock = "🔒 encrypted" if meta.get("encrypted") else "⚠️ plaintext"
    body = (
        f"🗄 DB backup {filename}\n"
        f"date {meta['date']} · schema v{meta['user_version']} · {meta['size']} B · {lock}\n"
        f"sha256 {meta['sha256']}\n"
        f"{counts}"
    )
    return "```\n" + body.translate(_MDV2_CODE_ESCAPE) + "\n```"


def _post(client, token, method, data=None, files=None, timeout=120):
    """POST to the Bot API. Never lets the token-bearing URL escape in an error:
    transport/HTTP failures are re-raised as BackupSendError with only the method
    name + status (the ``from None`` drops the original chain that quotes the URL)."""
    url = _API.format(token=token, method=method)
    try:
        resp = client.post(url, data=data, files=files, timeout=timeout)
    except httpx.HTTPError as exc:
        raise BackupSendError(f"{method}: transport error {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise BackupSendError(f"{method}: HTTP {resp.status_code}") from None
    payload = resp.json()
    if not payload.get("ok"):
        raise BackupSendError(f"{method}: not ok ({payload.get('description')})") from None
    return payload


def send_document(client, token, chat_id, file_path, caption, parse_mode=CAPTION_PARSE_MODE):
    file_path = Path(file_path)
    with open(file_path, "rb") as handle:
        files = {"document": (file_path.name, handle, "application/gzip")}
        data = {"chat_id": str(chat_id), "caption": caption}
        if parse_mode:
            data["parse_mode"] = parse_mode
        return _post(client, token, "sendDocument", data=data, files=files)


def send_message(client, token, chat_id, text):
    return _post(client, token, "sendMessage", data={"chat_id": str(chat_id), "text": text})


def _alert(client, config, text):
    """Best-effort failure notice. Swallows its own errors so a broken alert
    channel can't mask the real (already-logged) failure."""
    try:
        send_message(client, config.token, config.alert_chat_id, text)
    except Exception:  # noqa: BLE001
        log.error("alert delivery failed too")


def run_backup(config, client=None, sleep=time.sleep, work_dir=None, now=None):
    """Run the full pipeline. Returns an exit code: 0 = sent, 1 = send failed
    after all retries, 2 = fatal (e.g. no DB). The token is never logged."""
    own_client = client is None
    client = client or httpx.Client()
    work_dir = work_dir or (config.db_path.parent / "offsite_tmp")
    gz = None
    try:
        gz, meta = build_snapshot(config.db_path, work_dir, now=now, public_key=config.public_key)
        caption = format_caption(meta, gz.name)
        last_error = None
        for attempt in range(1, config.retries + 1):
            try:
                send_document(client, config.token, config.channel_id, gz, caption)
                log.info("sent %s (%d B, schema v%s) to channel", gz.name, meta["size"], meta["user_version"])
                return 0
            except BackupSendError as exc:
                last_error = exc
                log.warning("send attempt %d/%d failed: %s", attempt, config.retries, exc)
                if attempt < config.retries:
                    sleep(config.backoff_seconds * attempt)
        _alert(client, config, f"⚠️ DB backup FAILED after {config.retries} attempts — {last_error}")
        return 1
    except Exception as exc:  # noqa: BLE001 - any fatal must alert, not crash silently
        log.error("fatal: %s", type(exc).__name__)
        _alert(client, config, f"⚠️ DB backup fatal: {type(exc).__name__}: {exc}")
        return 2
    finally:
        if gz is not None:
            Path(gz).unlink(missing_ok=True)  # ship-and-forget; the channel is the store
        if own_client:
            client.close()
