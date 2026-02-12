import json
import uuid
from datetime import date, timedelta
from pathlib import Path

DEFAULT_EASE = 2.5
MIN_EASE = 1.3


def default_srs(today=None):
    if today is None:
        today = date.today()
    return {
        "interval": 1,
        "repetitions": 0,
        "ease": DEFAULT_EASE,
        "due": today.isoformat(),
    }


def load_words(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_words(path, words):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)


def add_word(words, entry):
    entry = dict(entry)
    if entry.get("id") in (None, "", "unique"):
        entry["id"] = str(uuid.uuid4())
    entry.setdefault("srs", default_srs())
    _dedupe_entry(words, entry)
    words.append(entry)
    return entry


def _normalize_text(text):
    return " ".join(str(text).strip().lower().split())


def _entry_key(entry):
    if entry.get("type") == "verb":
        return ("verb", _normalize_text(entry.get("infinitive", "")))
    return ("nomen", _normalize_text(entry.get("german", "")))


def _dedupe_entry(words, entry):
    key = _entry_key(entry)
    for w in words:
        if _entry_key(w) == key:
            raise ValueError("Already in database.")


def create_template_from_word(raw_word):
    raw_word = raw_word.strip()
    if not raw_word:
        return None

    # Nomen if only the first letter is uppercase and the rest is lowercase.
    is_nomen = raw_word[:1].isupper() and (len(raw_word) == 1 or raw_word[1:].islower())
    if is_nomen:
        return {
            "id": "unique",
            "type": "nomen",
            "article": "",
            "german": raw_word,
            "plural": "",
            "russian": "",
            "srs": default_srs(),
        }

    return {
        "id": "unique",
        "type": "verb",
        "infinitive": raw_word,
        "prateritum": "",
        "partizip2": "",
        "russian": "",
        "srs": default_srs(),
    }


def select_due(words, today=None):
    if today is None:
        today = date.today()
    due = []
    for w in words:
        try:
            if date.fromisoformat(w["srs"]["due"]) <= today:
                due.append(w)
        except Exception:
            continue
    return due


def update_sm2(srs, quality, today=None):
    if today is None:
        today = date.today()

    quality = max(0, min(5, int(quality)))
    interval = int(srs.get("interval", 1))
    repetitions = int(srs.get("repetitions", 0))
    ease = float(srs.get("ease", DEFAULT_EASE))

    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = int(round(interval * ease))
        repetitions += 1

    ease = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(MIN_EASE, ease)

    due = today + timedelta(days=interval)
    return {
        "interval": interval,
        "repetitions": repetitions,
        "ease": round(ease, 2),
        "due": due.isoformat(),
    }
