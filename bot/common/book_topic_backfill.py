"""Exact topic retirement and lossless revisioned filter transforms."""
from copy import deepcopy
import json
from .topics import sorted_topics, filter_is_all, filter_tokens
from .word_filter import validate_book_config

MAPPING = {"03": "2", "011": "12", "012": "13"}

def transform_legacy(raw, mapping=MAPPING):
    if raw is None or filter_is_all(raw) or not str(raw).strip():
        return raw
    tokens = filter_tokens(raw)
    if not any(t in mapping for t in tokens):
        return raw
    return "+".join(sorted_topics(set(mapping.get(t, t) for t in tokens)))

def filter_plan(conn, mapping=MAPPING):
    result = []
    app = conn.execute("SELECT value FROM app_settings WHERE key='topic_filter'").fetchone()
    if app and transform_legacy(app[0], mapping) != app[0]:
        result.append(dict(table="app_settings", user_id=None, key="topic_filter", before=app[0], after=transform_legacy(app[0], mapping)))
    users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM user_settings WHERE key IN ('topic_filter','book_topic_filter_v2','filter_revision') ORDER BY user_id")]
    for uid in users:
        settings = dict(conn.execute("SELECT key,value FROM user_settings WHERE user_id=? AND key IN ('topic_filter','book_topic_filter_v2','filter_revision')", (uid,)))
        oldrev = settings.get("filter_revision")
        try:
            if oldrev is not None and (not isinstance(oldrev, str) or not oldrev.isdecimal()):
                raise ValueError()
            revision = int(oldrev or 0)
        except (TypeError, ValueError):
            raise ValueError("invalid filter revision") from None
        changes = {}
        raw = settings.get("topic_filter")
        new = transform_legacy(raw, mapping)
        if new != raw:
            changes["topic_filter"] = new
        config = None
        if "book_topic_filter_v2" in settings:
            config = json.loads(settings["book_topic_filter_v2"])
            validate_book_config(config)
            if config["revision"] != revision:
                raise ValueError("v2/common filter revision mismatch")
            after = deepcopy(config)
            a1 = after["topics_by_level"].get("A1")
            if isinstance(a1, list) and any(t in mapping for t in a1):
                after["topics_by_level"]["A1"] = sorted_topics(set(mapping.get(t, t) for t in a1))
            if after != config:
                changes["book_topic_filter_v2"] = after
        if not changes:
            continue
        if config is not None:
            after = changes.get("book_topic_filter_v2", deepcopy(config))
            after["revision"] = revision + 1
            changes["book_topic_filter_v2"] = json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        changes["filter_revision"] = str(revision + 1)
        for key, value in sorted(changes.items()):
            result.append(dict(table="user_settings", user_id=uid, key=key, before=settings.get(key), after=value))
    return result

def preflight(conn, manifest):
    mapping = manifest.get("merge_mapping_exact", {})
    if any(k == v for k, v in mapping.items()):
        raise ValueError("invalid topic mapping")
    if conn.execute("SELECT 1 FROM vf_add_jobs WHERE status NOT IN ('completed','done','failed','cancelled','canceled') LIMIT 1").fetchone():
        raise ValueError("live dictionary job requires separate review")
    if conn.execute("SELECT 1 FROM vf_add_items WHERE status IN ('pending','running','queued','processing') LIMIT 1").fetchone():
        raise ValueError("pending dictionary item requires separate review")
    moves = manifest.get("word_topic_operations", [])
    seen = set()
    for move in moves:
        wid = move["word_id"]
        if wid in seen or set(move["expected_old"]) != {"topic"} or set(move["new"]) != {"topic"}:
            raise ValueError("duplicate or broad topic move")
        seen.add(wid)
        old, new = move["expected_old"]["topic"], move["new"]["topic"]
        row = conn.execute("SELECT topic FROM words WHERE id=?", (wid,)).fetchone()
        if old not in mapping or mapping[old] != new or not row or row[0] != old:
            raise ValueError("topic expected-old mismatch")
    actual = {r[0] for old in mapping for r in conn.execute("SELECT id FROM words WHERE topic=?", (old,))}
    if actual != seen:
        raise ValueError("unmanifested retired-topic word reference")
    for expected in manifest.get("target_metadata_guards", []):
        row = conn.execute("SELECT name,description,level FROM topics WHERE name=?", (expected["name"],)).fetchone()
        if not row or dict(row) != expected:
            raise ValueError("topic target metadata guard changed")
    for op in manifest.get("topic_registry_operations", []):
        expected = op["expected_old"]
        key = expected["name"] if expected else op["key"]
        row = conn.execute("SELECT name,description,level FROM topics WHERE name=?", (key,)).fetchone()
        old = dict(row) if row else None
        if op["operation"] == "retire_exact_registry_row":
            if old != expected or key not in mapping:
                raise ValueError("retired registry guard changed")
        elif op["operation"] == "insert_if_absent_or_exact_match":
            if old is not None and old != op["new"]:
                raise ValueError("new registry conflict")
        else:
            raise ValueError("unsupported registry operation")
    return dict(filters=filter_plan(conn, mapping), moves=moves, registry=manifest.get("topic_registry_operations", []))

def apply_plan(conn, plan):
    """Caller has validated all guards under its transaction/savepoint."""
    count = 0
    for change in plan["filters"]:
        if change["table"] == "app_settings":
            conn.execute("UPDATE app_settings SET value=? WHERE key=?", (change["after"], change["key"]))
        elif change["before"] is None:
            conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)", (change["user_id"], change["key"], change["after"]))
        else:
            conn.execute("UPDATE user_settings SET value=? WHERE user_id=? AND key=?", (change["after"], change["user_id"], change["key"]))
        count += 1
    for move in plan["moves"]:
        conn.execute("UPDATE words SET topic=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (move["new"]["topic"], move["word_id"]))
        count += 1
    for op in plan["registry"]:
        if op["operation"] == "retire_exact_registry_row":
            key = op["expected_old"]["name"]
            if conn.execute("SELECT 1 FROM words WHERE topic=?", (key,)).fetchone():
                raise ValueError("retired topic still referenced")
            conn.execute("DELETE FROM topics WHERE name=?", (key,))
            count += 1
        elif not conn.execute("SELECT 1 FROM topics WHERE name=?", (op["key"],)).fetchone():
            row = op["new"]
            conn.execute("INSERT INTO topics(name,description,level) VALUES(?,?,?)", (row["name"], row["description"], row["level"]))
            count += 1
    return count
