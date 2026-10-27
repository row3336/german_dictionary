"""Oral formatting and local keyboards; rendering never selects a question."""

from telegram import ReplyKeyboardMarkup

from ..i18n import t
from ..common import hilfsverb
from ..common.verb_construction import display_infinitive, construction_label, reflexive_hint
from ..ui.formatting import (
    _entry_translation, _example_lines, _format_de_ru_question, _format_ru_question,
    _md_escape, _append_verb_forms,
)
from .domain import dictionary_language, summary


def question(entry, mode):
    if mode.removeprefix('phrase_').startswith('de_'):
        return _format_de_ru_question(entry)
    text = _format_ru_question(entry, dictionary_language(mode))
    example = _example_lines(entry.get('example', ''))[:1]
    if example:
        text += '\n\n' + _md_escape(example[0])
    return text


def answer(entry, mode):
    if mode.removeprefix('phrase_').startswith('de_'):
        return '>>>    *' + _md_escape(_entry_translation(entry, dictionary_language(mode))) + '*'
    value = (display_infinitive(entry) + reflexive_hint(entry)
             if entry.get('type') == 'verb' else str(entry.get('german') or '').strip())
    lines = ['>>>    *' + _md_escape(value) + '*']
    if entry.get('type') == 'verb' and hilfsverb.value(entry) is not None:
        _append_verb_forms(lines, entry)
    return '\n'.join(lines)


def markup(frame, lang):
    if frame['phase'] == 'initial_hidden':
        rows = [[t(lang, 'stop'), t(lang, 'oral_next')]]
    elif frame['phase'] == 'grading':
        rows = [[t(lang, 'oral_wrong'), t(lang, 'oral_correct')],
                [t(lang, 'clarify_fields'), t(lang, 'stop')]]
    else:
        rows = [[t(lang, 'back')]]
        if frame.get('last_visible'):
            rows.insert(0, [t(lang, 'clarify_fields')])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def message(frame, lang, prefix=None):
    if frame['phase'] == 'completed':
        text = t(lang, 'oral_summary').format(**summary(frame))
    else:
        parts = []
        if frame.get('pending'):
            parts.append(answer(frame['pending']['entry'], frame['mode']))
        if frame.get('hidden'):
            parts.append(question(frame['hidden']['entry'], frame['mode']))
        text = '\n\n'.join(parts)
    if prefix:
        text = prefix + '\n\n' + text
    return text, {'parse_mode': 'Markdown', 'reply_markup': markup(frame, lang)}
