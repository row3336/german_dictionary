import json
from pathlib import Path

from dotenv import load_dotenv

from bot.ai import generate_fields
from bot.storage import CEFR_LEVELS, init_db, list_words_for_cefr_backfill, update_word_cefr_level


def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def _parse_level(text):
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError:
        return ""
    level = str(data.get("cefr_level", "")).strip().upper()
    return level if level in CEFR_LEVELS else ""


def _prompt(entry):
    entry_type = entry.get("type", "")
    if entry_type == "verb":
        word = entry.get("infinitive", "")
    else:
        word = entry.get("german", "")
    russian = entry.get("russian", "")
    return (
        "Return only JSON: {\"cefr_level\":\"A1|A2|B1|B2|C1|C2\"}. "
        "Pick the most common CEFR level for this German word in everyday learning context. "
        f"Word: {word}. Type: {entry_type}. Russian: {russian}"
    )


def main():
    load_dotenv()
    db_path = Path("./data/words.db")
    init_db(db_path)

    words = list_words_for_cefr_backfill(db_path)
    print(f"Words without CEFR level: {len(words)}")
    updated = 0
    failed = 0
    failed_items = []

    for entry in words:
        try:
            raw = generate_fields(_prompt(entry))
            level = _parse_level(raw)
            if not level:
                failed += 1
                failed_items.append(
                    {
                        "id": entry.get("id"),
                        "type": entry.get("type"),
                        "german": entry.get("german"),
                        "infinitive": entry.get("infinitive"),
                        "russian": entry.get("russian"),
                        "reason": "invalid_or_missing_cefr_level",
                        "raw_response": raw,
                    }
                )
                continue
            update_word_cefr_level(db_path, entry["id"], level)
            updated += 1
        except Exception as exc:
            failed += 1
            failed_items.append(
                {
                    "id": entry.get("id"),
                    "type": entry.get("type"),
                    "german": entry.get("german"),
                    "infinitive": entry.get("infinitive"),
                    "russian": entry.get("russian"),
                    "reason": f"exception: {exc}",
                }
            )

    failed_path = Path("./data/backfill_cefr_failed.json")
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    with failed_path.open("w", encoding="utf-8") as f:
        json.dump(failed_items, f, ensure_ascii=False, indent=2)

    print(f"Updated: {updated}")
    print(f"Failed: {failed}")
    print(f"Failed report: {failed_path.resolve()}")


if __name__ == "__main__":
    main()
