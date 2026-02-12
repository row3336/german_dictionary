import json
import uuid
from pathlib import Path


def normalize_text(text):
    return " ".join(str(text).strip().lower().split())


def entry_key(entry):
    if entry.get("type") == "verb":
        return ("verb", normalize_text(entry.get("infinitive", "")))
    return ("nomen", normalize_text(entry.get("german", "")))


def normalize_russian_singular(text):
    text = str(text).strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def migrate(path):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        words = json.load(f)

    seen = set()
    cleaned = []
    replaced_ids = 0
    removed_dupes = 0

    for entry in words:
        entry = dict(entry)
        if entry.get("id") in (None, "", "unique"):
            entry["id"] = str(uuid.uuid4())
            replaced_ids += 1

        if "russian" in entry:
            entry["russian"] = normalize_russian_singular(entry["russian"])

        key = entry_key(entry)
        if key in seen:
            removed_dupes += 1
            continue
        seen.add(key)
        cleaned.append(entry)

    with path.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    return replaced_ids, removed_dupes, len(cleaned)


if __name__ == "__main__":
    data_path = "./data/words.json"
    replaced, removed, total = migrate(data_path)
    print(f"Replaced ids: {replaced}")
    print(f"Removed duplicates: {removed}")
    print(f"Total records: {total}")
