"""DB search feature: the search menu, list views (matches / plural-match /
topics / verbs / learning-queue), free-text word lookup -> word card ->
clarify_fields entry, and the DB_SEARCH / DB_SEARCH_VIEW state handlers.
"""

import re
from datetime import date

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import CASE_TYPES, DB_SEARCH, DB_SEARCH_VIEW, MENU, PREPOSITION_TYPES, get_db_path, is_owner
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..common.settings import _learning_daily_qty, _learning_schedule_seconds
from ..common.topics import sorted_topics
from ..i18n import t
from ..storage import (
    map_synonym_group_ids,
    learning_add_word_by_id,
    learning_get_or_create_batch,
    list_all_words,
    list_learning_queue_words,
    list_topics,
    list_words_by_topic,
    search_entries,
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
)
from ..ui.keyboards import (
    _main_menu_markup,
)


_EXACT_VARIANTS_KEY = "db_search_exact_variants"


def _db_search_markup(user_id):
    """Handle db search markup."""
    lang = _user_interface_lang(user_id)
    actions = [
        t(lang, "db_search_matches"),
        t(lang, "db_search_plural_match"),
        t(lang, "db_search_topic_words"),
        t(lang, "db_search_topics_lern"),
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


def _clear_db_search_variants(context):
    """Drop the per-user ambiguity state when leaving its selection flow."""
    context.user_data.pop(_EXACT_VARIANTS_KEY, None)


def _is_legacy_db_search_control(text):
    """Recognize the retired keyboard grammar without treating all brackets as UI."""
    if text in {t(lang, "db_search_back_to_variants") for lang in ("en", "ru", "uk")}:
        return True
    match = re.fullmatch(r".+ \[([^\[\]]*)\]", text)
    if not match:
        return False
    cases = {_case_label(case).replace("+D/A (Wechsel)", "+D/A") for case in CASE_TYPES}
    suffixes = {"", *PREPOSITION_TYPES, *cases}
    suffixes.update(f"{kind}, {case}" for kind in PREPOSITION_TYPES for case in cases)
    return match.group(1) in suffixes


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
    if entry_type == "phrase":
        german = str(entry.get("german", "") or "").strip()
        parts = [entry_type, "-", german, translation]
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
    if _is_legacy_db_search_control(text):
        await update.message.reply_text(t(_user_interface_lang(user_id), "search_stale"))
        return None
    if context.user_data.get(_EXACT_VARIANTS_KEY):
        _clear_db_search_variants(context)
        return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "search_stale"))
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
        await update.message.reply_text(t(_user_interface_lang(user_id), "main_menu"), reply_markup=_main_menu_markup(user_id))
        return MENU
    if context.user_data.get("db_search_topic_pick"):
        markup, topics = _db_search_topic_markup(user_id)
        if text not in topics:
            await update.message.reply_text(t(_user_interface_lang(user_id), "choose_topic"), reply_markup=markup)
            return DB_SEARCH
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
        from ..study.repository import transaction
        from ..common.word_filter import resolve_quiz_filter
        from ..learning_requests import eligible_pool
        with transaction(get_db_path()) as conn:
            selected_filter = resolve_quiz_filter(user_id, conn=conn)
            entries = eligible_pool(conn, user_id, selected_filter.topics, selected_filter.cefr_levels)
        if not entries:
            return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "no_queued_learning"))
        lines = [f"learning queue: {len(entries)} words", ""]
        lines.extend(_format_topic_learning_line(entry) for entry in entries)
        await _send_lines_or_file(update.message, "\n".join(lines), "learning_queue", _db_search_markup(user_id))
        return DB_SEARCH
    if action == "db_search_matches":
        context.user_data.pop("db_search_topic_pick", None)
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
    if action == "db_search_topic_words":
        context.user_data["db_search_words_lists_menu"] = True
        await update.message.reply_text(t(_user_interface_lang(user_id), "words_lists"), reply_markup=_db_search_words_lists_markup(user_id))
        return DB_SEARCH
    if action == "db_search_topics_lists":
        context.user_data["db_search_words_lists_menu"] = True
        markup, topics = _db_search_topic_markup(user_id)
        if not topics:
            await update.message.reply_text(t(_user_interface_lang(user_id), "no_topics_in_db"), reply_markup=_db_search_words_lists_markup(user_id))
            return DB_SEARCH
        context.user_data["db_search_topic_pick"] = True
        await update.message.reply_text(t(_user_interface_lang(user_id), "choose_topic"), reply_markup=markup)
        return DB_SEARCH
    if action == "db_search_type_lists":
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
    _clear_db_search_variants(context)
    from ..common.search_view import new_snapshot, send_page
    result = search_entries(get_db_path(), text)
    new_snapshot(context, user_id, update.effective_chat.id, text, result)
    if result["kind"] == "empty":
        return await _send_db_search_prompt(update, user_id, t(_user_interface_lang(user_id), "not_found"))
    return await send_page(update.message, context)


async def db_search_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buttons in the DB-search results view mode."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action in {"back", "stop"}:
        context.user_data.pop("db_search_words_lists_menu", None)
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_type_pick", None)
        return await _send_db_search_prompt(update, user_id, None)
    if action in {
        "db_search_matches",
        "db_search_plural_match",
        "db_search_topic_words",
        "db_search_topics_lists",
        "db_search_type_lists",
        "db_search_topics_lern",
    }:
        return await db_search_input(update, context)
    # Any other text is treated as a new search query. (Editing a found word now
    # happens on the unified card screen handled by the ADD_EDIT clarify handler.)
    return await db_search_input(update, context)
