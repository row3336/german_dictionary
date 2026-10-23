"""Balanced L1 hints: preserve display text and exclude explanations from answers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TranslationHints:
    display: str
    variants: tuple[str, ...]
    diagnostic: bool = False


def parse_translation_hints(text):
    raw = str(text or "")
    depth, valid = 0, True
    outside = []
    for index, char in enumerate(raw):
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                valid = False
            else:
                depth -= 1
                if depth == 0:
                    # Hints may sit after a word, but never join two word parts.
                    if index + 1 < len(raw) and raw[index + 1].isalnum() and outside and outside[-1].isalnum():
                        valid = False
                    outside.append(" ")
        elif depth == 0:
            outside.append(char)
    if depth or not valid:
        # Never accept a guessed fragment from an ambiguous explanation.
        return TranslationHints(raw, (), True)
    import re
    variants = tuple(part.strip() for part in re.split(r"[,/;]", "".join(outside)) if part.strip())
    return TranslationHints(raw, variants)


def first_display_variant(text):
    """Keep the historical first slash option, including its complete hint."""
    raw = str(text or "").strip()
    parsed = parse_translation_hints(raw)
    if parsed.diagnostic:
        return raw
    depth = 0
    for index, char in enumerate(raw):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "/" and depth == 0:
            return raw[:index].strip()
    return raw
