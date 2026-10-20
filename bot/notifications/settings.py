"""Personal reminder preferences, daily dedup and the atomic Learning fact."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from ..storage import _connect

BERLIN = ZoneInfo("Europe/Berlin")
LEARNING_NEW_START_KEY = "learning_new_start_date"
KINDS = ("quiz", "new_words")
_CONFIG = {
    "quiz": (True, "18:00", "fsrs_afternoon_reminder_date"),
    "new_words": (False, "14:00", "new_words_reminder_date"),
}


@dataclass(frozen=True)
class ReminderPreference:
    enabled: bool
    time: str


def valid_time(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is not None


def _get(conn, user_id, key, default=None):
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (int(user_id), key)).fetchone()
    return row[0] if row is not None else default


def _set(conn, user_id, key, value):
    conn.execute(
        "INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
        (int(user_id), key, str(value)),
    )


def read_preference(conn, user_id, kind):
    """Defaults are read-only fallbacks; never overwrite an existing preference."""
    default_enabled, default_time, _ = _CONFIG[kind]
    enabled = _get(conn, user_id, f"{kind}_reminder_enabled")
    token = str(enabled).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        enabled = True
    elif token in {"0", "false", "no", "off"}:
        enabled = False
    else:
        enabled = default_enabled
    time = _get(conn, user_id, f"{kind}_reminder_time", default_time)
    return ReminderPreference(enabled, time if valid_time(time) else default_time)


def get_preference(db_path, user_id, kind):
    with _connect(db_path) as conn:
        return read_preference(conn, user_id, kind)


def set_preference(db_path, user_id, kind, *, enabled=None, time=None):
    if kind not in KINDS:
        raise ValueError("Unknown reminder")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("Expected a boolean")
    if time is not None and not valid_time(time):
        raise ValueError("Expected HH:MM from 00:00 to 23:59")
    with _connect(db_path) as conn:
        if enabled is not None:
            _set(conn, user_id, f"{kind}_reminder_enabled", "1" if enabled else "0")
        if time is not None:
            _set(conn, user_id, f"{kind}_reminder_time", time)


def due_to_notify(db_path, user_id, kind, now):
    """Check the user's current settings/day/fact, including after prior awaits."""
    local = now.astimezone(BERLIN)
    today = local.date().isoformat()
    with _connect(db_path) as conn:
        preference = read_preference(conn, user_id, kind)
        if not preference.enabled or local.strftime("%H:%M") < preference.time:
            return False
        if _get(conn, user_id, _CONFIG[kind][2], "") == today:
            return False
        if kind == "new_words" and _get(conn, user_id, LEARNING_NEW_START_KEY, "") == today:
            return False
    return True


def mark_sent(db_path, user_id, kind, day):
    with _connect(db_path) as conn:
        _set(conn, user_id, _CONFIG[kind][2], day)


def record_learning_new_start(conn, user_id, now):
    """Call only for nonempty first-due assignment, inside its existing transaction.

    No commit/connection is opened here. UTC ISO strings from Learning are also
    accepted; the fact records the assignment's Berlin day, not its future due.
    """
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    _set(conn, user_id, LEARNING_NEW_START_KEY, now.astimezone(BERLIN).date().isoformat())
