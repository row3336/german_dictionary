"""Oral formatting and local keyboards; rendering never selects a question.

A card is plain text plus explicit Telegram entities (UTF-16 offsets): stored
dictionary text is never parsed as markup. Only the current pending entry is
shown — its question, then its own answer under a native spoiler. The slot
reserved internally for the next card is never rendered.
"""

from telegram import MessageEntity, ReplyKeyboardMarkup

from ..i18n import t
from ..common import hilfsverb
from ..common.search_view import utf16_length
from ..common.verb_construction import display_infinitive, reflexive_hint
from ..text_utils import cap_plural, cap_word, low_word, normalize_russian_singular
from ..ui.formatting import (
    _article_with_emoji, _entry_translation, _example_lines, _expected_partizip2,
    _linked_verb_parts, _preposition_hint_parts, _type_hint,
)
from .domain import dictionary_language, summary

# Bot API text limit; checked on the plain message text (code points).
MESSAGE_LIMIT = 4096
# Spoiler block candidate: at least three lines, short lines widened with blank
# Braille cells (not whitespace, so never trimmed). Client geometry is unverified.
SPOILER_LINES = 3
SPOILER_WIDTH = 16
FILLER = "⠀"
QUESTION_MARK = ">> ❓"
ANSWER_PREFIX = ">>> "


class OralCardTooLong(ValueError):
    """The complete answer cannot fit one message; never sent truncated."""


def _de_direction(mode):
    return mode.removeprefix('phrase_').startswith('de_')


def _italic(text):
    return [("  ", None), (text, 'italic')]


def _verb_forms(entry):
    parts = _linked_verb_parts(entry)
    if parts is None:
        parts = [low_word(entry.get("prateritum", "")), _expected_partizip2(entry)]
    return [_italic(form) for form in parts if form]


def _de_question(entry):
    kind = low_word(entry.get("type", ""))
    if kind == "nomen":
        article = _article_with_emoji(entry.get("article", ""))
        german = cap_word(entry.get("german", ""))
        lines = [[(f"{article} {german}".strip() if article else german, None)]]
        plural = cap_plural(entry.get("plural", ""))
        if plural:
            lines.append(_italic(f"pl. die {plural}"))
    elif kind == "verb":
        lines = [[(low_word(display_infinitive(entry)) + reflexive_hint(entry), None)]]
        lines.extend(_verb_forms(entry))
    elif kind == "adjektiv":
        lines = [[(low_word(entry.get("german", "")), None)]]
        lines.extend(_italic(low_word(entry.get(key, ""))) for key in ("komparativ", "superlativ")
                     if low_word(entry.get(key, "")))
    elif kind == "praeposition":
        german, parts = low_word(entry.get("german", "")), _preposition_hint_parts(entry)
        lines = [[(f"{german} ({', '.join(parts)})" if parts else german, None)]]
    elif kind == "phrase":
        lines = [[(str(entry.get("german", "") or "").strip(), None)]]
    else:
        lines = [[(low_word(entry.get("german", "")), None)]]
    image = str(entry.get("image", "") or "").strip()
    if image:
        lines += [[], [(image, None)]]
    return lines


def _l1_question(entry, mode):
    value = normalize_russian_singular(_entry_translation(entry, dictionary_language(mode)))
    return [[(">>    " + value + _type_hint(entry), None)]]


def _answer(entry, mode):
    if _de_direction(mode):
        value, forms = _entry_translation(entry, dictionary_language(mode)), []
    elif entry.get("type") == "verb":
        value = display_infinitive(entry) + reflexive_hint(entry)
        forms = [_italic(form) for form in _linked_verb_parts(entry)] if hilfsverb.value(entry) is not None else []
    else:
        value, forms = str(entry.get("german") or "").strip(), []
    rows = str(value).split("\n") or [""]
    lines = [[(ANSWER_PREFIX, None), (rows[0], 'bold')]] + [[(row, 'bold')] for row in rows[1:]]
    return lines + forms


def _plain(lines):
    return "\n".join("".join(text for text, _ in line) for line in lines)


def question(entry, mode):
    """Plain question block (word/phrase, forms, media text, first example)."""
    lines = _de_question(entry) if _de_direction(mode) else _l1_question(entry, mode)
    example = _example_lines(entry.get("example", ""))[:1]
    if example:
        lines += [[], [(example[0], None)]]
    return _plain(lines)


def answer(entry, mode):
    """Plain answer block exactly as shown inside the spoiler (without padding)."""
    return _plain(_answer(entry, mode))


def _assemble(lines, spoiler_from):
    text, entities, units, spoiler_start = [], [], 0, None
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
            units += 1
        if index == spoiler_from:
            spoiler_start = units
        for segment, kind in line:
            if not segment:
                continue
            size = utf16_length(segment)
            if kind:
                entities.append((kind, units, size))
            text.append(segment)
            units += size
    if spoiler_start is not None and units > spoiler_start:
        entities.append(('spoiler', spoiler_start, units - spoiler_start))
    entities.sort(key=lambda item: (item[1], -item[2]))
    return "".join(text), entities


def _spoiler(lines, width):
    padded = []
    for line in lines:
        size = sum(len(text) for text, _ in line)
        padded.append(line + [(FILLER * (width - size), None)] if size < width else line)
    while len(padded) < SPOILER_LINES:
        padded.append([(FILLER * max(width, 1), None)])
    return padded


def card(entry, mode, prefix=None):
    """(text, [(type, utf16_offset, utf16_length)]); optional parts shrink first."""
    head = [[(row, None)] for row in str(prefix).split("\n")] + [[]] if prefix else []
    body = _de_question(entry) if _de_direction(mode) else _l1_question(entry, mode)
    example = _example_lines(entry.get("example", ""))[:1]
    for with_example, width in ((True, SPOILER_WIDTH), (False, SPOILER_WIDTH), (False, 1)):
        lines = head + body
        if with_example and example:
            lines = lines + [[], [(example[0], None)]]
        lines = lines + [[], [(QUESTION_MARK, None)], []]
        text, entities = _assemble(lines + _spoiler(_answer(entry, mode), width), len(lines))
        if len(text) <= MESSAGE_LIMIT:
            return text, entities
    raise OralCardTooLong("oral answer does not fit one Telegram message")


def markup(frame, lang):
    if frame['phase'] == 'completed':
        rows = [[t(lang, 'back')]]
        if frame.get('last_visible'):
            rows.insert(0, [t(lang, 'clarify_fields')])
    else:
        rows = [[t(lang, 'oral_wrong'), t(lang, 'oral_correct')],
                [t(lang, 'clarify_fields'), t(lang, 'stop')]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def message(frame, lang, prefix=None):
    pending = frame.get('pending')
    if frame['phase'] == 'completed' or not pending:
        text = t(lang, 'oral_summary').format(**summary(frame))
        if prefix:
            text = prefix + '\n\n' + text
        entities = None
    else:
        text, entities = card(pending['entry'], frame['mode'], prefix)
    return text, {'parse_mode': None, 'entities': entities, 'reply_markup': markup(frame, lang)}


def send_kwargs(kwargs):
    """Telegram objects are built at send time; cached views stay plain data."""
    entities = kwargs.get('entities')
    return {**kwargs, 'entities': None if entities is None else
            [MessageEntity(kind, offset, length) for kind, offset, length in entities]}
