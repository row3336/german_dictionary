import json
from pathlib import Path

from dotenv import load_dotenv

from bot.ai import generate_fields
from bot.storage import init_db, list_words_for_translations_backfill, update_word_translations


def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def _parse_translations(text):
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError:
        return "", ""
    ukr = str(data.get("ukrainian", "")).strip()
    eng = str(data.get("english", "")).strip()
    return ukr, eng


def _prompt(entry):
    entry_type = entry.get("type", "")
    word = entry.get("infinitive", "") if entry_type == "verb" else entry.get("german", "")
    russian = entry.get("russian", "")
    return (
        "Return only JSON: {\"ukrainian\":\"...\",\"english\":\"...\"}. "
        "Translate this German word into Ukrainian and English. "
        "Use short dictionary forms in singular for nouns. "
        f"Word: {word}. Type: {entry_type}. Russian hint: {russian}"
    )


def main():
    load_dotenv()
    db_path = Path("./data/words.db")
    init_db(db_path)

    words = list_words_for_translations_backfill(db_path)
    print(f"Words without translations: {len(words)}")
    updated = 0
    failed = 0
    failed_items = []

    for entry in words:
        try:
            raw = generate_fields(_prompt(entry))
            ukr, eng = _parse_translations(raw)
            if not ukr or not eng:
                failed += 1
                failed_items.append(
                    {
                        "id": entry.get("id"),
                        "type": entry.get("type"),
                        "german": entry.get("german"),
                        "infinitive": entry.get("infinitive"),
                        "russian": entry.get("russian"),
                        "reason": "invalid_or_missing_translation",
                        "raw_response": raw,
                    }
                )
                continue

            update_word_translations(db_path, entry["id"], ukrainian=ukr, english=eng)
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

    failed_path = Path("./data/backfill_translations_failed.json")
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    with failed_path.open("w", encoding="utf-8") as f:
        json.dump(failed_items, f, ensure_ascii=False, indent=2)

    print(f"Updated: {updated}")
    print(f"Failed: {failed}")
    print(f"Failed report: {failed_path.resolve()}")


if __name__ == "__main__":
    main()
