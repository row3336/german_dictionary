"""Shared fresh/migration DDL. Never commits the caller's transaction."""

from datetime import datetime, timezone


def ensure_schema(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(learning_items)")}
    if "recovery_remaining" not in columns:
        conn.execute("""ALTER TABLE learning_items ADD COLUMN recovery_remaining
                     INTEGER NOT NULL DEFAULT 0 CHECK(recovery_remaining BETWEEN 0 AND 3)""")
    if "first_due_pending" not in columns:
        conn.execute("""ALTER TABLE learning_items ADD COLUMN first_due_pending
                     INTEGER NOT NULL DEFAULT 0 CHECK(first_due_pending IN (0,1))""")
        conn.execute("""UPDATE learning_items SET first_due_pending=1
                     WHERE status='active' AND preview_done=0""")
    conn.execute("""CREATE TABLE IF NOT EXISTS study_actions (
        user_id INTEGER NOT NULL, action_key TEXT NOT NULL, flow_id TEXT NOT NULL,
        kind TEXT NOT NULL, word_id TEXT, mode TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('applied','undone')),
        receipt_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id,action_key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS study_deliveries (
        user_id INTEGER NOT NULL, transport_update_id INTEGER NOT NULL,
        flow_id TEXT NOT NULL, action_kind TEXT NOT NULL, action_key TEXT,
        state TEXT NOT NULL CHECK(state IN ('processing','committed')),
        result_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id,transport_update_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_study_pending_deliveries
                 ON study_deliveries(user_id,state,action_kind)""")
    from ..conjugation.schema import ensure_schema as ensure_conjugation_schema
    ensure_conjugation_schema(conn)


def migrate(conn, now_iso=None):
    """Add v18 state without changing any historical schedule or event."""
    ensure_schema(conn)
    from ..conjugation.schema import seed_admissions
    before = conn.execute("SELECT COUNT(*) FROM conjugation_admissions").fetchone()[0]
    seed_admissions(conn, now_iso or datetime.now(timezone.utc).isoformat())
    after = conn.execute("SELECT COUNT(*) FROM conjugation_admissions").fetchone()[0]
    # A viewed legacy row may belong to a partially previewed selection: preserve
    # its historical due and expose it for review instead of guessing readiness.
    diagnostic = [dict(zip(("user_id", "word_id", "batch_id", "preview_done", "step_index", "next_due"), row))
                  for row in conn.execute("""SELECT user_id,word_id,batch_id,preview_done,step_index,next_due
                      FROM learning_items WHERE status='active' ORDER BY user_id,word_id""")]
    return {"seeded_admissions": after - before, "legacy_active_learning": diagnostic}
