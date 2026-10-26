"""Accepted degree pairs for adjectives with more than one written form."""

TABLE = "adjective_degree_variants"
CREATE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS adjective_degree_variants (
    word_id TEXT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    comparative TEXT NOT NULL DEFAULT '',
    superlative TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(word_id, comparative, superlative),
    CHECK(comparative <> '' OR superlative <> '')
) WITHOUT ROWID"""


def attach_degree_pairs(conn, entry):
    """Add the selected answer contract; old schema21 DBs use scalar fields."""
    if entry.get("type") != "adjektiv" or not entry.get("id"):
        return entry
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone():
        return entry
    rows = conn.execute(
        "SELECT comparative, superlative FROM adjective_degree_variants "
        "WHERE word_id=? ORDER BY comparative, superlative", (entry["id"],)
    ).fetchall()
    if rows:
        entry["adjective_degree_pairs"] = [
            {"comparative": row[0], "superlative": row[1]} for row in rows
        ]
    return entry
