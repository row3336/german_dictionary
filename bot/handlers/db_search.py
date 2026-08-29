"""DB search feature: the search menu, list views (matches / plural-match /
weak / topics / verbs / learning-queue), free-text word lookup -> word card ->
clarify_fields entry, and the DB_SEARCH / DB_SEARCH_VIEW state handlers.
"""

import logging
import re
from datetime import date

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import ADD_EDIT, DB_SEARCH, DB_SEARCH_VIEW, MENU, get_db_path, is_owner
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..common.settings import _learning_daily_qty, _learning_schedule_seconds
from ..common.topics import sorted_topics
from ..i18n import t
from ..storage import (
    _safe_text,
    get_word_by_id,
    map_synonym_group_ids,
    learning_add_word_by_id,
    learning_get_or_create_batch,
    list_all_words,
    list_weak_form_words,
    list_learning_queue_words,
    list_topics,
    list_words_by_topic,
    search_word,
)
from ..text_utils import cap_word as _cap_word
from ..text_utils import low_word as _low_word
from ..text_utils import md_escape as _md_escape
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import normalize_lookup_text as _normalize_lookup_text
from ..text_utils import split_text_chunks as _split_text_chunks
from ..text_utils import type_label as _type_label
from ..ui.formatting import (
    _entry_translation,
    _expected_partizip2,
    _case_label,
    _format_topic_learning_line,
    _format_word_card,
    _preposition_hint_parts,
)
from ..ui.keyboards import (
    _clarify_fields_markup,
    _db_search_variants_markup,
    _main_menu_markup,
)


logger = logging.getLogger(__name__)

_EXACT_VARIANTS_KEY = "db_search_exact_variants"


def _db_search_markup(user_id):
    """Handle db search markup."""
    lang = _user_interface_lang(user_id)
    actions = [
        t(lang, "db_search_matches"),
        t(lang, "db_search_plural_match"),
        t(lang, "db_search_topic_words"),
        t(lang, "db_search_topics_lern"),
        t(lang, "db_search_weak_words"),
    ]
    rows = [actions[i:i + 4] for i in range(0, len(actions), 4)]
    back = t(lang, "back")
    if rows and len(rows[-1]) < 4:
        rows[-1].append(back)
    else:
        rows.append([back])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _db_search_words_lists_markup(user_id):
    """Handle words lists submenu markup."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup(
        [[t(lang, "db_search_topics_lists"), t(lang, "db_search_type_lists"), t(lang, "back")]],
        resize_keyboard=True,
    )


def _db_search_weak_markup(user_id):
    """Weak-words list keyboard: "send to learning" + back."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup([[t(lang, "send_learn"), t(lang, "back")]], resize_keyboard=True)


def _db_search_topic_markup(user_id):
    """Handle db search topic selector markup."""
    lang = _user_interface_lang(user_id)
    topics = sorted_topics(list_topics(get_db_path()))
    rows = []
    row = []
    for item in topics:
        row.append(str(item))
        if len(row) == 4:
            rows.append(row)
            row = []
    back = t(lang, "back")
    if row:
        row.append(back)
        rows.append(row)
    else:
        rows.append([back])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True), topics


def _db_search_type_markup(user_id):
    """Type selector markup: every word type present in the DB (4 per row) + back."""
    lang = _user_interface_lang(user_id)
    counts = {}
    for row in list_all_words(get_db_path()):
        tp = (row.get("type") or "").strip().lower()
        if tp:
            counts[tp] = counts.get(tp, 0) + 1
    types = sorted(counts, key=lambda tp: (-counts[tp], tp))
    rows = []
    row = []
    for tp in types:
        row.append(tp)
        if len(row) == 4:
            rows.append(row)
            row = []
    back = t(lang, "back")
    if row:
        row.append(back)
        rows.append(row)
    else:
        rows.append([back])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True), types


def _format_topic_words_line(entry):
    """Handle format topic words line (base form only)."""
    entry_type = str(entry.get("type", "")).strip().lower()
    if entry_type == "verb":
        base = str(entry.get("infinitive", "")).strip()
    else:
        base = str(entry.get("german", "")).strip()
    if entry_type == "nomen":
        article = str(entry.get("article", "")).strip()
        base = f"{article} {base}".strip() if article else base
    if entry_type == "praeposition":
        parts = [entry_type]
        ptype = str(entry.get("preposition_type", "")).strip()
        if ptype:
            parts.append(ptype)
        label = _case_label(entry.get("case_type", ""))
        if label:
            parts.append(label)
        return f"{base} [{', '.join(parts)}]"
    return f"{base} [{entry_type}]"


def _format_db_search_card(entry, dict_lang):
    """Handle format db search card."""
    word_type = _low_word(entry.get("type", ""))
    translation = _entry_translation(entry, dict_lang)
    lines = []
    if word_type == "nomen":
        article = str(entry.get("article", "")).strip()
        german = _cap_word(entry.get("german", ""))
        left = f"{article} {german}".strip() if article else german
        if translation:
            lines.append(f"{_md_escape(left)} - {_md_escape(translation)}")
        else:
            lines.append(_md_escape(left))
        plural = _cap_word(entry.get("plural", ""))
        if plural:
            lines.append("")
            lines.append(f"_pl. die {_md_escape(plural)}_")
        return "\n".join(lines)
    if word_type == "verb":
        infinitive = _low_word(entry.get("infinitive", ""))
        if translation:
            lines.append(f"{_md_escape(infinitive)} - {_md_escape(translation)}")
        else:
            lines.append(_md_escape(infinitive))
        pr = _low_word(entry.get("prateritum", ""))
        p2 = _expected_partizip2(entry)
        forms = ", ".join([x for x in [pr, p2] if x])
        if forms:
            lines.append("")
            lines.append(f"_{_md_escape(forms)}_")
        return "\n".join(lines)
    if word_type == "adjektiv":
        german = _low_word(entry.get("german", ""))
        if translation:
            lines.append(f"{_md_escape(german)} - {_md_escape(translation)}")
        else:
            lines.append(_md_escape(german))
        komp = _low_word(entry.get("komparativ", ""))
        supr = _low_word(entry.get("superlativ", ""))
        forms = ", ".join([x for x in [komp, supr] if x])
        if forms:
            lines.append("")
            lines.append(f"_  {_md_escape(forms)}_")
        return "\n".join(lines)
    german = _low_word(entry.get("german", ""))
    if translation:
        return f"{_md_escape(german)} - {_md_escape(translation)}"
    return _md_escape(german)


def _select_db_search_exact(token):
    """Return every exact match in the same priority tier as the old primary."""
    candidate = _normalize_lookup_text(token)
    if not candidate:
        return []
    rows = list_all_words(get_db_path())
    exact_non_phrase = []
    exact_phrase = []
    for row in rows:
        entry_type = _type_label(row.get("type", ""))
        forms = {
            _normalize_lookup_text(row.get("german", "")),
            _normalize_lookup_text(row.get("infinitive", "")),
            _normalize_lookup_text(row.get("plural", "")),
            _normalize_lookup_text(row.get("prateritum", "")),
            _normalize_lookup_text(row.get("partizip2", "")),
            _normalize_lookup_text(row.get("russian", "")),
            _normalize_lookup_text(row.get("ukrainian", "")),
            _normalize_lookup_text(row.get("english", "")),
        }
        if candidate in forms:
            if entry_type == "phrase":
                exact_phrase.append(row)
            else:
                exact_non_phrase.append(row)
    if exact_non_phrase:
        return exact_non_phrase
    if exact_phrase:
        return exact_phrase
    return []


def _select_db_search_primary(token):
    """Pick the legacy single primary while keeping LIKE fallback unchanged."""
    exact = _select_db_search_exact(token)
    if exact:
        return exact[0]
    return search_word(get_db_path(), token)


def _is_semantic_preposition_set(entries):
    """Only ambiguous exact preposition meanings enter the selection flow."""
    return len(entries) > 1 and all(
        _type_label(entry.get("type", "")) == "praeposition" for entry in entries
    )


def _format_semantic_variant_label(entry):
    """Compact semantic label built from the existing preposition normalizers."""
    parts = [
        "+D/A" if part == "+D/A (Wechsel)" else part
        for part in _preposition_hint_parts(entry)
    ]
    return f"{_low_word(entry.get('german', ''))} [{', '.join(parts)}]"


def _build_semantic_variant_state(entries, query):
    """Build a collision-free label-to-id map, or return collision details."""
    label_to_id = {}
    for entry in entries:
        label = _format_semantic_variant_label(entry)
        word_id = entry.get("id")
        if not word_id or label in label_to_id:
            return None, {
                "label": label,
                "word_ids": [label_to_id.get(label), word_id],
            }
        label_to_id[label] = word_id
    return {
        "query": str(query),
        "label_to_id": label_to_id,
        "word_ids": list(label_to_id.values()),
    }, None


def _clear_db_search_variants(context):
    """Drop the per-user ambiguity state when leaving its selection flow."""
    context.user_data.pop(_EXACT_VARIANTS_KEY, None)


async def _show_saved_db_search_variants(update, context, message_key=None):
    """Render only the saved labels; this path deliberately performs no DB query."""
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    state = context.user_data.get(_EXACT_VARIANTS_KEY) or {}
    label_to_id = state.get("label_to_id")
    labels = list(label_to_id) if isinstance(label_to_id, dict) else []
    if not labels:
        _clear_db_search_variants(context)
        return await _send_db_search_prompt(
            update, user_id, t(lang, "db_search_variant_unavailable")
        )
    await update.message.reply_text(
        t(lang, message_key or "db_search_choose_variant"),
        reply_markup=_db_search_variants_markup(labels, user_id),
    )
    return DB_SEARCH


def _find_phrase_side_matches(token, primary_entry):
    """Collect phrase matches containing token excluding primary card."""
    candidate = _normalize_lookup_text(token)
    if not candidate:
        return []
    primary_id = (primary_entry or {}).get("id")
    out = []
    for row in list_all_words(get_db_path()):
        if _type_label(row.get("type", "")) != "phrase":
            continue
        if primary_id and row.get("id") == primary_id:
            continue
        phrase = _normalize_lookup_text(row.get("german", ""))
        if not phrase:
            continue
        if candidate in phrase:
            out.append(row)
    return out


async def _send_db_search_card(update, context, entry, query, show_variants=False):
    """Render a chosen card and enter the existing clarify/full-card flow."""
    user_id = update.effective_user.id
    phrase_matches = _find_phrase_side_matches(query, entry)
    await update.message.reply_text(
        f"```text\n{_format_word_card(entry, title='Word card:', user_id=user_id)}\n```",
        parse_mode="Markdown",
    )
    if phrase_matches:
        lines = ["Phrase matches:"]
        for item in phrase_matches:
            phrase = _safe_text(item.get("german", "")).strip()
            trans = _entry_translation(item, _user_dict_lang(user_id))
            lines.append(f"- {phrase} -> {trans}" if trans else f"- {phrase}")
        await update.message.reply_text(
            f"```text\n{chr(10).join(lines)}\n```", parse_mode="Markdown"
        )
    context.user_data["quiz_entry"] = entry
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_source"] = "quiz"
    context.user_data["edit_stage"] = "pick_field"
    context.user_data["edit_return_state"] = DB_SEARCH_VIEW

    from .clarify_fields import _is_prep_entry

    await update.message.reply_text(
        t(_user_interface_lang(user_id), "clarify_choose_action"),
        reply_markup=_clarify_fields_markup(
            "quiz",
            user_id,
            in_search=True,
            is_prep=_is_prep_entry(entry),
            show_variants=show_variants,
        ),
    )
    return ADD_EDIT


async def _open_saved_db_search_variant(update, context):
    """Resolve a button label to its saved id, then fetch only that exact id."""
    state = context.user_data.get(_EXACT_VARIANTS_KEY) or {}
    label_to_id = state.get("label_to_id")
    saved_ids = state.get("word_ids")
    if not isinstance(label_to_id, dict) or not isinstance(saved_ids, list):
        return await _show_saved_db_search_variants(
            update, context, "db_search_variant_unavailable"
        )
    selected_label = update.message.text.strip()
    word_id = label_to_id.get(selected_label)
    if word_id not in saved_ids:
        return await _show_saved_db_search_variants(
            update, context, "db_search_variant_unavailable"
        )
    entry = get_word_by_id(get_db_path(), word_id)
    if (
        not entry
        or entry.get("id") != word_id
        or _type_label(entry.get("type", "")) != "praeposition"
        or _format_semantic_variant_label(entry) != selected_label
    ):
        return await _show_saved_db_search_variants(
            update, context, "db_search_variant_unavailable"
        )
    return await _send_db_search_card(
        update, context, entry, state.get("query", ""), show_variants=True
    )


def _format_match_line(entry, dict_lang):
    """Handle format match line."""
    entry_type = _low_word(entry.get("type", ""))
    translation = _entry_translation(entry, dict_lang)
    if entry_type == "nomen":
        article = str(entry.get("article", "")).strip()
        german = _cap_word(entry.get("german", ""))
        plural = _cap_word(entry.get("plural", ""))
        parts = [entry_type, "-", article, german, plural, translation]
        return " ".join(p for p in parts if p)
    if entry_type == "verb":
        infinitive = _low_word(entry.get("infinitive", ""))
        pr = _low_word(entry.get("prateritum", ""))
        p2 = _low_word(entry.get("partizip2", ""))
        parts = [entry_type, "-", infinitive, pr, p2, translation]
        return " ".join(p for p in parts if p)
    if entry_type == "adjektiv":
        german = _low_word(entry.get("german", ""))
        komp = _low_word(entry.get("komparativ", ""))
        supr = _low_word(entry.get("superlativ", ""))
        parts = [entry_type, "-", german, komp, supr, translation]
        return " ".join(p for p in parts if p)
    german = _low_word(entry.get("german", ""))
    parts = [entry_type, "-", german, translation]
    return " ".join(p for p in parts if p)


def _list_match_pairs(entries, dict_lang):
    """Handle list match pairs."""
    if not entries:
        return []
    # One query for all group ids: grouping criterion #3 (below) and the
    # "✅ linked" tag both need them for every entry.
    syn_groups = map_synonym_group_ids(get_db_path())
    by_base = {}
    by_translation = {}
    by_syn_group = {}
    for entry in entries:
        entry_type = _low_word(entry.get("type", ""))
        if entry_type == "verb":
            base = _normalize_lookup_text(entry.get("infinitive", ""))
        else:
            base = _normalize_lookup_text(entry.get("german", ""))
        if base:
            base_key = (entry_type or "word", base)
            if entry_type == "praeposition":
                # spec #1: same-spelling prepositions with a different
                # (preposition_type, case_type) are intentional distinct words, not
                # duplicates — fold both into the key so they don't surface as a
                # "match pair". Only an exact triple match still groups (real dup).
                base_key = (
                    entry_type,
                    base,
                    _normalize_lookup_text(entry.get("preposition_type", "")),
                    _normalize_lookup_text(entry.get("case_type", "")),
                )
            by_base.setdefault(base_key, []).append(entry)
        translation = _normalize_lookup_text(_entry_translation(entry, dict_lang))
        if translation:
            by_translation.setdefault(("translation", translation), []).append(entry)
        # T3: manually linked synonyms (senden/schicken) share neither spelling
        # nor translation text, so the two criteria above never surface them —
        # the synonym group itself is a third grouping criterion.
        gid = syn_groups.get(entry.get("id"))
        if gid:
            by_syn_group.setdefault(gid, []).append(entry)

    groups = []
    for group in by_base.values():
        if len(group) > 1:
            groups.append(group)
    for group in by_translation.values():
        if len(group) > 1:
            groups.append(group)
    for group in by_syn_group.values():
        if len(group) > 1:
            groups.append(group)

    blocks = []
    seen_pairs = set()
    for group in groups:
        for idx in range(0, len(group), 2):
            pair = group[idx:idx + 2]
            if len(pair) < 2:
                continue
            # The same pair can qualify by translation AND by synonym group —
            # show it once.
            pair_key = frozenset((pair[0].get("id"), pair[1].get("id")))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            lines = [_format_match_line(pair[0], dict_lang), _format_match_line(pair[1], dict_lang)]
            gid_a = syn_groups.get(pair[0].get("id"))
            gid_b = syn_groups.get(pair[1].get("id"))
            if gid_a and gid_b and gid_a == gid_b:
                lines.append("✅ linked")
            blocks.append("\n".join(lines))
    return blocks


def _format_weak_form_line(entry):
    """spec #6: `word — weak forms` (e.g. `Haus — artikel, plural`)."""
    # Nouns are capitalized (German orthography); verbs stay lowercase.
    if _low_word(entry.get("type", "")) == "nomen":
        word = _cap_word(entry.get("german") or "")
    else:
        word = entry.get("german") or entry.get("infinitive") or ""
    forms = ", ".join(entry.get("weak_forms") or [])
    return f"{word} — {forms}"


def _list_filename(list_name):
    """db_<list>_<date>.txt with a filesystem-safe list token."""
    safe = re.sub(r"[^\w.-]+", "_", str(list_name).strip()) or "list"
    return f"db_{safe}_{date.today().isoformat()}.txt"


async def _send_lines_or_file(message, body, list_name, markup, caption=None):
    """Send a db_search listing inline or as a file (T6, owner decision 11.07).

    Short output (fits one message) stays inline as before; anything that would
    need 2+ messages goes as ONE .txt document instead of a message series
    (rate-limiter friendly, easier to scroll on a PC). The reply keyboard rides
    on the same message either way."""
    chunks = _split_text_chunks(body)
    if len(chunks) <= 1:
        await message.reply_text(
            f"```text\n{body}\n```",
            reply_markup=markup,
            parse_mode="Markdown",
        )
        return
    await message.reply_document(
        document=body.encode("utf-8"),
        filename=_list_filename(list_name),
        caption=caption or body.split("\n", 1)[0].strip()[:200],
        reply_markup=markup,
    )


async def _send_db_search_prompt(update: Update, user_id, prefix_text=None):
    """Handle send db search prompt."""
    text = t(_user_interface_lang(user_id), "search_prompt")
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(text, reply_markup=_db_search_markup(user_id))
    return DB_SEARCH


async def db_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle input/buttons in DB search (queries and search sub-modes)."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if context.user_data.get(_EXACT_VARIANTS_KEY):
        if action == "back":
            _clear_db_search_variants(context)
            return await _send_db_search_prompt(update, user_id, None)
        return await _open_saved_db_search_variant(update, context)
    in_words_lists = bool(context.user_data.get("db_search_words_lists_menu"))
    if (context.user_data.get("db_search_topic_pick") or context.user_data.get("db_search_type_pick")) and action == "back":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_type_pick", None)
        if in_words_lists:
            await update.message.reply_text(t(_user_interface_lang(user_id), "words_lists"), reply_markup=_db_search_words_lists_markup(user_id))
            return DB_SEARCH
        return await _send_db_search_prompt(update, user_id, None)
    if action == "back":
        if in_words_lists:
            context.user_data.pop("db_search_words_lists_menu", None)
            return await _send_db_search_prompt(update, user_id, None)
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        await update.message.reply_text(t(_user_interface_lang(user_id), "main_menu"), reply_markup=_main_menu_markup(user_id))
        return MENU
    if context.user_data.get("db_search_topic_pick"):
        markup, topics = _db_search_topic_markup(user_id)
        if text not in topics:
            await update.message.reply_text(t(_user_interface_lang(user_id), "choose_topic"), reply_markup=markup)
            return DB_SEARCH
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_words_by_topic(get_db_path(), text)
        if not entries:
            await update.message.reply_text(t(_user_interface_lang(user_id), "no_words_in_topic").format(topic=text), reply_markup=markup)
            return DB_SEARCH
        lines = [f"topic {text}: {len(entries)} words", ""]
        lines.extend(_format_topic_words_line(entry) for entry in entries)
        await _send_lines_or_file(update.message, "\n".join(lines), f"topic_{text}", markup)
        return DB_SEARCH
    if context.user_data.get("db_search_type_pick"):
        markup, types = _db_search_type_markup(user_id)
        chosen = text.strip().lower()
        if chosen not in types:
            await update.message.reply_text(t(_user_interface_lang(user_id), "choose_type"), reply_markup=markup)
            return DB_SEARCH
        context.user_data.pop("db_search_weak_ids", None)
        entries = [x for x in list_all_words(get_db_path()) if (x.get("type") or "").strip().lower() == chosen]
        if not entries:
            await update.message.reply_text(t(_user_interface_lang(user_id), "no_type_words").format(type=chosen), reply_markup=markup)
            return DB_SEARCH
        lines = [f"{chosen}: {len(entries)} words", ""]
        lines.extend(_format_topic_words_line(entry) for entry in entries)
        await _send_lines_or_file(update.message, "\n".join(lines), f"type_{chosen}", markup)
        return DB_SEARCH
    if action == "db_search_topics_lern":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_learning_queue_words(get_db_path(), user_id=user_id)
        if not entries:
            return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "no_queued_learning"))
        lines = [f"learning queue: {len(entries)} words", ""]
        lines.extend(_format_topic_learning_line(entry) for entry in entries)
        await _send_lines_or_file(update.message, "\n".join(lines), "learning_queue", _db_search_markup(user_id))
        return DB_SEARCH
    if action == "send_learn" and context.user_data.get("db_search_weak_ids"):
        ids = list(dict.fromkeys(context.user_data.get("db_search_weak_ids") or []))
        db_path = get_db_path()
        batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=_learning_daily_qty(db_path, user_id))
        added = 0
        for wid in ids:
            added += int(
                learning_add_word_by_id(
                    db_path,
                    user_id=user_id,
                    word_id=wid,
                    batch_id=batch_id,
                    first_delay_seconds=_learning_schedule_seconds(db_path, user_id)[0],
                )
            )
        context.user_data.pop("db_search_weak_ids", None)
        return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "added_to_learning_n").format(added=added))
    if action == "db_search_matches":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_all_words(get_db_path())
        blocks = _list_match_pairs(entries, _user_dict_lang(user_id))
        if not blocks:
            return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "no_matches_found"))
        await _send_lines_or_file(
            update.message, "\n\n".join(blocks), "matches", _db_search_markup(user_id),
            caption=f"matches: {len(blocks)} pairs",
        )
        return DB_SEARCH
    if action == "db_search_plural_match":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        entries = []
        for row in list_all_words(get_db_path()):
            if (row.get("type") or "").strip().lower() != "nomen":
                continue
            sg = _normalize_answer(row.get("german", ""))
            pl = _normalize_answer(row.get("plural", ""))
            if sg and pl and sg == pl:
                entries.append(row)
        if not entries:
            return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "no_plural_singular"))
        lines = [f"plural_match: {len(entries)}", ""]
        for e in entries:
            article = (e.get("article") or "").strip()
            german = _cap_word(e.get("german", ""))
            lines.append(f"{article} {german}".strip())
        await _send_lines_or_file(update.message, "\n".join(lines), "plural_match", _db_search_markup(user_id))
        return DB_SEARCH
    if action == "db_search_weak_words":
        context.user_data.pop("db_search_topic_pick", None)
        # spec #6: real weak words = active weak-form episodes (#18A), not FSRS streaks.
        entries = list_weak_form_words(get_db_path(), user_id=user_id)
        if not entries:
            context.user_data.pop("db_search_weak_ids", None)
            return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "no_weak_words"))
        context.user_data["db_search_weak_ids"] = [x.get("id") for x in entries if x.get("id")]
        lines = [_format_weak_form_line(entry) for entry in entries]
        await _send_lines_or_file(
            update.message, "\n\n".join(lines), "weak_words", _db_search_weak_markup(user_id),
            caption=f"weak words: {len(entries)}",
        )
        return DB_SEARCH
    if action == "db_search_topic_words":
        context.user_data["db_search_words_lists_menu"] = True
        await update.message.reply_text(t(_user_interface_lang(user_id), "words_lists"), reply_markup=_db_search_words_lists_markup(user_id))
        return DB_SEARCH
    if action == "db_search_topics_lists":
        context.user_data.pop("db_search_weak_ids", None)
        context.user_data["db_search_words_lists_menu"] = True
        markup, topics = _db_search_topic_markup(user_id)
        if not topics:
            await update.message.reply_text(t(_user_interface_lang(user_id), "no_topics_in_db"), reply_markup=_db_search_words_lists_markup(user_id))
            return DB_SEARCH
        context.user_data["db_search_topic_pick"] = True
        await update.message.reply_text(t(_user_interface_lang(user_id), "choose_topic"), reply_markup=markup)
        return DB_SEARCH
    if action == "db_search_type_lists":
        context.user_data.pop("db_search_weak_ids", None)
        context.user_data["db_search_words_lists_menu"] = True
        markup, types = _db_search_type_markup(user_id)
        if not types:
            await update.message.reply_text(t(_user_interface_lang(user_id), "no_words_in_db"), reply_markup=_db_search_words_lists_markup(user_id))
            return DB_SEARCH
        context.user_data["db_search_type_pick"] = True
        await update.message.reply_text(t(_user_interface_lang(user_id), "choose_type"), reply_markup=markup)
        return DB_SEARCH
    context.user_data.pop("db_search_topic_pick", None)
    context.user_data.pop("db_search_type_pick", None)
    context.user_data.pop("db_search_words_lists_menu", None)
    context.user_data.pop("db_search_weak_ids", None)
    exact = _select_db_search_exact(text)
    if _is_semantic_preposition_set(exact):
        variant_state, collision = _build_semantic_variant_state(exact, text)
        if collision:
            _clear_db_search_variants(context)
            logger.warning(
                "db_search semantic variant label collision label=%r word_ids=%r",
                collision["label"],
                collision["word_ids"],
            )
            await update.message.reply_text(
                t(_user_interface_lang(user_id), "db_search_variant_collision"),
                reply_markup=_db_search_markup(user_id),
            )
            return DB_SEARCH
        context.user_data[_EXACT_VARIANTS_KEY] = variant_state
        return await _show_saved_db_search_variants(update, context)

    _clear_db_search_variants(context)
    entry = exact[0] if exact else search_word(get_db_path(), text)
    if not entry:
        return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "not_found"))
    return await _send_db_search_card(update, context, entry, text)


async def db_search_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buttons in the DB-search results view mode."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action in {"back", "stop"}:
        context.user_data.pop("db_search_words_lists_menu", None)
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_type_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        return await _send_db_search_prompt(update, user_id, None)
    if action in {
        "db_search_matches",
        "db_search_plural_match",
        "db_search_weak_words",
        "db_search_topic_words",
        "db_search_topics_lists",
        "db_search_type_lists",
        "db_search_topics_lern",
    }:
        return await db_search_input(update, context)
    # Any other text is treated as a new search query. (Editing a found word now
    # happens on the unified card screen handled by the ADD_EDIT clarify handler.)
    return await db_search_input(update, context)
