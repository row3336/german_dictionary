"""Explicit, one-time release operation for the frozen approved-user cohort.

Only the five filter settings and one versioned app_settings receipt are writable.
No startup hook, schema migration, configuration loading or caller commit occurs.
"""

from contextlib import contextmanager
import hashlib
import json
import re


VERSION = 1
SCHEMA_VERSION = 20
OPERATION = "existing_users_a1_all_v1"
RECEIPT_KEY = "release_existing_users_a1_all_v1"
FILTER_KEYS = ("filter_mode", "active_book_level", "topic_filter",
               "book_topic_filter_v2", "filter_revision")
_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


class ExistingUserFilterError(ValueError):
    """A rejected release operation leaves its caller's transaction intact."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def load_json(raw):
    """Reject ambiguous duplicate keys and non-JSON numeric constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ExistingUserFilterError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ExistingUserFilterError("invalid JSON constant")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError) as exc:
        raise ExistingUserFilterError("invalid JSON document") from exc


def _digest(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _revision(before):
    entry = before["filter_revision"]
    if not entry["present"]:
        return 0
    raw = entry["value"]
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
        raise ExistingUserFilterError("invalid filter revision")
    try:
        return int(raw)
    except ValueError as exc:
        raise ExistingUserFilterError("invalid filter revision") from exc


def _validate_ids(ids):
    if (not isinstance(ids, list) or not ids
            or any(type(uid) is not int or uid <= 0 for uid in ids)
            or ids != sorted(set(ids))):
        raise ExistingUserFilterError("cohort must be sorted unique positive integer IDs")


def validate_manifest(manifest):
    """Validate shape and digest, including the exact absence/value before image."""
    fields = {"version", "operation", "schema_version", "receipt_key", "targets", "digest"}
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise ExistingUserFilterError("invalid manifest shape")
    if (type(manifest["version"]) is not int or manifest["version"] != VERSION
            or manifest["operation"] != OPERATION
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != SCHEMA_VERSION
            or manifest["receipt_key"] != RECEIPT_KEY
            or not isinstance(manifest["targets"], list)):
        raise ExistingUserFilterError("unsupported manifest")
    ids = []
    for target in manifest["targets"]:
        if (not isinstance(target, dict)
                or set(target) != {"user_id", "status", "before", "revision"}
                or target["status"] != "approved"
                or not isinstance(target["before"], dict)
                or set(target["before"]) != set(FILTER_KEYS)):
            raise ExistingUserFilterError("invalid target shape")
        ids.append(target["user_id"])
        for entry in target["before"].values():
            if (not isinstance(entry, dict) or set(entry) != {"present", "value"}
                    or type(entry["present"]) is not bool
                    or (entry["value"] is not None and not isinstance(entry["value"], str))
                    or (not entry["present"] and entry["value"] is not None)):
                raise ExistingUserFilterError("invalid expected setting")
        if type(target["revision"]) is not int or target["revision"] != _revision(target["before"]):
            raise ExistingUserFilterError("invalid expected revision")
    _validate_ids(ids)
    payload = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest["digest"] != _digest(payload):
        raise ExistingUserFilterError("manifest digest mismatch")
    return manifest


def _receipt(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (RECEIPT_KEY,)).fetchone()
    if row is None:
        return None
    receipt = load_json(row[0])
    fields = {"version", "operation", "schema_version", "digest", "target_ids", "state"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt["version"]) is not int or receipt["version"] != VERSION
            or receipt["operation"] != OPERATION
            or type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != SCHEMA_VERSION
            or receipt["state"] != "applied"
            or not isinstance(receipt["digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt["digest"]) is None):
        raise ExistingUserFilterError("invalid release receipt")
    _validate_ids(receipt["target_ids"])
    return receipt


def _schema(conn):
    # The release envelope/receipt stays v20 so previously prepared approvals
    # remain valid after the unrelated words-only Perfekt/hilfsverb migrations.
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (20, 21, 22):
        raise ExistingUserFilterError("existing-user filter release requires schema 20, 21 or 22")


def _a1_ready(conn):
    if not conn.execute("""SELECT 1 FROM topics t JOIN words w ON w.topic=t.name
                           WHERE t.level='A1' AND TRIM(t.name)<>'' LIMIT 1""").fetchone():
        raise ExistingUserFilterError("A1 topic registry is not ready")


def _before(conn, user_id):
    marks = ",".join("?" for _ in FILTER_KEYS)
    values = dict(conn.execute(
        f"SELECT key,value FROM user_settings WHERE user_id=? AND key IN ({marks})",
        (user_id, *FILTER_KEYS)).fetchall())
    return {key: {"present": key in values, "value": values.get(key)} for key in FILTER_KEYS}


@contextmanager
def _snapshot(conn):
    conn.execute("SAVEPOINT existing_user_filters")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO existing_user_filters")
        raise
    finally:
        conn.execute("RELEASE existing_user_filters")


def prepare(conn):
    """Freeze all currently approved users; read only, including on legacy filters."""
    with _snapshot(conn):
        _schema(conn)
        if _receipt(conn) is not None:
            raise ExistingUserFilterError("release already applied; do not prepare another cohort")
        _a1_ready(conn)
        targets = []
        for row in conn.execute("SELECT user_id,status FROM users WHERE status='approved' ORDER BY user_id"):
            before = _before(conn, row[0])
            targets.append({"user_id": row[0], "status": row[1],
                            "before": before, "revision": _revision(before)})
        payload = {"version": VERSION, "operation": OPERATION,
                   "schema_version": SCHEMA_VERSION, "receipt_key": RECEIPT_KEY,
                   "targets": targets}
        return validate_manifest({**payload, "digest": _digest(payload)})


def check(conn, manifest):
    """Check the receipt before comparing old target preferences or statuses."""
    validate_manifest(manifest)
    with _snapshot(conn):
        _schema(conn)
        receipt = _receipt(conn)
        ids = [target["user_id"] for target in manifest["targets"]]
        report = {"state": "ready", "targets": len(ids), "digest": manifest["digest"]}
        if receipt is not None:
            if receipt["digest"] != manifest["digest"] or receipt["target_ids"] != ids:
                raise ExistingUserFilterError("release receipt conflicts with manifest")
            return {**report, "state": "already_applied"}
        _a1_ready(conn)
        for target in manifest["targets"]:
            row = conn.execute("SELECT status FROM users WHERE user_id=?", (target["user_id"],)).fetchone()
            if row is None or row[0] != target["status"]:
                raise ExistingUserFilterError("target status changed")
            if _before(conn, target["user_id"]) != target["before"]:
                raise ExistingUserFilterError("target filter settings changed")
        return report


def _inactive_choices(raw):
    """Retain only safe inactive branches without importing bot runtime/config.

    This conservative check mirrors the v2 document boundary, not its resolver.
    Malformed documents are replaced in full. Existing topic order is preserved.
    """
    try:
        value = load_json(raw)
        if (not isinstance(value, dict)
                or set(value) != {"version", "selected_levels", "topics_by_level", "browsing_level", "revision"}
                or type(value["version"]) is not int or value["version"] != 2
                or type(value["revision"]) is not int or value["revision"] < 0
                or value["browsing_level"] not in _LEVELS
                or not isinstance(value["selected_levels"], list)
                or any(level not in _LEVELS for level in value["selected_levels"])
                or not isinstance(value["topics_by_level"], dict)):
            return {}
        choices = value["topics_by_level"]
        if any(level not in choices for level in value["selected_levels"]):
            return {}
        for level, topics in choices.items():
            if level not in _LEVELS or (topics != "all" and not (
                    isinstance(topics, list) and topics and all(
                        isinstance(name, str) and name and name.strip() == name for name in topics))):
                return {}
        return {level: topics for level, topics in choices.items() if level != "A1"}
    except (ValueError, TypeError):
        return {}


def apply(conn, manifest):
    """Apply in a caller-owned transaction; rollback this operation on any error.

    The release CLI owns BEGIN IMMEDIATE and the verified pre-DML backup. Callers
    using this low-level API own that reservation/backup and the final commit.
    """
    if not conn.in_transaction:
        raise ExistingUserFilterError("apply requires a caller-owned transaction")
    with _snapshot(conn):
        report = check(conn, manifest)
        if report["state"] == "already_applied":
            return report
        for target in manifest["targets"]:
            revision = target["revision"] + 1
            choices = _inactive_choices(target["before"]["book_topic_filter_v2"]["value"])
            config = {"version": 2, "selected_levels": ["A1"],
                      "topics_by_level": {**choices, "A1": "all"},
                      "browsing_level": "A1", "revision": revision}
            values = ("topics", "A1", "all", canonical_json(config), str(revision))
            conn.executemany("""INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)
                ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value""",
                [(target["user_id"], key, value) for key, value in zip(FILTER_KEYS, values)])
        receipt = {"version": VERSION, "operation": OPERATION,
                   "schema_version": SCHEMA_VERSION, "digest": manifest["digest"],
                   "target_ids": [target["user_id"] for target in manifest["targets"]],
                   "state": "applied"}
        conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?)",
                     (RECEIPT_KEY, canonical_json(receipt)))
        return {**report, "state": "applied"}
