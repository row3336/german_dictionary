"""Accepted degree pairs for adjectives with more than one written form."""

TABLE = "adjective_degree_variants"
CREATE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS adjective_degree_variants (
    word_id TEXT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    comparative TEXT NOT NULL DEFAULT '',
    superlative TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(word_id, comparative, superlative),
    CHECK(comparative <> '' OR superlative <> '')
) WITHOUT ROWID"""


def table_state(conn):
    """False when absent; True for the exact contract; otherwise refuse."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if row is None:
        return False
    if row[0] != CREATE_TABLE_SQL.replace("IF NOT EXISTS ", "", 1):
        raise ValueError("incompatible adjective degree table DDL")
    columns = conn.execute("PRAGMA table_info(" + TABLE + ")").fetchall()
    if ([(r[1], r[2].upper(), r[3], r[5]) for r in columns]
            != [("word_id", "TEXT", 1, 1), ("comparative", "TEXT", 1, 2), ("superlative", "TEXT", 1, 3)]):
        raise ValueError("incompatible adjective degree table")
    foreign = conn.execute("PRAGMA foreign_key_list(" + TABLE + ")").fetchall()
    if len(foreign) != 1 or tuple(foreign[0][2:5]) != ("words", "word_id", "id"):
        raise ValueError("incompatible adjective degree foreign key")
    return True


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
