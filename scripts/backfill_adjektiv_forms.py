import argparse
import json
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from bot.ai import generate_fields
from bot.storage import init_db

NON_COMPARABLE_ADJEKTIV = {
    "verheiratet",
    "verwitwet",
    "geschieden",
    "ledig",
    "sehr gut",
}


def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def _parse_forms(text):
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError:
        return "", ""
    komparativ = str(data.get("komparativ", "")).strip()
    superlativ = str(data.get("superlativ", "")).strip()
    return komparativ, superlativ


def _prompt(german, russian):
    return (
        "Return only JSON and nothing else: "
        "{\"komparativ\":\"...\",\"superlativ\":\"...\"}. "
        "Given a German adjective in base form, return its Komparativ and Superlativ. "
        "No explanations. No extra fields. "
        f"Word: {german}. Russian hint: {russian}"
    )


def _load_candidates(db_path, limit):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in NON_COMPARABLE_ADJEKTIV)
        query = """
            SELECT id, german, russian, komparativ, superlativ
            FROM words
            WHERE type = 'adjektiv'
              AND (TRIM(COALESCE(komparativ, '')) = '' OR TRIM(COALESCE(superlativ, '')) = '')
              AND lower(trim(german)) NOT IN ({})
            ORDER BY created_at ASC
        """.format(placeholders)
        params = tuple(sorted(NON_COMPARABLE_ADJEKTIV))
        if limit and int(limit) > 0:
            query += " LIMIT ?"
            params = params + (int(limit),)
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _apply_update(db_path, word_id, komparativ, superlativ):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE words
            SET komparativ = ?, superlativ = ?
            WHERE id = ?
            """,
            (komparativ, superlativ, word_id),
        )


def main():
    parser = argparse.ArgumentParser(description="Backfill komparativ/superlativ for existing adjectives.")
    parser.add_argument("--db", default="./data/words.db", help="Path to SQLite DB.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N adjectives (0 = all).")
    parser.add_argument("--apply", action="store_true", help="Apply updates to DB. Without this flag: dry-run only.")
    args = parser.parse_args()

    load_dotenv()
    db_path = Path(args.db)
    init_db(db_path)

    candidates = _load_candidates(db_path, args.limit)
    print(f"Candidates: {len(candidates)}")

    preview = []
    failed = []
    updated = 0

    for row in candidates:
        word_id = row["id"]
        german = (row.get("german") or "").strip()
        russian = (row.get("russian") or "").strip()
        old_komp = (row.get("komparativ") or "").strip()
        old_supr = (row.get("superlativ") or "").strip()
        try:
            raw = generate_fields(_prompt(german, russian))
            new_komp, new_supr = _parse_forms(raw)
        except Exception as exc:
            failed.append(
                {
                    "id": word_id,
                    "german": german,
                    "reason": f"exception: {exc}",
                }
            )
            continue

        final_komp = old_komp or new_komp
        final_supr = old_supr or new_supr

        if not final_komp or not final_supr:
            failed.append(
                {
                    "id": word_id,
                    "german": german,
                    "reason": "missing_forms",
                    "raw_response": raw,
                }
            )
            continue

        changed = (final_komp != old_komp) or (final_supr != old_supr)
        preview.append(
            {
                "id": word_id,
                "german": german,
                "old_komparativ": old_komp,
                "old_superlativ": old_supr,
                "new_komparativ": final_komp,
                "new_superlativ": final_supr,
                "changed": changed,
            }
        )

        if args.apply and changed:
            _apply_update(db_path, word_id, final_komp, final_supr)
            updated += 1

    out_dir = Path("./data")
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_path = out_dir / "backfill_adjektiv_forms_preview.json"
    failed_path = out_dir / "backfill_adjektiv_forms_failed.json"
    preview_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    failed_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Dry-run: {not args.apply}")
    print(f"Updated: {updated}")
    print(f"Preview: {preview_path.resolve()}")
    print(f"Failed: {len(failed)}")
    print(f"Failed report: {failed_path.resolve()}")


if __name__ == "__main__":
    main()
