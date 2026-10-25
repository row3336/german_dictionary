"""Schema21 adds nullable linked Perfekt metadata without dictionary backfill."""


def ensure_schema(conn):
    if "perfekt_variants_json" not in {r[1] for r in conn.execute("PRAGMA table_info(words)")}:
        conn.execute("ALTER TABLE words ADD COLUMN perfekt_variants_json TEXT DEFAULT NULL")


def migrate(conn):
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != 20:
        raise RuntimeError("Perfekt schema21 requires exact schema20")
    ensure_schema(conn)
    return {"annotated": 0, "backfilled": 0, "reset": 0}
