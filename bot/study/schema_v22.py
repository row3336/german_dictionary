"""Schema22: scalar words.hilfsverb replaces retired perfekt_variants_json.

The step runs inside the dispatcher transaction. Each legacy JSON value is
validated by the isolated schema21 reader and converted to hat/ist/wechsel;
any invalid, ambiguous or unsupported value aborts the whole migration with a
per-ID report. Missing JSON stays NULL (unknown), never 'hat'. Only the new
column changes: IDs, timestamps, examples, progress and history are untouched.
"""

import json
import sqlite3

from ..common.hilfsverb import FIELD, LEGACY_FIELD, VALUES

COLUMN_SQL = ("TEXT DEFAULT NULL CHECK(" + FIELD + " IS NULL OR " + FIELD + " IN ("
              + ",".join("'" + v + "'" for v in VALUES) + "))")


class UnsupportedLegacyPerfekt(RuntimeError):
    """Legacy annotations that cannot be converted without guessing."""

    def __init__(self, cases):
        self.cases = cases
        super().__init__("schema22 cannot convert legacy perfekt_variants_json: "
                         + json.dumps(cases, ensure_ascii=False, sort_keys=True))


def _columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(words)")}


def ensure_schema(conn):
    """Fresh/idempotent DDL: domain CHECK plus verb-only triggers."""
    if FIELD not in _columns(conn):
        conn.execute("ALTER TABLE words ADD COLUMN " + FIELD + " " + COLUMN_SQL)
    for action in ("INSERT", "UPDATE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS words_hilfsverb_type_{action.lower()}
            BEFORE {action} ON words WHEN NEW.{FIELD} IS NOT NULL AND NEW.type!='verb'
            BEGIN SELECT RAISE(ABORT,'hilfsverb requires verb'); END""")


def _legacy_references(conn):
    """Schema objects other than the words table that mention the old column."""
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name!='words' AND sql LIKE ?", ("%" + LEGACY_FIELD + "%",))]


def convert(conn):
    """Return {word_id: value} for every non-NULL legacy value or raise."""
    from ..common import perfekt
    cursor = conn.execute(f"SELECT * FROM words WHERE {LEGACY_FIELD} IS NOT NULL ORDER BY id")
    names = [d[0] for d in cursor.description]
    converted, cases = {}, []
    for raw_row in cursor.fetchall():
        row = dict(zip(names, raw_row))
        try:
            converted[row["id"]] = perfekt.legacy_hilfsverb(row)
        except perfekt.PerfektValidationError as exc:
            cases.append({"id": row["id"], "type": row.get("type"),
                          "infinitive": row.get("infinitive"), "reason": str(exc)})
    if cases:
        raise UnsupportedLegacyPerfekt(cases)
    return converted


def migrate(conn):
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != 21:
        raise RuntimeError("hilfsverb schema22 requires exact schema21")
    if tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2]) < (3, 35):
        raise RuntimeError("schema22 requires SQLite >= 3.35 for DROP COLUMN; got "
                           + sqlite3.sqlite_version)
    report = {"converted": 0, "counts": dict.fromkeys(VALUES, 0), "legacy_column_dropped": False,
              "legacy_variants_dropped": 0, "legacy_variant_examples_dropped": 0}
    legacy = LEGACY_FIELD in _columns(conn)
    converted = {}
    if legacy:
        references = _legacy_references(conn)
        if references:
            raise RuntimeError("schema22: legacy column referenced by " + ", ".join(references))
        converted = convert(conn)
        for (raw,) in conn.execute(f"SELECT {LEGACY_FIELD} FROM words WHERE {LEGACY_FIELD} IS NOT NULL"):
            variants = json.loads(raw)["variants"]
            report["legacy_variants_dropped"] += len(variants)
            report["legacy_variant_examples_dropped"] += sum(len(v.get("examples", [])) for v in variants)
    ensure_schema(conn)
    if conn.execute(f"SELECT 1 FROM words WHERE {FIELD} IS NOT NULL LIMIT 1").fetchone():
        raise RuntimeError("schema22: hilfsverb already populated before conversion")
    for word_id, aux in converted.items():
        # Representation change only: updated_at and every other column stay.
        if conn.execute(f"UPDATE words SET {FIELD}=? WHERE id=? AND {FIELD} IS NULL",
                        (aux, word_id)).rowcount != 1:
            raise RuntimeError("schema22 conversion target changed: " + word_id)
        report["counts"][aux] += 1
    report["converted"] = len(converted)
    if legacy:
        conn.execute("ALTER TABLE words DROP COLUMN " + LEGACY_FIELD)
        report["legacy_column_dropped"] = True
    stored = dict(conn.execute(f"SELECT id,{FIELD} FROM words WHERE {FIELD} IS NOT NULL"))
    if stored != converted:
        raise RuntimeError("schema22 postflight: converted values differ")
    return report
