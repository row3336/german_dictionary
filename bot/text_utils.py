import json
import re


def normalize_answer(text):
    return " ".join(text.strip().lower().split())


def normalize_lookup_text(text):
    return " ".join(str(text).strip().lower().split())


def normalize_russian_singular(text):
    text = str(text).strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def russian_variants(text):
    source = str(text or "")
    parts = re.split(r"[,/;]", source)
    out = []
    for part in parts:
        value = normalize_answer(part)
        if value:
            out.append(value)
    return out


def md_escape(text):
    text = str(text or "")
    for ch in ("*", "_", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


def type_label(raw_type):
    return normalize_lookup_text(raw_type)


def cap_word(word):
    """Capitalize a noun for storage.

    A mixed-case tail (uppercase AND lowercase after the first letter, e.g.
    "KFZ-Mechaniker") is intentional casing — keep it, only raise the first
    letter. A mono-case word ("haus", "HAUS") is a typo/caps input — classic
    first-upper + rest-lower. (Owner rule, todo T1, 2026-07-11.)
    """
    word = str(word or "").strip()
    if not word:
        return ""
    tail = word[1:]
    has_upper = any(c.isupper() for c in tail)
    has_lower = any(c.islower() for c in tail)
    if has_upper and has_lower:
        return word[:1].upper() + tail
    return word[:1].upper() + tail.lower()


def low_word(word):
    return str(word or "").strip().lower()


def split_plural_forms(plural):
    """Split a stored plural field into individual forms (comma/slash separated).

    A noun can have several plural forms (e.g. 'Pizzas, Pizzen'); each must be
    matchable and displayable on its own.
    """
    raw = str(plural or "")
    return [p.strip() for p in re.split(r"[,/]", raw) if p.strip()]


def cap_plural(plural):
    """Capitalize every plural form (German plurals are nouns)."""
    forms = split_plural_forms(plural)
    if not forms:
        return cap_word(plural)
    return ", ".join(cap_word(f) for f in forms)


def split_text_chunks(text, max_size=3500):
    """Split text into <=max_size chunks on line boundaries."""
    text = str(text or "").strip()
    if not text:
        return []
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        add = len(line) + 1
        if current and current_len + add > max_size:
            chunks.append("\n".join(current).strip())
            current = [line]
            current_len = add
        else:
            current.append(line)
            current_len += add
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1:
        return text
    if end == -1:
        return text[start:]
    return text[start : end + 1]


def try_parse_json(text):
    candidate = extract_json(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        opens = candidate.count("{")
        closes = candidate.count("}")
        if opens > closes:
            fixed = candidate + ("}" * (opens - closes))
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                return None
        return None
