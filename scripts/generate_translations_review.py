"""One-off maintenance: re-parse ukrainian/english for every word via AI and
write the result to a review file — does NOT touch the DB.

Two independent passes per word (owner's spec, 2026-07-01):
1. ukrainian <- russian (already in the DB; a same-language-family translation
   is more precise than re-deriving it from German).
2. english <- german word (infinitive for verbs, "to <verb>" form), with the
   russian translation passed along only as a sense-disambiguation hint.

Deterministic: bot.ai.generate_fields() already forces temperature=0 for
non-reasoning models (gpt-4.1-nano included) — no extra work needed for that.
Both outputs are force-lowercased in code (not just asked for in the prompt).

Usage (from repo root, server or local):
    python3 -m scripts.generate_translations_review --db ./data/words.db \
        --out ./claude_docs/translations_review.json [--limit 20]

Review the output file, then apply it with scripts.apply_translations_review.
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv

from bot.ai import generate_fields


def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def _parse_key(text, key):
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError:
        return ""
    return str(data.get(key, "")).strip().lower()


def _ukrainian_prompt(russian):
    return (
        "Translate the following Russian word or short phrase into Ukrainian. "
        "Give the closest natural dictionary equivalent (same part of speech, same meaning), "
        "lowercase, no extra words or punctuation. "
        f'Return only JSON: {{"ukrainian":"..."}}. Russian: {russian}'
    )


def _english_prompt(entry):
    russian = entry.get("russian", "")
    if entry.get("type") == "verb":
        word = entry.get("infinitive", "")
        return (
            f'Give the English translation of the German verb "{word}" '
            f"(Russian meaning hint: {russian}). Answer with the English infinitive using the "
            '"to " prefix (e.g. gehen -> to go), lowercase, no extra words. '
            'Return only JSON: {"english":"..."}'
        )
    word = entry.get("german", "")
    return (
        f'Give the English translation of the German word "{word}" '
        f'(part of speech: {entry.get("type", "")}; Russian meaning hint: {russian}). '
        "Answer with a short natural dictionary equivalent, lowercase, no article, no extra words. "
        'Return only JSON: {"english":"..."}'
    )


def _generate_one(prompt, retries=1):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return generate_fields(prompt), None
        except Exception as exc:  # noqa: BLE001 - report and move on, never crash the batch
            last_exc = exc
            if attempt < retries:
                time.sleep(1)
    return "", str(last_exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/words.db")
    parser.add_argument("--out", default="./claude_docs/translations_review.json")
    parser.add_argument("--limit", type=int, default=0, help="process only the first N words (0 = all)")
    args = parser.parse_args()

    load_dotenv()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, type, german, infinitive, russian, ukrainian, english FROM words ORDER BY id"
    ).fetchall()
    conn.close()

    if args.limit:
        rows = rows[: args.limit]

    print(f"Words to process: {len(rows)}")
    results = []
    failed = 0

    for i, row in enumerate(rows, start=1):
        entry = dict(row)
        uk_raw, uk_err = _generate_one(_ukrainian_prompt(entry.get("russian", "")))
        ukrainian_new = _parse_key(uk_raw, "ukrainian") if not uk_err else ""
        en_raw, en_err = _generate_one(_english_prompt(entry))
        english_new = _parse_key(en_raw, "english") if not en_err else ""

        if not ukrainian_new or not english_new:
            failed += 1

        results.append(
            {
                "id": entry["id"],
                "type": entry["type"],
                "german": entry["german"],
                "infinitive": entry["infinitive"],
                "russian": entry["russian"],
                "ukrainian_old": entry["ukrainian"],
                "ukrainian_new": ukrainian_new or None,
                "english_old": entry["english"],
                "english_new": english_new or None,
                "error": uk_err or en_err,
            }
        )
        if i % 50 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)} (failed so far: {failed})")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Done. Total: {len(results)}, failed: {failed}")
    print(f"Review file: {out_path.resolve()}")
    print("Nothing was written to the DB. Review the file, then run scripts.apply_translations_review.")


if __name__ == "__main__":
    main()
