"""SQLite primitives for a single atomic study transition."""

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .srs import default_srs


@contextmanager
def transaction(db_path):
    """Open one writer. Schema preparation belongs to startup, not transitions."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def utc_iso(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


def _row(conn, sql, args):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def get_progress(conn, user_id, word_id, mode, today=None):
    row = _row(conn, """SELECT interval,repetitions,ease,due FROM progress
               WHERE user_id=? AND word_id=? AND mode=?""", (user_id, word_id, mode))
    return row or default_srs(today)


def set_progress(conn, user_id, word_id, mode, srs, now=None):
    conn.execute("""INSERT INTO progress
        (user_id,word_id,mode,interval,repetitions,ease,due,updated_at)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id,word_id,mode) DO UPDATE SET
        interval=excluded.interval,repetitions=excluded.repetitions,
        ease=excluded.ease,due=excluded.due,updated_at=excluded.updated_at""",
        (user_id, word_id, mode, srs["interval"], srs["repetitions"], srs["ease"],
         srs["due"], utc_iso(now)))
    from ..conjugation.schema import admit_if_qualified
    admit_if_qualified(conn, user_id, word_id, mode, srs["repetitions"], utc_iso(now))


def capture_snapshot(conn, user_id, word_id, modes=None,
                     affected_learning_ids=None, event_ids=None):
    modes = list(dict.fromkeys(modes or []))
    progress = {mode: _row(conn, """SELECT * FROM progress
                WHERE user_id=? AND word_id=? AND mode=?""", (user_id, word_id, mode))
                for mode in modes}
    ids = list(dict.fromkeys([word_id, *(affected_learning_ids or [])]))
    items = [{"user_id": int(user_id), "word_id": wid,
              "before": _row(conn, "SELECT * FROM learning_items WHERE user_id=? AND word_id=?",
                             (user_id, wid))} for wid in ids if wid]
    events = {}
    for event_id in event_ids or []:
        row = _row(conn, "SELECT * FROM quiz_events WHERE user_id=? AND id=?", (user_id, event_id))
        if row is None:
            raise ValueError("event does not belong to user")
        events[str(event_id)] = row
    return {"version": 2, "user_id": int(user_id), "word_id": word_id,
            "modes": modes, "progress": progress,
            "learning_item": items[0]["before"] if items else None,
            "affected_learning_items": items, "event_before": events,
            "created_event_ids": []}


def _restore_learning(conn, user_id, word_id, row):
    exists = conn.execute("SELECT 1 FROM words WHERE id=?", (word_id,)).fetchone()
    batch = row and conn.execute("SELECT 1 FROM learning_batches WHERE id=? AND user_id=?",
                                (row.get("batch_id"), user_id)).fetchone()
    if not row or not exists or not batch:
        conn.execute("DELETE FROM learning_items WHERE user_id=? AND word_id=?", (user_id, word_id))
        return bool(exists)
    normalized = dict(row)
    normalized["user_id"], normalized["word_id"] = user_id, word_id
    normalized.setdefault("recovery_remaining", 0)
    normalized.setdefault("first_due_pending", int(
        normalized.get("status") == "active" and not normalized.get("preview_done")))
    allowed = {r[1] for r in conn.execute("PRAGMA table_info(learning_items)")}
    columns = [key for key in normalized if key in allowed]
    updates = ",".join(f"{key}=excluded.{key}" for key in columns if key not in {"user_id", "word_id"})
    conn.execute(f"""INSERT INTO learning_items ({','.join(columns)})
        VALUES ({','.join('?' for _ in columns)})
        ON CONFLICT(user_id,word_id) DO UPDATE SET {updates}""",
        tuple(normalized[key] for key in columns))
    return True


def _snapshot_integer(value):
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid snapshot integer") from exc
    if isinstance(value, float) and value != number:
        raise ValueError("fractional snapshot integer")
    if not -(2 ** 63) <= number < 2 ** 63:
        raise ValueError("snapshot integer outside SQLite range")
    return number


def _snapshot_mapping(value, label):
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"invalid snapshot {label}")
    return value or {}


def _snapshot_row(value, label, required):
    if not isinstance(value, dict) or any(value.get(key) is None for key in required):
        raise ValueError(f"invalid snapshot {label} row")
    for key, field in value.items():
        if not isinstance(key, str) or not isinstance(field, (str, int, float, type(None))):
            raise ValueError(f"invalid snapshot {label} field")
        if isinstance(field, int):
            _snapshot_integer(field)
        if isinstance(field, float) and not math.isfinite(field):
            raise ValueError(f"nonfinite snapshot {label} field")
        if label == "learning" and field is None:
            raise ValueError("null snapshot learning field")
    if label == "learning":
        for key, allowed in (("recovery_remaining", range(4)), ("first_due_pending", (0, 1)),
                             ("preview_done", (0, 1))):
            if key in value and _snapshot_integer(value[key]) not in allowed:
                raise ValueError("invalid snapshot learning flag")
    return value


def validate_snapshot(snapshot):
    """Reject malformed historical JSON before any restoration SQL runs.

    Missing legacy collections retain their old empty/default semantics. Invalid
    data raises ValueError; database errors are deliberately never intercepted.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("invalid snapshot object")
    _snapshot_integer(snapshot.get("user_id") or 0)
    _snapshot_integer(snapshot.get("version", 1))
    if not isinstance(snapshot.get("word_id", ""), str):
        raise ValueError("invalid snapshot word id")
    modes = snapshot.get("modes")
    if modes is not None and (not isinstance(modes, (list, tuple)) or
                              any(not isinstance(mode, str) for mode in modes)):
        raise ValueError("invalid snapshot modes")
    progress = _snapshot_mapping(snapshot.get("progress"), "progress")
    for mode, row in progress.items():
        if not isinstance(mode, str):
            raise ValueError("invalid snapshot progress mode")
        if row is not None:
            _snapshot_row(row, "progress", ("interval", "repetitions", "ease", "due", "updated_at"))
    learning = snapshot.get("learning_item")
    if learning is not None:
        _snapshot_row(learning, "learning", ("batch_id", "next_due"))
    items = snapshot.get("affected_learning_items")
    if items is not None:
        if not isinstance(items, list):
            raise ValueError("invalid snapshot learning collection")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("word_id"), str):
                raise ValueError("invalid snapshot learning entry")
            if "user_id" in item:
                _snapshot_integer(item["user_id"])
            if item.get("before") is not None:
                _snapshot_row(item["before"], "learning", ("batch_id", "next_due"))
    ids = snapshot.get("created_event_ids")
    if ids is not None:
        if not isinstance(ids, list):
            raise ValueError("invalid snapshot event ids")
        for event_id in ids:
            _snapshot_integer(event_id)
    for event_id, row in _snapshot_mapping(snapshot.get("event_before"), "events").items():
        _snapshot_integer(event_id)
        _snapshot_row(row, "event", ("word_id", "mode", "is_correct", "created_at"))
    for mode, count in _snapshot_mapping(snapshot.get("event_counts"), "event counts").items():
        if not isinstance(mode, str) or _snapshot_integer(count) < 0:
            raise ValueError("invalid snapshot event count")


def restore_snapshot(conn, snapshot):
    """Restore exact v2 effects, retaining count-based legacy compatibility."""
    if not isinstance(snapshot, dict):
        return False
    validate_snapshot(snapshot)
    user_id = int(snapshot.get("user_id") or 0)
    word_id = str(snapshot.get("word_id") or "")
    word_exists = bool(conn.execute("SELECT 1 FROM words WHERE id=?", (word_id,)).fetchone())
    for mode in snapshot.get("modes") or []:
        row = (snapshot.get("progress") or {}).get(mode)
        if row and word_exists:
            conn.execute("""INSERT INTO progress
                (user_id,word_id,mode,interval,repetitions,ease,due,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id,word_id,mode) DO UPDATE SET
                interval=excluded.interval,repetitions=excluded.repetitions,ease=excluded.ease,
                due=excluded.due,updated_at=excluded.updated_at""",
                (user_id, word_id, mode, row["interval"], row["repetitions"], row["ease"],
                 row["due"], row["updated_at"]))
        else:
            conn.execute("DELETE FROM progress WHERE user_id=? AND word_id=? AND mode=?",
                         (user_id, word_id, mode))
    items = snapshot.get("affected_learning_items")
    if items is None:
        items = [{"word_id": word_id, "before": snapshot.get("learning_item")}]
    restored = word_exists
    for item in items:
        if item.get("user_id", user_id) != user_id:
            raise ValueError("snapshot crosses users")
        restored = _restore_learning(conn, user_id, item["word_id"], item.get("before")) or restored
    from ..learning_store import touch_batch
    for batch_id in {item["before"]["batch_id"] for item in items if item.get("before")}:
        touch_batch(conn, user_id, batch_id)
    if int(snapshot.get("version", 1)) >= 2:
        for event_id in snapshot.get("created_event_ids") or []:
            conn.execute("DELETE FROM quiz_events WHERE user_id=? AND id=?", (user_id, event_id))
        for event_id, row in (snapshot.get("event_before") or {}).items():
            # A deleted parent is deliberately never resurrected.
            if conn.execute("SELECT 1 FROM words WHERE id=?", (row["word_id"],)).fetchone():
                conn.execute("""UPDATE quiz_events SET is_correct=?,prev_is_correct=?,created_at=?
                    WHERE user_id=? AND id=? AND word_id=? AND mode=?""",
                    (row["is_correct"], row.get("prev_is_correct"), row["created_at"],
                     user_id, int(event_id), row["word_id"], row["mode"]))
    else:
        for mode in snapshot.get("modes") or []:
            target = int((snapshot.get("event_counts") or {}).get(mode, 0))
            conn.execute("""DELETE FROM quiz_events WHERE id IN (
                SELECT id FROM quiz_events WHERE user_id=? AND word_id=? AND mode=?
                ORDER BY id DESC LIMIT MAX(0,(SELECT COUNT(*) FROM quiz_events
                WHERE user_id=? AND word_id=? AND mode=?)-?))""",
                (user_id, word_id, mode, user_id, word_id, mode, target))
    return restored


def append_event(conn, user_id, word_id, mode, is_correct, now=None):
    previous = conn.execute("""SELECT is_correct FROM quiz_events
        WHERE user_id=? AND word_id=? AND mode=? ORDER BY id DESC LIMIT 1""",
        (user_id, word_id, mode)).fetchone()
    return conn.execute("""INSERT INTO quiz_events
        (user_id,word_id,mode,is_correct,prev_is_correct,created_at) VALUES (?,?,?,?,?,?)""",
        (user_id, word_id, mode, int(is_correct), previous[0] if previous else None,
         utc_iso(now))).lastrowid


def append_transaction(conn, user_id, action, payload):
    return conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES (?,?,?)",
                        (user_id, action, json.dumps(payload, ensure_ascii=False))).lastrowid
