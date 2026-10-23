"""Schema20 adds an explicit Add queue without touching existing study state."""


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS learning_requests (
        user_id INTEGER NOT NULL,
        word_id TEXT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('pending','consumed','cancelled')),
        request_id TEXT NOT NULL, queued_at TEXT NOT NULL, consumed_at TEXT,
        word_generation INTEGER NOT NULL DEFAULT 0 CHECK(word_generation>=0),
        learning_lifecycle INTEGER NOT NULL DEFAULT 0 CHECK(learning_lifecycle>=0),
        PRIMARY KEY(user_id,word_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS learning_requests_fifo
        ON learning_requests(user_id,status,queued_at,word_id)""")


def migrate(conn):
    ensure_schema(conn)
    return {"enqueued": 0, "restarted": 0}
