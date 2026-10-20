"""Add-word feature + AI recognition pipeline: text/document/JSON input, the
precheck stage (spell-correct + dedup), the per-word AI recognition queue, the
recognized-entry preview/confirm/clarify flow, saving, banning, and queue stop.
"""

import logging
import os
import re

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..ai import generate_fields
from ..app_config import (
    ADD_CLARIFY,
    ADD_CONFIRM,
    ADD_EDIT,
    ADD_INPUT,
    ADD_MENU,
    ADD_PRECHECK,
    ADD_PRECHECK_EDIT,
    AI_BAN_WORD,
    AI_CLARIFY_AI,
    AI_CLARIFY_FIELD,
    AI_CONFIRM,
    AI_KEYS,
    AI_REJECT,
    AI_SKIP,
    ALLOWED_AI_TYPES,
    ARTICLE_SET,
    CASE_TYPES,
    CEFR_LEVELS,
    MENU,
    NUMERAL_SET,
    NUMERIC_RE,
    PRECHECK_CONFIRM,
    PRECHECK_EDIT,
    PRECHECK_KEYS,
    PRECHECK_REJECT,
    PREPOSITION_TYPES,
    STOP_TEXT,
    TOPIC_FILTER_EDIT,
    get_db_path,
)
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.add_words_gate import guard_add_handler
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _reply_text_with_retry
from ..common.settings import _topic_filter_banner
from ..common.topics import parse_topic_input
from ..i18n import t
from ..storage import (
    add_banned_word,
    add_word,
    find_existing_word,
    has_translation_collision,
    insert_topic_if_absent,
    is_banned_word,
    list_all_words,
    list_topics,
    list_topics_with_desc,
    set_topic_level,
)
from ..text_utils import cap_word as _cap_word
from ..text_utils import md_escape as _md_escape
from ..text_utils import normalize_lookup_text as _normalize_lookup_text
from ..text_utils import normalize_russian_singular as _normalize_russian_singular
from ..text_utils import russian_variants as _russian_variants
from ..text_utils import try_parse_json as _try_parse_json
from ..text_utils import type_label as _type_label
from ..ui.formatting import _entry_translation, _format_word_card
from ..verb_forms_parsing.add_flow import vf_add_prompt, vf_recheck_prompt
from ..ui.keyboards import (
    _add_input_markup,
    _add_keys,
    _add_menu_markup,
    _clarify_fields_markup,
    _main_menu_markup,
)


def _stop_keys(user_id):
    """Handle stop keys."""
    return [[t(_user_interface_lang(user_id), "stop")]]


def _clarify_keys(user_id):
    """Handle clarify keys."""
    return AI_KEYS


def _display_type_label(raw_type):
    """Handle display type label."""
    token = _type_label(raw_type)
    mapping = {
        "nomen": "Nomen",
        "verb": "Verb",
        "phrase": "Phrase",
        "adjektiv": "Adjektiv",
        "pronomen": "Pronomen",
        "adverb": "Adverb",
        "praeposition": "Praeposition",
        "konjunktion": "Konjunktion",
        "partikel": "Partikel",
    }
    return mapping.get(token, token.capitalize() if token else "?")


def _display_ai_entry(entry):
    """Handle display ai entry."""
    result = dict(entry or {})
    entry_type = _type_label(result.get("type", ""))
    result["type"] = entry_type
    if entry_type == "phrase":
        return result
    if entry_type == "nomen":
        article = str(result.get("article", "")).strip().lower()
        if article:
            result["article"] = article
        result["german"] = _cap_word(result.get("german", ""))
    else:
        for field in ("german", "infinitive", "prateritum", "partizip2", "komparativ", "superlativ"):
            value = str(result.get(field, "")).strip().lower()
            if value:
                result[field] = value
    for field in ("russian", "ukrainian", "english"):
        value = str(result.get(field, "")).strip().lower()
        if value:
            result[field] = value
    return result


def _scan_words_before_ai(lines):
    """Handle scan words before ai."""
    filtered = []
    existing = []
    recognition = []
    for raw in lines:
        parsed = _parse_add_line_spec(raw)
        if not parsed:
            continue
        reason = _prefilter_reject_reason(parsed.get("lookup_word", ""))
        if reason:
            filtered.append({"raw": parsed.get("raw", raw), "reason": reason})
            continue
        if _exists_word_for_type(parsed.get("forced_type"), parsed.get("lookup_word", "")):
            existing.append({"raw": parsed.get("raw", raw), "reason": "Already in database for this type."})
            continue
        recognition.append(parsed)
    return filtered, existing, recognition


def _parse_add_line_spec(raw_line):
    """Parse add-word line with optional [type] and comma-separated forms."""
    raw = str(raw_line or "").strip()
    if not raw:
        return None
    # Tolerate re-fed precheck/Edit output: drop leading "N." numbering and the
    # trailing "(from ...)" spell-correction note, so an already-formatted line
    # like "1. von [Praeposition] (from von)" parses back to "von [Praeposition]".
    raw = re.sub(r"^\s*\d+[.)]\s+", "", raw)
    raw = re.sub(r"\s*\(from\b[^)]*\)\s*$", "", raw).strip()
    if not raw:
        return None
    m = re.match(r"^(.*?)\s*\[([a-zA-Z_]+)\]\s*$", raw)
    forced_type = ""
    body = raw
    if m:
        body = m.group(1).strip()
        forced_type = _type_label(m.group(2))
    parts = [x.strip() for x in body.split(",") if x.strip()]
    if not parts:
        return None
    item = {
        "raw": raw,
        "body": body,
        "forced_type": forced_type,
        "lookup_word": "",
        "forced_fields": {},
    }
    if forced_type == "verb":
        item["lookup_word"] = parts[0]
        item["forced_fields"] = {
            "infinitive": parts[0],
            "prateritum": parts[1] if len(parts) > 1 else "",
            "partizip2": parts[2] if len(parts) > 2 else "",
        }
        return item
    if forced_type == "phrase":
        phrase = " ".join(parts[0].split())
        item["lookup_word"] = phrase
        item["forced_fields"] = {
            "german": phrase,
        }
        return item
    if forced_type == "nomen":
        first = parts[0]
        article, base = _split_leading_article(first)
        item["lookup_word"] = base or first
        item["forced_fields"] = {
            "article": article,
            "german": base or first,
            "plural": parts[1] if len(parts) > 1 else "",
        }
        return item
    # Explicit type already recognized (adjektiv/adverb/praeposition/pronomen/
    # konjunktion/partikel) — keep it; don't let auto-article/auto-phrase override.
    if forced_type in ALLOWED_AI_TYPES:
        item["lookup_word"] = " ".join(body.split())
        return item
    # Article-led inputs must stay nouns, never auto-phrases.
    article_auto, base_auto = _split_leading_article(parts[0])
    if article_auto:
        item["forced_type"] = "nomen"
        item["lookup_word"] = base_auto or parts[0]
        item["forced_fields"] = {
            "article": article_auto,
            "german": base_auto or parts[0],
            "plural": parts[1] if len(parts) > 1 else "",
        }
        return item
    # Auto phrase mode for multi-word input without comma-separated forms.
    if "," not in body and len(body.split()) > 1:
        phrase = " ".join(body.split())
        item["forced_type"] = "phrase"
        item["lookup_word"] = phrase
        item["forced_fields"] = {"german": phrase}
        return item
    item["lookup_word"] = parts[0]
    return item


def _exists_word_for_type(forced_type, lookup_word):
    """Type-aware duplicate check for add pipeline."""
    word = _normalize_lookup_text(lookup_word)
    if not word:
        return False
    if forced_type == "praeposition":
        # spec #1: a second meaning of the same preposition is a legitimate new word
        # (distinct preposition_type/case_type). Don't pre-block on spelling here —
        # the exact (german, preposition_type, case_type) duplicate is enforced at
        # save time in storage._dedupe_entry.
        return False
    if forced_type not in {"nomen", "verb", "phrase"}:
        return bool(find_existing_word(get_db_path(), lookup_word))
    for entry in list_all_words(get_db_path()):
        et = _type_label(entry.get("type", ""))
        if et != forced_type:
            continue
        if forced_type == "verb":
            variants = {_normalize_lookup_text(entry.get("infinitive", ""))}
            variants.update(_russian_variants(entry.get("prateritum", "")))
            variants.update(_russian_variants(entry.get("partizip2", "")))
        elif forced_type == "phrase":
            variants = {_normalize_lookup_text(entry.get("german", ""))}
        else:
            variants = {_normalize_lookup_text(entry.get("german", ""))}
            variants.update(_russian_variants(entry.get("plural", "")))
        if word in variants:
            return True
    return False


def _prefilter_reject_reason(word):
    """Handle prefilter reject reason."""
    normalized = _normalize_lookup_text(word)
    if not normalized:
        return "Empty value."
    if len(normalized) < 2:
        return "Filtered: too short word."
    if NUMERIC_RE.match(normalized):
        return "Filtered: looks like a number."
    if normalized in ARTICLE_SET:
        return "Filtered: article words are not stored."
    if normalized in NUMERAL_SET:
        return "Filtered: numeral words are not stored."
    if is_banned_word(get_db_path(), normalized):
        return "Filtered: banned word."
    return None


@guard_add_handler
async def _save_entry(update: Update, entry):
    """Handle save entry."""
    if "russian" in entry:
        entry["russian"] = _normalize_russian_singular(entry["russian"])
    try:
        saved = add_word(get_db_path(), entry)
    except ValueError as exc:
        await update.message.reply_text(
            str(exc),
            reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        )
        return None

    await update.message.reply_text(
        f"✅ Saved: {saved.get('id')}",
        reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
    )
    return saved


def _ai_prompt(word, clarification):
    """Handle ai prompt."""
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "Phrase type is also allowed for fixed expressions: return type=phrase, german, russian. "
        "For nomen return: type, article, german, plural, russian. "
        "For verb return: type, infinitive, prateritum, partizip2, russian. "
        "For adjektiv return: type, german, komparativ, superlativ, russian. "
        "For praeposition return: type, german, russian, preposition_type, case_type. "
        f"preposition_type is the semantic category, one of: {', '.join(PREPOSITION_TYPES)}. "
        "case_type is the governed case, one of: dativ, akkusativ, genitiv, wechsel "
        "(wechsel = two-way Wechselpraeposition: Dativ on Wo?, Akkusativ on Wohin?). "
        "If unsure about either, return an empty string for it. "
        "For all other allowed types return: type, german, russian. "
        "Use Russian translations in singular only. No extra text. "
        "German noun plurals are irregular: recall the exact plural form, "
        "do not guess by mechanically adding -e/-s. Examples:\n"
        '{"type":"nomen","article":"das","german":"Wort","plural":"Wörter","russian":"слово"}\n'
        '{"type":"nomen","article":"der","german":"Bus","plural":"Busse","russian":"автобус"}\n'
        '{"type":"nomen","article":"die","german":"Stadt","plural":"Städte","russian":"город"}\n'
        '{"type":"verb","infinitive":"gehen","prateritum":"ging","partizip2":"gegangen","russian":"идти"}\n'
        '{"type":"praeposition","german":"mit","russian":"с","preposition_type":"modal","case_type":"dativ"}'
        f"\nWord: {word}{extra}"
    )


def _split_leading_article(raw_word):
    """Extract leading der/die/das article from raw input."""
    text = str(raw_word or "").strip()
    if not text:
        return "", ""
    parts = text.split(None, 1)
    if len(parts) < 2:
        return "", text
    first = parts[0].strip().lower()
    if first not in {"der", "die", "das"}:
        return "", text
    return first, parts[1].strip()


def _ai_precheck_prompt(word):
    """Handle ai precheck prompt."""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, phrase, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "Correct spelling if needed. "
        "Return JSON fields: type, value (corrected base form). "
        "For nouns, value is the singular base form (no article). "
        "For verbs, value is the infinitive. "
        "For other types, value is the base German form. "
        f"\nWord: {word}"
    )


def _parse_precheck_entry(entry):
    """Handle parse precheck entry."""
    if not isinstance(entry, dict):
        return None
    entry_type = _type_label(entry.get("type", ""))
    if entry_type not in ALLOWED_AI_TYPES:
        return None
    value = entry.get("value") or entry.get("german") or entry.get("infinitive") or entry.get("word")
    value = str(value or "").strip()
    if not value:
        return None
    if entry_type == "nomen":
        value = _cap_word(value)
    return {"type": entry_type, "word": value}


def _format_precheck_list(items, topic):
    """Handle format precheck list."""
    lines = [f"Precheck list (topic: {topic}):"]
    active = [x for x in items if x.get("word")]
    for idx, item in enumerate(active, start=1):
        word = item.get("word", "")
        item_type = _display_type_label(item.get("type", "?"))
        original = item.get("original", "")
        if original and _normalize_lookup_text(original) != _normalize_lookup_text(word):
            lines.append(f"{idx}. {word} [{item_type}] (from {original})")
        else:
            lines.append(f"{idx}. {word} [{item_type}]")
    skipped = [x for x in items if x.get("skipped_reason")]
    if skipped:
        lines.append("")
        lines.append("Skipped:")
        for item in skipped:
            lines.append(f"- {item.get('original','')} -> {item.get('skipped_reason','')}")
    return "\n".join(lines)


@guard_add_handler
async def _precheck_words(update: Update, context: ContextTypes.DEFAULT_TYPE, words):
    """Handle precheck words."""
    if not os.getenv("OPENAI_API_KEY"):
        await _reply_text_with_retry(
            update.message,
            "AI is not configured. Set OPENAI_API_KEY in .env.",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return None

    items = []
    for raw_item in words:
        if isinstance(raw_item, dict):
            raw = raw_item.get("raw", "")
            lookup_word = raw_item.get("lookup_word", raw)
            forced_type = raw_item.get("forced_type", "")
        else:
            raw = str(raw_item)
            lookup_word = raw
            forced_type = ""
        if forced_type in ALLOWED_AI_TYPES and lookup_word:
            # Type already recognized (explicit [type] tag) — keep it, skip AI, and
            # use the bare word as "original" so no spurious "(from ...)" is shown.
            items.append(
                {
                    "original": lookup_word,
                    "word": lookup_word,
                    "type": forced_type,
                    "forced_fields": dict(raw_item.get("forced_fields") or {}) if isinstance(raw_item, dict) else {},
                    "forced_type": forced_type,
                }
            )
            continue
        prompt_text = _ai_precheck_prompt(lookup_word)
        try:
            response_text = generate_fields(prompt_text)
        except Exception:
            items.append({"original": raw, "word": raw, "type": "?"})
            continue
        entry = _try_parse_json(response_text)
        parsed = _parse_precheck_entry(entry)
        if not parsed:
            items.append({"original": raw, "word": raw, "type": "?"})
            continue
        item = {"original": raw, "word": parsed["word"], "type": parsed["type"]}
        if isinstance(raw_item, dict):
            if raw_item.get("forced_fields"):
                item["forced_fields"] = dict(raw_item.get("forced_fields") or {})
            if raw_item.get("forced_type"):
                item["forced_type"] = raw_item.get("forced_type")
        items.append(item)
    return items


def _validate_ai_entry(entry):
    """Handle validate ai entry."""
    if not isinstance(entry, dict):
        return False

    entry_type = _type_label(entry.get("type", ""))
    if entry_type not in ALLOWED_AI_TYPES:
        return False

    if entry_type == "nomen":
        return bool(entry.get("german")) and bool(entry.get("russian"))
    if entry_type == "verb":
        return bool(entry.get("infinitive")) and bool(entry.get("russian"))
    return bool(entry.get("german")) and bool(entry.get("russian"))


def _normalize_ai_entry(entry, topic):
    """Handle normalize ai entry."""
    entry = dict(entry)
    entry_type = _type_label(entry.get("type", ""))
    entry["type"] = entry_type

    if entry_type == "verb":
        return {
            "type": "verb",
            "infinitive": str(entry.get("infinitive", "")).strip(),
            "prateritum": str(entry.get("prateritum", "")).strip(),
            "partizip2": str(entry.get("partizip2", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "topic": topic,
        }

    if entry_type == "nomen":
        return {
            "type": "nomen",
            "article": str(entry.get("article", "")).strip(),
            "german": str(entry.get("german", "")).strip(),
            "plural": str(entry.get("plural", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "topic": topic,
        }

    if entry_type == "adjektiv":
        return {
            "type": "adjektiv",
            "german": str(entry.get("german", "")).strip(),
            "komparativ": str(entry.get("komparativ", "")).strip(),
            "superlativ": str(entry.get("superlativ", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "topic": topic,
        }

    if entry_type == "praeposition":
        # spec #1: keep the two extra fields, but only if the AI returned a value from
        # the controlled vocabulary — anything else becomes '' (shown blank on the
        # owner review step to fix by hand). Never store free text here.
        ptype = str(entry.get("preposition_type", "")).strip().lower()
        ctype = str(entry.get("case_type", "")).strip().lower()
        return {
            "type": "praeposition",
            "german": str(entry.get("german", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "topic": topic,
            "preposition_type": ptype if ptype in PREPOSITION_TYPES else "",
            "case_type": ctype if ctype in CASE_TYPES else "",
        }

    return {
        "type": entry_type,
        "german": str(entry.get("german", "")).strip(),
        "russian": _normalize_russian_singular(entry.get("russian", "")),
        "ukrainian": str(entry.get("ukrainian", "")).strip(),
        "english": str(entry.get("english", "")).strip(),
        "topic": topic,
    }


def _is_non_infinitive_verb_input(raw_word, entry):
    """Handle is non infinitive verb input."""
    if entry.get("type") != "verb":
        return False
    source = _normalize_lookup_text(raw_word)
    infinitive = _normalize_lookup_text(entry.get("infinitive", ""))
    if not source or not infinitive:
        return False
    return source != infinitive


def _format_ai_preview(entry, dict_lang, has_synonym=False):
    """Handle format ai preview."""
    entry = _display_ai_entry(entry)
    entry_type = entry.get("type")
    translation = _entry_translation(entry, dict_lang)
    warn = "❗ has a synonym in DB" if has_synonym else ""
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        extras = f" ({plural})" if plural else ""
        lines = [
            f"Recognized {_display_type_label(entry_type)}",
            "",
            f"{art} *{_md_escape(ger)}*{_md_escape(extras)} - {_md_escape(translation)}",
        ]
        if warn:
            lines += ["", warn]
        return "\n".join(lines)

    if entry_type == "verb":
        inf = entry.get("infinitive", "")
        pr = entry.get("prateritum", "")
        p2 = entry.get("partizip2", "")
        extras = " / ".join([x for x in [pr, p2] if x])
        extras = f" ({extras})" if extras else ""
        lines = [
            f"Recognized {_display_type_label(entry_type)}",
            "",
            f"*{_md_escape(inf)}*{_md_escape(extras)} - {_md_escape(translation)}",
        ]
        if warn:
            lines += ["", warn]
        return "\n".join(lines)

    if entry_type == "adjektiv":
        ger = entry.get("german", "")
        komp = entry.get("komparativ", "")
        supr = entry.get("superlativ", "")
        extras = " / ".join([x for x in [komp, supr] if x])
        extras = f" ({extras})" if extras else ""
        lines = [
            f"Recognized {_display_type_label(entry_type)}",
            "",
            f"*{_md_escape(ger)}*{_md_escape(extras)} - {_md_escape(translation)}",
        ]
        if warn:
            lines += ["", warn]
        return "\n".join(lines)

    ger = entry.get("german", "")
    lines = [
        f"Recognized {_display_type_label(entry_type)}",
        "",
        f"*{_md_escape(ger)}* - {_md_escape(translation)}",
    ]
    # spec #1: surface the preposition fields on the review step so the owner can
    # verify them (blank = AI unsure / rejected value → fix via clarify buttons).
    if entry_type == "praeposition":
        ptype = entry.get("preposition_type", "") or "-"
        ctype = entry.get("case_type", "") or "-"
        lines.append(f"_preposition_type: {_md_escape(ptype)} · case_type: {_md_escape(ctype)}_")
    if warn:
        lines += ["", warn]
    return "\n".join(lines)


@guard_add_handler
async def _process_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word, clarification):
    """Handle process ai word."""
    has_queue = bool(context.user_data.get("add_queue"))
    if isinstance(word, dict):
        raw_word = str(word.get("raw", "")).strip() or str(word.get("lookup_word", "")).strip()
        forced_type = _type_label(word.get("forced_type", ""))
        forced_fields = dict(word.get("forced_fields") or {})
        lookup_word = str(word.get("lookup_word", raw_word)).strip() or raw_word
    else:
        raw_word = str(word or "").strip()
        forced_type = ""
        forced_fields = {}
        lookup_word = raw_word
    forced_article, normalized_word = _split_leading_article(lookup_word)
    lookup_word = normalized_word if forced_article and normalized_word else lookup_word

    reject_reason = _prefilter_reject_reason(lookup_word)
    if reject_reason:
        if has_queue:
            logging.info("Queue skip filtered word: %s", raw_word)
            return ADD_INPUT
        await _reply_text_with_retry(
            update.message,
            f"{reject_reason} Skipped: {raw_word}",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    existing = None
    if forced_type in {"nomen", "verb", "phrase"}:
        if _exists_word_for_type(forced_type, lookup_word):
            existing = {"type": forced_type, "value": lookup_word}
    elif forced_type == "praeposition":
        # spec #1: don't pre-block a new preposition meaning; the exact triple
        # duplicate is caught at save time (see _exists_word_for_type).
        existing = None
    else:
        existing = find_existing_word(get_db_path(), lookup_word)
    if existing:
        if isinstance(existing, dict) and "value" in existing:
            existing_type = existing.get("type", "word")
            existing_value = existing.get("value", "")
        else:
            existing_type = existing.get("type", "word")
            existing_value = existing.get("infinitive", "") if existing_type == "verb" else existing.get("german", "")
        if has_queue:
            logging.info("Queue skip existing word: %s", raw_word)
            return ADD_INPUT
        await _reply_text_with_retry(
            update.message,
            f"Already in database: [{existing_type}] {existing_value}",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    if not os.getenv("OPENAI_API_KEY"):
        await _reply_text_with_retry(
            update.message,
            "AI is not configured. Set OPENAI_API_KEY in .env.",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return MENU

    context.user_data["pending_word"] = raw_word
    ai_word = lookup_word
    extra_note = ""
    if forced_article:
        extra_note = f" Input already includes article '{forced_article}'. Keep this article and do not infer another one."
    if forced_type in {"nomen", "verb", "phrase"}:
        extra_note = f"{extra_note} Force part of speech: {forced_type}.".strip()
    prompt_text = _ai_prompt(ai_word, f"{clarification or ''}{extra_note}".strip())
    context.user_data["last_ai_prompt"] = prompt_text
    try:
        response_text = generate_fields(prompt_text)
    except Exception as exc:
        logging.exception("AI request failed for word: %s", raw_word)
        await _reply_text_with_retry(
            update.message,
            f"Error: AI request failed: {exc}",
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _try_parse_json(response_text)
    if entry is None or not _validate_ai_entry(entry):
        logging.warning(
            "AI parse failed word=%s clarification=%s raw=%s",
            raw_word,
            clarification,
            (response_text or "")[:500],
        )
        await _reply_text_with_retry(
            update.message,
            (
                f"Error: AI failed to parse for word: {raw_word}\n"
                f"Clarification: {clarification or '-'}\n"
                "Use Confirm, Clarify AI, clarify_fields, Reject, Ban, or Stop."
            ),
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _normalize_ai_entry(entry, _session_add_topic(context))
    if forced_type in {"nomen", "verb", "phrase"}:
        entry["type"] = forced_type
    for key, value in forced_fields.items():
        if key in entry and str(value or "").strip():
            entry[key] = str(value).strip()
    if forced_article and entry.get("type") == "nomen":
        entry["article"] = forced_article
        german = str(entry.get("german", "")).strip()
        _, german_no_article = _split_leading_article(german)
        entry["german"] = german_no_article or german
    if _is_non_infinitive_verb_input(raw_word, entry):
        inf = entry.get("infinitive", "")
        existing_inf = _exists_word_for_type("verb", inf) if inf else None
        if existing_inf:
            msg = f"❌ Verb form detected: {raw_word}. Base infinitive: {inf}. Already in database, not saved."
            if has_queue:
                await _reply_text_with_retry(update.message, msg)
                logging.info("Queue skip non-infinitive verb input (existing infinitive): %s -> %s", raw_word, inf)
                return ADD_INPUT
            await _reply_text_with_retry(
                update.message,
                msg,
                reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
            )
            return ADD_INPUT

    context.user_data["pending_entry"] = entry
    return await _show_ai_preview(update, entry)


@guard_add_handler
async def _show_ai_preview(update: Update, entry):
    """Handle show ai preview."""
    dict_lang = _user_dict_lang(update.effective_user.id)
    translation = _entry_translation(entry, dict_lang)
    has_synonym = has_translation_collision(get_db_path(), translation, dict_lang)
    preview = _format_ai_preview(entry, dict_lang, has_synonym=has_synonym)
    await _reply_text_with_retry(
        update.message,
        preview,
        reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_CONFIRM


def _session_add_topic(context):
    """The topic chosen for the current add/VF batch (spec_topics_levels.md P4).

    Decoupled from the quiz topic-filter: the add-flow no longer writes TOPIC_FILTER_KEY,
    so adding words never changes what the user is quizzed on. Default '1' as before.
    """
    return str((context.user_data or {}).get("add_topic") or "1")


def _level_reply_keyboard(user_id):
    """Reply keyboard of the six CEFR levels (+ back) for a new topic's level pick."""
    rows = [list(CEFR_LEVELS[i : i + 3]) for i in range(0, len(CEFR_LEVELS), 3)]
    rows.append([t(_user_interface_lang(user_id), "back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


@guard_add_handler
async def _finalize_add_topic(update, context, name):
    """Commit the chosen topic for the pending batch and continue (AI queue or VF job).

    The topic lives only in this session (context.user_data['add_topic']); it is NOT
    written to the quiz topic-filter (decoupled, spec_topics_levels.md P4).
    """
    context.user_data["add_topic"] = name
    context.user_data.pop("topic_return", None)
    context.user_data.pop("pending_new_topic", None)
    context.user_data.pop("pending_topic_level", None)
    then = context.user_data.pop("add_topic_then", "ai")
    if then == "vf":
        words = context.user_data.pop("pending_vf_words", [])
        from ..verb_forms_parsing.add_flow import start_vf_add_job  # local import: anti-cycle
        return await start_vf_add_job(update, context, words)
    pending = context.user_data.pop("pending_add_queue", [])
    context.user_data.pop("pending_add_stats", None)
    context.user_data["add_queue"] = pending
    return await _process_next_ai_word(update, context)


@guard_add_handler
async def _ask_topic_level(update, context, name):
    """Prompt the mandatory book-level for a freshly created topic (6 reply buttons)."""
    context.user_data["pending_topic_level"] = name
    context.user_data.pop("pending_new_topic", None)
    await update.message.reply_text(
        t(_user_interface_lang(update.effective_user.id), "wf_new_topic_level").format(name=name),
        reply_markup=_level_reply_keyboard(update.effective_user.id),
    )
    return TOPIC_FILTER_EDIT


@guard_add_handler
async def handle_add_topic_input(update, context, raw):
    """Owner add-flow topic selection (spec #8 + spec_topics_levels.md P4).

    Existing topic: selected as-is (keeps its level). New topic: name -> description ->
    mandatory book-level (A1..C2), then commit. Level is asked only for new topics.
    """
    db = get_db_path()
    # step 3: awaiting the new topic's book-level (reply button = a CEFR level)
    pending_level = context.user_data.get("pending_topic_level")
    if pending_level:
        try:
            set_topic_level(db, pending_level, raw)
        except ValueError:
            await update.message.reply_text(
                t(_user_interface_lang(update.effective_user.id), "wf_new_topic_level").format(name=pending_level),
                reply_markup=_level_reply_keyboard(update.effective_user.id),
            )
            return TOPIC_FILTER_EDIT
        return await _finalize_add_topic(update, context, pending_level)

    # step 2: awaiting the new topic's description
    pending_name = context.user_data.get("pending_new_topic")
    if pending_name:
        desc = str(raw or "").strip()
        if not desc:
            await update.message.reply_text(f"Enter description for new topic {pending_name}:")
            return TOPIC_FILTER_EDIT
        insert_topic_if_absent(db, pending_name, desc)
        return await _ask_topic_level(update, context, pending_name)

    # step 1: topic name (+ optional inline description)
    name, desc = parse_topic_input(raw)
    if not name:
        await update.message.reply_text("Send a topic name (e.g. '05 Essen' for a new one).")
        return TOPIC_FILTER_EDIT

    known = {n for n, _ in list_topics_with_desc(db)}
    if name in known:
        return await _finalize_add_topic(update, context, name)  # existing: keeps its level
    if desc:
        insert_topic_if_absent(db, name, desc)
        return await _ask_topic_level(update, context, name)
    context.user_data["pending_new_topic"] = name
    await update.message.reply_text(f"Enter description for new topic {name}:")
    return TOPIC_FILTER_EDIT


@guard_add_handler
async def prompt_add_topic(update, context):
    """Show the 'choose/create a topic' prompt for a batch (VF add reuses this).

    The caller stashes its continuation (add_topic_then + pending payload) first;
    handle_add_topic_input resumes it via _finalize_add_topic once a topic is picked.
    """
    lang = _user_interface_lang(update.effective_user.id)
    topics = ", ".join(list_topics(get_db_path())) or "-"
    await _reply_text_with_retry(
        update.message,
        f"Topics: {topics}\nChoose a topic, or type 'name description' to create a new one:",
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
    )
    context.user_data["topic_return"] = "add_queue"
    return TOPIC_FILTER_EDIT


@guard_add_handler
async def _process_next_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle process next ai word."""
    queue = context.user_data.get("add_queue") or []
    while queue:
        word = queue.pop(0)
        context.user_data["add_queue"] = queue
        state = await _process_ai_word(update, context, word, None)
        if state in (ADD_CONFIRM, ADD_CLARIFY, MENU):
            return state
        queue = context.user_data.get("add_queue") or []

    context.user_data.pop("pending_word", None)
    context.user_data.pop("pending_entry", None)
    await _reply_text_with_retry(
        update.message,
        "✅ Done.",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU


@guard_add_handler
async def _ban_pending_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban pending word."""
    word = (context.user_data.get("pending_word") or "").strip()
    if not word:
        await update.message.reply_text(
            "❌ No word to ban.",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT
    created = add_banned_word(get_db_path(), word)
    status = "✅ Added to banned list." if created else "✅ Already in banned list."
    await update.message.reply_text(f"{status} ({word})")
    context.user_data.pop("pending_entry", None)
    return await _process_next_ai_word(update, context)


@guard_add_handler
async def _stop_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stop queue."""
    context.user_data.pop("add_queue", None)
    context.user_data.pop("pending_word", None)
    context.user_data.pop("pending_entry", None)
    context.user_data.pop("edit_stage", None)
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_source", None)
    context.user_data.pop("edit_return_state", None)
    context.user_data.pop("flag_word_id", None)
    context.user_data.pop("flag_return_state", None)
    await update.message.reply_text(
        "⏹ Stopped.",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU


def _parse_precheck_edit_lines(lines):
    """Handle parse precheck edit lines."""
    result = []
    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue
        low = raw.lower()
        if low.startswith("precheck list"):
            continue
        raw = re.sub(r"^\d+\.\s*", "", raw).strip()
        if not raw:
            continue
        if "(from " in raw:
            raw = raw.split("(from ", 1)[0].strip()
        if " - " in raw:
            raw = raw.split(" - ", 1)[0].strip()
        if not raw:
            continue
        result.append(raw)
    return result


@guard_add_handler
async def add_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add submenu router (spec #2): pick recognition method.

    - AI: the current interactive flow, unchanged (prompt -> ADD_INPUT).
    - VF add / VF recheck: filled in by later spec #2 steps (stubs for now).
    - back: main menu.
    """
    user_id = update.effective_user.id
    text = update.message.text.strip()
    action = _action_key_from_text(text, user_id)

    if action == "back":
        return await _back_to_main_menu(update)

    if action == "add_ai":
        await _reply_text_with_retry(
            update.message,
            f"Send a word or a list (one per line), or upload a .txt file.\n\n{_topic_filter_banner(user_id)}",
            reply_markup=_add_input_markup(user_id),
            parse_mode="Markdown",
        )
        return ADD_INPUT

    if action == "vf_recheck":
        # VF recheck = the new .de recheck flow (spec #2 step 6): reset stale checks,
        # ask for 'all' or a list, run the same background job, produce a diff file.
        return await vf_recheck_prompt(update, context)

    if action == "add_vf":
        return await vf_add_prompt(update, context)

    await _reply_text_with_retry(
        update.message,
        "Choose: AI, VF add, VF recheck, or back.",
        reply_markup=_add_menu_markup(user_id),
    )
    return ADD_MENU


@guard_add_handler
async def add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add input."""
    raw = update.message.text.strip()
    user_id = update.effective_user.id
    if _action_key_from_text(raw, user_id) == "back":
        return await _back_to_main_menu(update)

    if raw.startswith("{"):
        entry = _try_parse_json(raw)
        if entry is None:
            await update.message.reply_text(
                "Invalid JSON. Make sure it starts with '{' and ends with '}'. Try again.",
                reply_markup=ReplyKeyboardMarkup(_add_keys(user_id), resize_keyboard=True),
            )
            return ADD_INPUT
        context.user_data["pending_entry"] = entry
        return await _show_ai_preview(update, entry)

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text("Empty input. Send a word or list.")
        return ADD_INPUT

    filtered, existing, recognition = _scan_words_before_ai(lines)
    context.user_data["precheck_filtered"] = filtered
    context.user_data["precheck_existing"] = existing
    if not recognition:
        await _reply_text_with_retry(
            update.message,
            (
                f"Already in database {len(existing)}\n"
                f"Filtred {len(filtered)}\n"
                "Nothing to recognize."
            ),
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    items = await _precheck_words(update, context, recognition)
    if items is None:
        return MENU
    for row in filtered:
        items.append({"original": row.get("raw", ""), "word": "", "type": "-", "skipped_reason": row.get("reason", "")})
    for row in existing:
        items.append({"original": row.get("raw", ""), "word": "", "type": "-", "skipped_reason": row.get("reason", "")})
    context.user_data["precheck_items"] = items
    await _reply_text_with_retry(
        update.message,
        f"```text\n{_format_precheck_list(items, _session_add_topic(context))}\n```",
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_PRECHECK


@guard_add_handler
async def add_precheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add precheck."""
    text = update.message.text.strip()
    if text == STOP_TEXT or _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)
    if text == PRECHECK_REJECT:
        context.user_data.pop("precheck_items", None)
        context.user_data.pop("precheck_filtered", None)
        context.user_data.pop("precheck_existing", None)
        await _reply_text_with_retry(
            update.message,
            "Precheck rejected. Send a word or list.",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT
    if text == PRECHECK_EDIT:
        await _reply_text_with_retry(
            update.message,
            "Send corrected list (one per line).",
            reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_PRECHECK_EDIT
    if text != PRECHECK_CONFIRM:
        await _reply_text_with_retry(
            update.message,
            "Use Approve or Reject list.",
            reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
        )
        return ADD_PRECHECK

    items = context.user_data.get("precheck_items") or []
    recognition = []
    for x in items:
        if not x.get("word"):
            continue
        recognition.append(
            {
                "raw": x.get("original", x.get("word", "")),
                "lookup_word": x.get("word", ""),
                "forced_type": x.get("forced_type", ""),
                "forced_fields": dict(x.get("forced_fields") or {}),
            }
        )
    context.user_data.pop("precheck_items", None)
    filtered = context.user_data.pop("precheck_filtered", [])
    existing = context.user_data.pop("precheck_existing", [])
    context.user_data["pending_add_queue"] = recognition
    context.user_data["pending_add_stats"] = {
        "existing": len(existing),
        "filtered": len(filtered),
    }
    await _reply_text_with_retry(
        update.message,
        (
            f"Already in database {len(existing)}\n"
            f"Filtred {len(filtered)}\n"
            f"For recognition {len(recognition)}\n"
            f"Topics: {', '.join(list_topics(get_db_path())) or '-'}\n"
            "Choose a topic, or type 'name description' to create a new one:"
        ),
        reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
    )
    context.user_data["topic_return"] = "add_queue"
    return TOPIC_FILTER_EDIT


@guard_add_handler
async def add_precheck_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add precheck edit."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await _reply_text_with_retry(
            update.message,
            "Empty list. Send corrected list (one per line).",
            reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_PRECHECK_EDIT
    recognition_raw = _parse_precheck_edit_lines(lines)
    recognition = [_parse_add_line_spec(x) for x in recognition_raw]
    recognition = [x for x in recognition if x]
    if not recognition:
        await _reply_text_with_retry(
            update.message,
            "No valid words in list.",
            reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_PRECHECK_EDIT
    context.user_data.pop("precheck_items", None)
    filtered = context.user_data.pop("precheck_filtered", [])
    existing = context.user_data.pop("precheck_existing", [])
    context.user_data["pending_add_queue"] = recognition
    context.user_data["pending_add_stats"] = {
        "existing": len(existing),
        "filtered": len(filtered),
    }
    await _reply_text_with_retry(
        update.message,
        (
            f"Already in database {len(existing)}\n"
            f"Filtred {len(filtered)}\n"
            f"For recognition {len(recognition)}\n"
            f"Topics: {', '.join(list_topics(get_db_path())) or '-'}\n"
            "Choose a topic, or type 'name description' to create a new one:"
        ),
        reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
    )
    context.user_data["topic_return"] = "add_queue"
    return TOPIC_FILTER_EDIT


@guard_add_handler
async def add_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add document."""
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file.")
        return ADD_INPUT

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text("File is empty.")
        return ADD_INPUT

    filtered, existing, recognition = _scan_words_before_ai(lines)
    context.user_data["precheck_filtered"] = filtered
    context.user_data["precheck_existing"] = existing
    if not recognition:
        await _reply_text_with_retry(
            update.message,
            (
                f"Already in database {len(existing)}\n"
                f"Filtred {len(filtered)}\n"
                "Nothing to recognize."
            ),
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    items = await _precheck_words(update, context, recognition)
    if items is None:
        return MENU
    for row in filtered:
        items.append({"original": row.get("raw", ""), "word": "", "type": "-", "skipped_reason": row.get("reason", "")})
    for row in existing:
        items.append({"original": row.get("raw", ""), "word": "", "type": "-", "skipped_reason": row.get("reason", "")})
    context.user_data["precheck_items"] = items
    await _reply_text_with_retry(
        update.message,
        f"```text\n{_format_precheck_list(items, _session_add_topic(context))}\n```",
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_PRECHECK


@guard_add_handler
async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add confirm."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)

    if text == AI_BAN_WORD:
        return await _ban_pending_word(update, context)

    if text == AI_CLARIFY_AI:
        await update.message.reply_text(
            "Send clarification text for AI.",
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    if text == AI_CLARIFY_FIELD:
        context.user_data["edit_source"] = "add"
        context.user_data["edit_stage"] = "pick_field"
        entry = context.user_data.get("pending_entry")
        if entry:
            await update.message.reply_text(_format_word_card(entry, "AI word card:", user_id=update.effective_user.id))
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_fields_markup("add"),
        )
        return ADD_EDIT

    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)

    if text != AI_CONFIRM:
        await update.message.reply_text(
            "Use: Confirm, Clarify AI, clarify_fields, Reject, Ban, or Stop.",
            reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        )
        return ADD_CONFIRM

    entry = context.user_data.pop("pending_entry", None)
    if not entry:
        await update.message.reply_text("Nothing to save.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    saved = await _save_entry(update, entry)
    if saved is None:
        return await _process_next_ai_word(update, context)

    return await _process_next_ai_word(update, context)


@guard_add_handler
async def add_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add clarify."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)
    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)
    if text == AI_CONFIRM:
        entry = context.user_data.pop("pending_entry", None)
        if not entry:
            return await _process_next_ai_word(update, context)
        saved = await _save_entry(update, entry)
        if saved is None:
            return await _process_next_ai_word(update, context)
        return await _process_next_ai_word(update, context)
    if text == AI_SKIP:
        return await _process_next_ai_word(update, context)
    if text == AI_BAN_WORD:
        return await _ban_pending_word(update, context)
    if text == AI_CLARIFY_FIELD:
        context.user_data["edit_source"] = "add"
        context.user_data["edit_stage"] = "pick_field"
        entry = context.user_data.get("pending_entry")
        if entry:
            await update.message.reply_text(_format_word_card(entry, "AI word card:", user_id=update.effective_user.id))
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_clarify_fields_markup("add"),
        )
        return ADD_EDIT
    if text == AI_CLARIFY_AI:
        await update.message.reply_text(
            "Send clarification text for AI.",
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    word = context.user_data.get("pending_word")
    if not word:
        await update.message.reply_text("No word to clarify.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    return await _process_ai_word(update, context, word, text)
