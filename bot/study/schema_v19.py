"""Shared schema19 DDL and legacy pass freeze, without commit or data repair."""

import json

from ..common.verb_construction import canonical_infinitive
from ..conjugation.domain import eligible_word


def _add(conn, table, name, definition):
    if name not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def collision_report(conn):
    seen, issues = {}, []
    for wid, infinitive, case in conn.execute("SELECT id,infinitive,reflexive_case FROM words WHERE type='verb'"):
        canonical = canonical_infinitive(infinitive)
        key = (canonical, case)
        if infinitive != canonical:
            issues.append({"id": wid, "reason": "noncanonical infinitive", "canonical": canonical})
        if key in seen:
            issues.append({"ids": [seen[key], wid], "reason": "duplicate identity"})
        seen[key] = wid
    return issues


def ensure_schema(conn):
    _add(conn, "words", "reflexive_case", "TEXT DEFAULT NULL CHECK(reflexive_case IS NULL OR reflexive_case IN ('none','akkusativ','dativ'))")
    collisions = collision_report(conn)
    if collisions:
        raise ValueError("schema19 identity preflight: " + json.dumps(collisions, ensure_ascii=False))
    for action in ("INSERT", "UPDATE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS words_reflexive_type_{action.lower()}
            BEFORE {action} ON words WHEN NEW.reflexive_case IS NOT NULL AND NEW.type!='verb'
            BEGIN SELECT RAISE(ABORT,'reflexive_case requires verb'); END""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS words_verb_identity
        ON words(infinitive,COALESCE(reflexive_case,'__legacy__')) WHERE type='verb'""")
    conn.execute("CREATE INDEX IF NOT EXISTS words_verb_lookup ON words(infinitive,reflexive_case)")
    _add(conn, "conjugation_passes", "prompt_snapshot_json", "TEXT DEFAULT NULL")
    _add(conn, "conjugation_admissions", "study_generation", "INTEGER NOT NULL DEFAULT 0 CHECK(study_generation>=0)")
    _add(conn, "quiz_events", "study_generation", "INTEGER NOT NULL DEFAULT 0 CHECK(study_generation>=0)")
    conn.execute("""CREATE TABLE IF NOT EXISTS study_word_generations (
        user_id INTEGER NOT NULL, word_id TEXT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL CHECK(generation>=0), PRIMARY KEY(user_id,word_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS study_reset_operations (
        id TEXT PRIMARY KEY,manifest_sha256 TEXT NOT NULL UNIQUE,applied_at TEXT NOT NULL,result_json TEXT NOT NULL)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS quiz_events_generation
        ON quiz_events(user_id,word_id,study_generation,mode,created_at)""")


def migrate(conn):
    ensure_schema(conn)
    diagnostics = []
    for pid, wid in conn.execute("SELECT id,word_id FROM conjugation_passes WHERE status='active' AND prompt_snapshot_json IS NULL").fetchall():
        parent = conn.execute("SELECT infinitive,german,reflexive_case,type,is_separable FROM words WHERE id=?", (wid,)).fetchone()
        # Preserve frozen expected forms; only a currently valid parent can
        # supply the legacy prompt. Never fabricate a prompt for another POS.
        valid_parent = (parent and eligible_word({"type": parent[3], "is_separable": parent[4]})
                        and str(parent[0] or parent[1]).strip())
        if not valid_parent:
            conn.execute("UPDATE conjugation_passes SET status='reset',revision=revision+1 WHERE id=?", (pid,))
            diagnostics.append({"pass_id": pid, "outcome": "invalid_legacy_parent"})
            continue
        snapshot = {"v": 1, "word": parent[0] or parent[1], "reflexive_case": parent[2],
                    "study_generation": 0, "legacy": True}
        conn.execute("UPDATE conjugation_passes SET prompt_snapshot_json=? WHERE id=?", (json.dumps(snapshot), pid))
    return {"legacy_pass_diagnostics": diagnostics}
