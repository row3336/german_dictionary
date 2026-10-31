"""The Learning-only v18 schema shared by fresh setup and explicit migration."""

import uuid


def create_learning_schema_v18(conn):
    """Add Learning state without changing existing steps, deadlines, or events."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(learning_items)")}
    if "recovery_remaining" not in columns:
        conn.execute("ALTER TABLE learning_items ADD COLUMN recovery_remaining INTEGER NOT NULL DEFAULT 0 CHECK(recovery_remaining BETWEEN 0 AND 3)")
    if "first_due_pending" not in columns:
        conn.execute("ALTER TABLE learning_items ADD COLUMN first_due_pending INTEGER NOT NULL DEFAULT 0 CHECK(first_due_pending IN (0, 1))")
        conn.execute("""UPDATE learning_items SET first_due_pending=1
            WHERE status='active' AND preview_done=0""")
    if "cycle_token" not in columns:
        conn.execute("ALTER TABLE learning_items ADD COLUMN cycle_token TEXT NOT NULL DEFAULT ''")
    for row in conn.execute("SELECT user_id, word_id FROM learning_items WHERE cycle_token='' AND word_id IS NOT NULL"):
        conn.execute(
            "UPDATE learning_items SET cycle_token=? WHERE user_id=? AND word_id=?",
            (uuid.uuid4().hex, row[0], row[1]),
        )
    for statement in (
        """CREATE TABLE IF NOT EXISTS study_actions (
            user_id INTEGER NOT NULL,
            action_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            flow_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            word_id TEXT,
            state TEXT NOT NULL CHECK(state IN ('applied','undone')),
            receipt TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, action_key)
        )""",
        """CREATE TABLE IF NOT EXISTS study_deliveries (
            user_id INTEGER NOT NULL,
            transport_update_id INTEGER NOT NULL,
            action_key TEXT,
            kind TEXT NOT NULL,
            flow_id TEXT NOT NULL,
            state TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, transport_update_id)
        )""",
        """CREATE TABLE IF NOT EXISTS learning_display_state (
            user_id INTEGER PRIMARY KEY,
            flow_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('preview','question','waiting','completed')),
            word_id TEXT,
            action_key TEXT,
            source_action_key TEXT,
            feedback_text TEXT NOT NULL DEFAULT '',
            send_state TEXT NOT NULL CHECK(send_state IN ('planned','uncertain','confirmed','failed')),
            telegram_message_id INTEGER,
            cycle_token TEXT,
            suspended_for_clarify INTEGER NOT NULL DEFAULT 0 CHECK(suspended_for_clarify IN (0,1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_study_actions_user_state ON study_actions(user_id, state)",
        "CREATE INDEX IF NOT EXISTS idx_study_deliveries_action ON study_deliveries(user_id, action_key)",
    ):
        conn.execute(statement)
