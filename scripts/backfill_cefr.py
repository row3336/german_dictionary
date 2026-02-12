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

    for entry in words:
        try:
            level = _parse_level(generate_fields(_prompt(entry)))
            if not level:
                failed += 1
                continue
            update_word_cefr_level(db_path, entry["id"], level)
            updated += 1
        except Exception:
            failed += 1

    print(f"Updated: {updated}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
