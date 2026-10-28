"""Owner book structure: split the PONS book into PONS_00..PONS_14, rename GSV.

One explicit, guarded data operation. No schema change, startup hook or reimport:
each of the manifested PONS cards gets exactly one chapter topic (first occurrence
in the source book), the old ``PONS`` filter token expands to the same card set,
and the old registry row is retired only after no word references it. The GSV key
stays; only its description changes. Callers own the transaction and the commit.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re

from .db_safety import SCHEMA_VERSION
from .topics import filter_is_all, filter_tokens, sorted_topics
from .word_filter import validate_book_config

VERSION = 1
OPERATION = "book_topics_owner_structure_v1"
RECEIPT_KEY = "release_book_topics_owner_structure_v1"
# Canonical digest of scripts/pons_chapter_topics_v1.json (reviewed manifest).
MANIFEST_DIGEST = "22e150a3d94803b1cf119d2824eb10af418ea7b22901d44b21a3fc1866850e82"
OLD_KEY = "PONS"
LEVEL = "PONS"
_FILTER_KEYS = ("topic_filter", "book_topic_filter_v2", "filter_revision")
_SOURCE_KEY = re.compile(r"pons2:([0-9]{2}):L[0-9]+(?::[0-9]+)?")
# Tables whose rows the operation may write; every other table must stay identical.
_WRITABLE = {"words", "topics", "user_settings", "app_settings", "sqlite_sequence"}


class TopicStructureError(ValueError):
    """A rejected operation leaves the caller's transaction unchanged."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_manifest(manifest, pinned=MANIFEST_DIGEST):
    """Check shape, one topic per card, source-key chapter and the pinned digest."""
    fields = {"version", "operation", "assignment_rule", "provenance", "old_registry",
              "new_registry", "gsv", "counts", "cards"}
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise TopicStructureError("invalid manifest shape")
    if (manifest["version"] != VERSION or manifest["operation"] != OPERATION
            or manifest["assignment_rule"] != "first_occurrence_source_key"):
        raise TopicStructureError("unsupported manifest")
    if manifest["old_registry"] != {"name": OLD_KEY, "description": "PONS_B1", "level": LEVEL}:
        raise TopicStructureError("invalid old registry row")
    names = [f"PONS_{i:02d}" for i in range(15)]
    registry = manifest["new_registry"]
    if (not isinstance(registry, list) or [r.get("name") for r in registry] != names
            or any(set(r) != {"name", "description", "level"} or r["level"] != LEVEL
                   or not isinstance(r["description"], str) or not r["description"].strip()
                   for r in registry)):
        raise TopicStructureError("invalid new registry rows")
    gsv = manifest["gsv"]
    if (not isinstance(gsv, dict) or set(gsv) != {"name", "level", "old_description", "new_description"}
            or gsv["name"] != "GSV" or gsv["level"] != "B1"
            or not all(isinstance(gsv[k], str) and gsv[k] for k in ("old_description", "new_description"))):
        raise TopicStructureError("invalid GSV row")
    seen, counts = set(), {}
    for card in manifest["cards"]:
        if (not isinstance(card, dict) or set(card) != {"id", "type", "german", "source_key", "topic"}
                or not all(isinstance(card[k], str) and card[k] for k in card)):
            raise TopicStructureError("invalid card entry")
        match = _SOURCE_KEY.fullmatch(card["source_key"])
        if card["id"] in seen or not match or card["topic"] != "PONS_" + match[1]:
            raise TopicStructureError("duplicate card or topic outside first-occurrence chapter")
        seen.add(card["id"])
        counts[card["topic"]] = counts.get(card["topic"], 0) + 1
    if not seen or manifest["counts"] != counts or set(counts) - set(names):
        raise TopicStructureError("manifest counts mismatch")
    if pinned is not None and digest(manifest) != pinned:
        raise TopicStructureError("manifest digest mismatch")
    return manifest


def load_manifest(text, pinned=MANIFEST_DIGEST):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TopicStructureError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, TypeError) as exc:
        raise TopicStructureError("invalid manifest JSON") from exc
    return validate_manifest(value, pinned)


@contextmanager
def _savepoint(conn):
    conn.execute("SAVEPOINT book_topic_structure")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO book_topic_structure")
        raise
    finally:
        conn.execute("RELEASE book_topic_structure")


def _row(conn, name):
    row = conn.execute("SELECT name,description,level FROM topics WHERE name=?", (name,)).fetchone()
    return None if row is None else {"name": row[0], "description": row[1], "level": row[2]}


def _receipt(conn, manifest):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (RECEIPT_KEY,)).fetchone()
    if row is None:
        return None
    try:
        receipt = json.loads(row[0])
    except ValueError:
        receipt = None
    if (not isinstance(receipt, dict) or receipt.get("operation") != OPERATION
            or receipt.get("state") != "applied" or receipt.get("manifest_digest") != digest(manifest)
            or receipt.get("receipt_sha256") != digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})):
        raise TopicStructureError("receipt conflicts with manifest")
    return receipt


def expand_legacy(raw, new_keys):
    """Replace only the exact old token; 'all', empty and absent stay verbatim."""
    if raw is None or filter_is_all(raw) or not str(raw).strip():
        return raw
    tokens = filter_tokens(raw)
    if OLD_KEY not in tokens:
        return raw
    return "+".join(sorted_topics({t for t in tokens if t != OLD_KEY} | set(new_keys)))


def expand_v2(config, new_keys):
    """Only an explicit PONS-branch list holding the old key changes (active or not)."""
    after = deepcopy(config)
    branch = after["topics_by_level"].get(LEVEL)
    if isinstance(branch, list) and OLD_KEY in branch:
        after["topics_by_level"][LEVEL] = sorted_topics({t for t in branch if t != OLD_KEY} | set(new_keys))
    return after


def _revision(raw):
    if raw is None:
        return 0
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
        raise TopicStructureError("invalid filter revision")
    return int(raw)


def _dump_v2(config):
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def filter_plan(conn, new_keys):
    """Changes for the app default and every user filter; malformed input aborts."""
    changes = []
    app = conn.execute("SELECT value FROM app_settings WHERE key='topic_filter'").fetchone()
    if app is not None and expand_legacy(app[0], new_keys) != app[0]:
        changes.append({"table": "app_settings", "user_id": None, "key": "topic_filter",
                        "before": app[0], "after": expand_legacy(app[0], new_keys)})
    marks = ",".join("?" for _ in _FILTER_KEYS)
    users = [r[0] for r in conn.execute(
        f"SELECT DISTINCT user_id FROM user_settings WHERE key IN ({marks}) ORDER BY user_id", _FILTER_KEYS)]
    for uid in users:
        settings = dict(conn.execute(
            f"SELECT key,value FROM user_settings WHERE user_id=? AND key IN ({marks})", (uid, *_FILTER_KEYS)))
        revision = _revision(settings.get("filter_revision"))
        after = {}
        legacy = expand_legacy(settings.get("topic_filter"), new_keys)
        if legacy != settings.get("topic_filter"):
            after["topic_filter"] = legacy
        if "book_topic_filter_v2" in settings:
            try:
                config = json.loads(settings["book_topic_filter_v2"])
                validate_book_config(config)
            except (ValueError, TypeError, KeyError) as exc:
                raise TopicStructureError(f"malformed book filter for user {uid}") from exc
            if config["revision"] != revision:
                raise TopicStructureError(f"v2/common filter revision mismatch for user {uid}")
            expanded = expand_v2(config, new_keys)
            if expanded != config or after:
                expanded["revision"] = revision + 1
                after["book_topic_filter_v2"] = _dump_v2(expanded)
        if not after:
            continue
        after["filter_revision"] = str(revision + 1)
        for key in sorted(after):
            changes.append({"table": "user_settings", "user_id": uid, "key": key,
                            "before": settings.get(key), "after": after[key]})
    return changes


def _jobs_idle(conn):
    if conn.execute("SELECT 1 FROM vf_add_jobs WHERE status NOT IN "
                    "('completed','done','failed','cancelled','canceled') LIMIT 1").fetchone():
        raise TopicStructureError("live dictionary job requires separate review")
    if conn.execute("SELECT 1 FROM vf_add_items WHERE status IN "
                    "('pending','running','queued','processing') LIMIT 1").fetchone():
        raise TopicStructureError("pending dictionary item requires separate review")


def _schema(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise TopicStructureError(f"book topic structure requires schema {SCHEMA_VERSION}")


def _new_names(manifest):
    return [r["name"] for r in manifest["new_registry"]]


def _cards_at(conn, topics):
    marks = ",".join("?" for _ in topics)
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        f"SELECT id,type,german,topic FROM words WHERE topic IN ({marks})", list(topics))}


def _check_cards(conn, manifest, topics, expected_topic):
    actual = _cards_at(conn, topics)
    if set(actual) != {c["id"] for c in manifest["cards"]}:
        raise TopicStructureError("unmanifested or missing PONS card")
    for card in manifest["cards"]:
        if actual[card["id"]] != (card["type"], card["german"], expected_topic(card)):
            raise TopicStructureError("PONS card identity changed")


def plan(conn, manifest):
    """Read-only preflight; 'already_applied' is a replay with zero operations."""
    validate_manifest(manifest, None)
    with _savepoint(conn):
        _schema(conn)
        if _receipt(conn, manifest) is not None:
            return {"state": "already_applied", "operations": 0, "manifest_digest": digest(manifest)}
        names, gsv = _new_names(manifest), manifest["gsv"]
        if _row(conn, OLD_KEY) != manifest["old_registry"]:
            raise TopicStructureError("old PONS registry row changed")
        if _row(conn, "GSV") != {"name": "GSV", "description": gsv["old_description"], "level": gsv["level"]}:
            raise TopicStructureError("GSV registry row changed")
        if any(_row(conn, name) is not None for name in names) or _cards_at(conn, names):
            raise TopicStructureError("target PONS topic already exists")
        _jobs_idle(conn)
        _check_cards(conn, manifest, [OLD_KEY], lambda card: OLD_KEY)
        filters = filter_plan(conn, names)
        return {"state": "ready", "manifest_digest": digest(manifest),
                "moves": len(manifest["cards"]), "counts": dict(manifest["counts"]),
                "registry_insert": names, "registry_retire": OLD_KEY,
                "gsv_description": [gsv["old_description"], gsv["new_description"]],
                "filters": filters}


def _protected(conn, changed):
    """Digest of all rows the operation must not alter (words minus topic, user data)."""
    h = hashlib.sha256()
    columns = [r[1] for r in conn.execute("PRAGMA table_info(words)") if r[1] != "topic"]
    for row in conn.execute(f"SELECT {','.join(columns)} FROM words ORDER BY id"):
        h.update(repr(tuple(row)).encode("utf-8"))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if r[0] not in _WRITABLE]
    for table in tables:
        width = len(conn.execute(f'SELECT * FROM "{table}" LIMIT 0').description)
        order = ",".join(str(i) for i in range(1, width + 1))
        h.update(table.encode("utf-8"))
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            h.update(repr(tuple(row)).encode("utf-8"))
    for row in conn.execute("SELECT user_id,key,value FROM user_settings ORDER BY user_id,key"):
        if (row[0], row[1]) not in changed:
            h.update(repr(tuple(row)).encode("utf-8"))
    for row in conn.execute("SELECT name,description,level FROM topics ORDER BY name"):
        if row[0] != OLD_KEY and row[0] != "GSV" and not row[0].startswith("PONS_"):
            h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def _write_setting(conn, change, value, expected):
    if change["table"] == "app_settings":
        cur = conn.execute("UPDATE app_settings SET value=? WHERE key=? AND value=?",
                           (value, change["key"], expected))
    elif expected is None:
        cur = conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)",
                           (change["user_id"], change["key"], value))
    else:
        cur = conn.execute("UPDATE user_settings SET value=? WHERE user_id=? AND key=? AND value=?",
                           (value, change["user_id"], change["key"], expected))
    if cur.rowcount != 1:
        raise TopicStructureError("filter setting changed during operation")


def _counts(conn, names):
    marks = ",".join("?" for _ in names)
    return dict(conn.execute(
        f"SELECT topic,COUNT(*) FROM words WHERE topic IN ({marks}) GROUP BY topic ORDER BY topic", names))


def apply(conn, manifest):
    """Apply inside a caller-owned transaction; any guard failure rolls this back."""
    if not conn.in_transaction:
        raise TopicStructureError("apply requires a caller-owned transaction")
    with _savepoint(conn):
        report = plan(conn, manifest)
        if report["state"] == "already_applied":
            return report
        names, gsv = _new_names(manifest), manifest["gsv"]
        changed = {(c["user_id"], c["key"]) for c in report["filters"] if c["table"] == "user_settings"}
        total = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        protected = _protected(conn, changed)
        conn.executemany("INSERT INTO topics(name,description,level) VALUES(?,?,?)",
                         [(r["name"], r["description"], r["level"]) for r in manifest["new_registry"]])
        for card in manifest["cards"]:
            cur = conn.execute("UPDATE words SET topic=? WHERE id=? AND topic=?",
                               (card["topic"], card["id"], OLD_KEY))
            if cur.rowcount != 1:
                raise TopicStructureError("PONS card move failed")
        cur = conn.execute("UPDATE topics SET description=? WHERE name='GSV' AND description=? AND level=?",
                           (gsv["new_description"], gsv["old_description"], gsv["level"]))
        if cur.rowcount != 1:
            raise TopicStructureError("GSV rename failed")
        for change in report["filters"]:
            _write_setting(conn, change, change["after"], change["before"])
        if conn.execute("SELECT 1 FROM words WHERE topic=? LIMIT 1", (OLD_KEY,)).fetchone():
            raise TopicStructureError("old PONS topic still referenced")
        old = manifest["old_registry"]
        cur = conn.execute("DELETE FROM topics WHERE name=? AND description=? AND level=?",
                           (old["name"], old["description"], old["level"]))
        if cur.rowcount != 1:
            raise TopicStructureError("old PONS registry retirement failed")
        if (conn.execute("SELECT COUNT(*) FROM words").fetchone()[0] != total
                or _counts(conn, names) != manifest["counts"]
                or _protected(conn, changed) != protected):
            raise TopicStructureError("postflight mismatch")
        receipt = {"version": VERSION, "operation": OPERATION, "state": "applied",
                   "manifest_digest": digest(manifest), "moves": len(manifest["cards"]),
                   "filter_changes": report["filters"]}
        receipt["receipt_sha256"] = digest(receipt)
        conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (RECEIPT_KEY, canonical_json(receipt)))
        return {**report, "state": "applied", "operations": len(manifest["cards"]) + len(names) + 2
                + len(report["filters"])}


def rollback(conn, manifest):
    """Reverse an applied receipt; filters get their old content at a new revision."""
    if not conn.in_transaction:
        raise TopicStructureError("rollback requires a caller-owned transaction")
    validate_manifest(manifest, None)
    with _savepoint(conn):
        _schema(conn)
        receipt = _receipt(conn, manifest)
        if receipt is None:
            raise TopicStructureError("no applied receipt for this manifest")
        names, gsv, old = _new_names(manifest), manifest["gsv"], manifest["old_registry"]
        if (_row(conn, OLD_KEY) is not None or conn.execute(
                "SELECT 1 FROM words WHERE topic=? LIMIT 1", (OLD_KEY,)).fetchone()
                or [_row(conn, n) for n in names] != manifest["new_registry"]
                or _row(conn, "GSV") != {"name": "GSV", "description": gsv["new_description"], "level": gsv["level"]}):
            raise TopicStructureError("registry changed after apply")
        _jobs_idle(conn)
        _check_cards(conn, manifest, names, lambda card: card["topic"])
        filters = receipt["filter_changes"]
        for change in filters:
            if change["table"] == "app_settings":
                row = conn.execute("SELECT value FROM app_settings WHERE key=?", (change["key"],)).fetchone()
            else:
                row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?",
                                   (change["user_id"], change["key"])).fetchone()
            if row is None or row[0] != change["after"]:
                raise TopicStructureError("filter changed after apply; restore the backup instead")
        changed = {(c["user_id"], c["key"]) for c in filters if c["table"] == "user_settings"}
        total = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        protected = _protected(conn, changed)
        conn.execute("INSERT INTO topics(name,description,level) VALUES(?,?,?)",
                     (old["name"], old["description"], old["level"]))
        for card in manifest["cards"]:
            cur = conn.execute("UPDATE words SET topic=? WHERE id=? AND topic=?", (OLD_KEY, card["id"], card["topic"]))
            if cur.rowcount != 1:
                raise TopicStructureError("PONS card restore failed")
        conn.executemany("DELETE FROM topics WHERE name=?", [(n,) for n in names])
        conn.execute("UPDATE topics SET description=? WHERE name='GSV'", (gsv["old_description"],))
        by_user = {}
        for change in filters:
            if change["table"] == "app_settings":
                _write_setting(conn, change, change["before"], change["after"])
            else:
                by_user.setdefault(change["user_id"], {})[change["key"]] = change
        for uid, entries in by_user.items():
            # Old callbacks/tokens stay invalid: the restored set gets a newer revision.
            revision = int(entries["filter_revision"]["after"]) + 1
            if "topic_filter" in entries:
                _write_setting(conn, entries["topic_filter"], entries["topic_filter"]["before"],
                               entries["topic_filter"]["after"])
            if "book_topic_filter_v2" in entries:
                restored = json.loads(entries["book_topic_filter_v2"]["before"])
                restored["revision"] = revision
                _write_setting(conn, entries["book_topic_filter_v2"], _dump_v2(restored),
                               entries["book_topic_filter_v2"]["after"])
            _write_setting(conn, entries["filter_revision"], str(revision), entries["filter_revision"]["after"])
        conn.execute("DELETE FROM app_settings WHERE key=?", (RECEIPT_KEY,))
        if (conn.execute("SELECT COUNT(*) FROM words").fetchone()[0] != total
                or _counts(conn, [OLD_KEY]) != {OLD_KEY: len(manifest["cards"])}
                or _protected(conn, changed) != protected):
            raise TopicStructureError("rollback postflight mismatch")
        return {"state": "rolled_back", "manifest_digest": digest(manifest),
                "moves": len(manifest["cards"]), "filter_users": sorted(by_user),
                "app_default_restored": any(c["table"] == "app_settings" for c in filters)}
