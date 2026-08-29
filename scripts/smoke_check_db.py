import argparse
import sqlite3
import sys

from bot.storage import init_db


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return bool(row)


def _get_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def run_smoke(db_path):
    init_db(db_path)
    ok = True
    lines = []
    with sqlite3.connect(db_path) as conn:
        required_tables = [
            "words",
            "progress",
            "quiz_events",
            "learning_items",
            "learning_batches",
            "app_settings",
        ]
        for table in required_tables:
            exists = _table_exists(conn, table)
            lines.append(f"{'OK' if exists else 'FAIL'} table:{table}")
            ok = ok and exists

        words_cols = _get_columns(conn, "words")
        for col in ("topic", "komparativ", "superlativ", "type", "german", "russian"):
            present = col in words_cols
            lines.append(f"{'OK' if present else 'FAIL'} words.column:{col}")
            ok = ok and present

        words_cnt = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        adj_cnt = conn.execute("SELECT COUNT(*) FROM words WHERE type='adjektiv'").fetchone()[0]
        missing_adj_forms = conn.execute(
            """
            SELECT COUNT(*)
            FROM words
            WHERE type='adjektiv'
              AND (TRIM(COALESCE(komparativ,''))='' OR TRIM(COALESCE(superlativ,''))='')
            """
        ).fetchone()[0]
        lines.append(f"INFO words.total={words_cnt}")
        lines.append(f"INFO words.adjektiv={adj_cnt}")
        lines.append(f"INFO words.adjektiv_missing_forms={missing_adj_forms}")

        orphan_progress = conn.execute(
            """
            SELECT COUNT(*)
            FROM progress p
            WHERE NOT EXISTS (SELECT 1 FROM words w WHERE w.id = p.word_id)
            """
        ).fetchone()[0]
        orphan_events = conn.execute(
            """
            SELECT COUNT(*)
            FROM quiz_events e
            WHERE NOT EXISTS (SELECT 1 FROM words w WHERE w.id = e.word_id)
            """
        ).fetchone()[0]
        lines.append(f"{'OK' if orphan_progress == 0 else 'FAIL'} orphan.progress={orphan_progress}")
        lines.append(f"{'OK' if orphan_events == 0 else 'FAIL'} orphan.quiz_events={orphan_events}")
        ok = ok and (orphan_progress == 0) and (orphan_events == 0)

    return ok, lines


def main():
    parser = argparse.ArgumentParser(description="Smoke checks for words DB.")
    parser.add_argument("--db", default="./data/words.db", help="Path to SQLite DB")
    args = parser.parse_args()
    ok, lines = run_smoke(args.db)
    print("SMOKE CHECK DB")
    for line in lines:
        print(line)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
