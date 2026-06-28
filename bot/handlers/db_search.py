"""DB search feature: the search menu, list views (matches / plural-match /
weak / topics / verbs / learning-queue), free-text word lookup -> word card ->
clarify_fields entry, and the DB_SEARCH / DB_SEARCH_VIEW state handlers.
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import DB_SEARCH, DB_SEARCH_VIEW, MENU, get_db_path, is_owner
from ..common.lang import _mode_l1_de, _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..common.settings import _learning_daily_qty, _learning_schedule_seconds
from ..handlers.clarify_fields import clarify_fields_input
from ..i18n import t
from ..storage import (
    _safe_text,
    get_synonym_group_id,
    learning_add_word_by_id,
    learning_get_or_create_batch,
    list_all_words,
    list_fsrs_weak_words,
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
    _format_topic_learning_line,
    _format_word_card,
)
from ..ui.keyboards import _clarify_root_markup, _main_menu_markup


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


def _topic_sort_key(token):
    """Handle topic sort key with numeric priority."""
    value = str(token or "").strip()
    if value.isdigit():
        return (0, int(value), value)
    return (1, value.lower(), value)


def _db_search_topic_markup(user_id):
    """Handle db search topic selector markup."""
    lang = _user_interface_lang(user_id)
    topics = sorted(list_topics(get_db_path()), key=_topic_sort_key)
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


def _select_db_search_primary(token):
    """Pick primary DB-search entry preferring non-phrase exact matches."""
    candidate = _normalize_lookup_text(token)
    if not candidate:
        return None
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
        return exact_non_phrase[0]
    if exact_phrase:
        return exact_phrase[0]
    return search_word(get_db_path(), token)


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
    by_base = {}
    by_translation = {}
    for entry in entries:
        entry_type = _low_word(entry.get("type", ""))
        if entry_type == "verb":
            base = _normalize_lookup_text(entry.get("infinitive", ""))
        else:
            base = _normalize_lookup_text(entry.get("german", ""))
        if base:
            by_base.setdefault((entry_type or "word", base), []).append(entry)
        translation = _normalize_lookup_text(_entry_translation(entry, dict_lang))
        if translation:
            by_translation.setdefault(("translation", translation), []).append(entry)

    groups = []
    for group in by_base.values():
        if len(group) > 1:
            groups.append(group)
    for group in by_translation.values():
        if len(group) > 1:
            groups.append(group)

    blocks = []
    group_cache = {}
    for group in groups:
        for idx in range(0, len(group), 2):
            pair = group[idx:idx + 2]
            if len(pair) < 2:
                continue
            lines = [_format_match_line(pair[0], dict_lang), _format_match_line(pair[1], dict_lang)]
            gid_a = group_cache.get(pair[0].get("id"))
            if gid_a is None:
                gid_a = get_synonym_group_id(get_db_path(), pair[0].get("id"))
                group_cache[pair[0].get("id")] = gid_a
            gid_b = group_cache.get(pair[1].get("id"))
            if gid_b is None:
                gid_b = get_synonym_group_id(get_db_path(), pair[1].get("id"))
                group_cache[pair[1].get("id")] = gid_b
            if gid_a and gid_b and gid_a == gid_b:
                lines.append("✅ linked")
            blocks.append("\n".join(lines))
    return blocks


def _format_fsrs_weak_line(entry, dict_lang):
    """Handle format fsrs weak line."""
    streak = int(entry.get("wrong_streak") or 0)
    return f"{_format_match_line(entry, dict_lang)} | wrong_streak={streak}"


async def _send_db_search_prompt(update: Update, user_id, prefix_text=None):
    """Handle send db search prompt."""
    text = "Send a word to search in DB:"
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(text, reply_markup=_db_search_markup(user_id))
    return DB_SEARCH


async def db_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle input/buttons in DB search (queries and search sub-modes)."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    in_words_lists = bool(context.user_data.get("db_search_words_lists_menu"))
    if (context.user_data.get("db_search_topic_pick") or context.user_data.get("db_search_type_pick")) and action == "back":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_type_pick", None)
        if in_words_lists:
            await update.message.reply_text("words_lists:", reply_markup=_db_search_words_lists_markup(user_id))
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
            await update.message.reply_text("Choose topic:", reply_markup=markup)
            return DB_SEARCH
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_words_by_topic(get_db_path(), text)
        if not entries:
            await update.message.reply_text(f"No words in topic {text}.", reply_markup=markup)
            return DB_SEARCH
        lines = [f"topic {text}: {len(entries)} words", ""]
        lines.extend(_format_topic_words_line(entry) for entry in entries)
        chunks = _split_text_chunks("\n".join(lines))
        for idx, chunk in enumerate(chunks):
            reply_markup = markup if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        return DB_SEARCH
    if context.user_data.get("db_search_type_pick"):
        markup, types = _db_search_type_markup(user_id)
        chosen = text.strip().lower()
        if chosen not in types:
            await update.message.reply_text("Choose type:", reply_markup=markup)
            return DB_SEARCH
        context.user_data.pop("db_search_weak_ids", None)
        entries = [x for x in list_all_words(get_db_path()) if (x.get("type") or "").strip().lower() == chosen]
        if not entries:
            await update.message.reply_text(f"No {chosen} words.", reply_markup=markup)
            return DB_SEARCH
        lines = [f"{chosen}: {len(entries)} words", ""]
        lines.extend(_format_topic_words_line(entry) for entry in entries)
        chunks = _split_text_chunks("\n".join(lines))
        for idx, chunk in enumerate(chunks):
            reply_markup = markup if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        return DB_SEARCH
    if action == "db_search_topics_lern":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_learning_queue_words(get_db_path(), user_id=user_id)
        if not entries:
            return await _send_db_search_prompt(update, user_id, "No new queued words for learning.")
        lines = [f"learning queue: {len(entries)} words", ""]
        lines.extend(_format_topic_learning_line(entry) for entry in entries)
        chunks = _split_text_chunks("\n".join(lines))
        for idx, chunk in enumerate(chunks):
            markup = _db_search_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=markup,
                parse_mode="Markdown",
            )
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
        return await _send_db_search_prompt(update, user_id, f"Added to learning: {added}")
    if action == "db_search_matches":
        context.user_data.pop("db_search_topic_pick", None)
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_all_words(get_db_path())
        blocks = _list_match_pairs(entries, _user_dict_lang(user_id))
        if not blocks:
            return await _send_db_search_prompt(update, user_id, "No matches found.")
        chunks = _split_text_chunks("\n\n".join(blocks))
        for idx, chunk in enumerate(chunks):
            markup = _db_search_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=markup,
                parse_mode="Markdown",
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
            return await _send_db_search_prompt(update, user_id, "No plural==singular words found.")
        lines = [f"plural_match: {len(entries)}", ""]
        for e in entries:
            article = (e.get("article") or "").strip()
            german = _cap_word(e.get("german", ""))
            lines.append(f"{article} {german}".strip())
        chunks = _split_text_chunks("\n".join(lines))
        for idx, chunk in enumerate(chunks):
            markup = _db_search_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=markup,
                parse_mode="Markdown",
            )
        return DB_SEARCH
    if action == "db_search_weak_words":
        context.user_data.pop("db_search_topic_pick", None)
        mode = _mode_l1_de(user_id)
        entries = list_fsrs_weak_words(get_db_path(), user_id=user_id, mode=mode, min_wrong_streak=4)
        if not entries:
            context.user_data.pop("db_search_weak_ids", None)
            return await _send_db_search_prompt(update, user_id, "No weak words found.")
        context.user_data["db_search_weak_ids"] = [x.get("id") for x in entries if x.get("id")]
        lines = [_format_fsrs_weak_line(entry, _user_dict_lang(user_id)) for entry in entries]
        chunks = _split_text_chunks("\n\n".join(lines))
        for idx, chunk in enumerate(chunks):
            markup = _db_search_weak_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=markup,
                parse_mode="Markdown",
            )
        return DB_SEARCH
    if action == "db_search_topic_words":
        context.user_data["db_search_words_lists_menu"] = True
        await update.message.reply_text("words_lists:", reply_markup=_db_search_words_lists_markup(user_id))
        return DB_SEARCH
    if action == "db_search_topics_lists":
        context.user_data.pop("db_search_weak_ids", None)
        context.user_data["db_search_words_lists_menu"] = True
        markup, topics = _db_search_topic_markup(user_id)
        if not topics:
            await update.message.reply_text("No topics found in DB.", reply_markup=_db_search_words_lists_markup(user_id))
            return DB_SEARCH
        context.user_data["db_search_topic_pick"] = True
        await update.message.reply_text("Choose topic:", reply_markup=markup)
        return DB_SEARCH
    if action == "db_search_type_lists":
        context.user_data.pop("db_search_weak_ids", None)
        context.user_data["db_search_words_lists_menu"] = True
        markup, types = _db_search_type_markup(user_id)
        if not types:
            await update.message.reply_text("No words found in DB.", reply_markup=_db_search_words_lists_markup(user_id))
            return DB_SEARCH
        context.user_data["db_search_type_pick"] = True
        await update.message.reply_text("Choose type:", reply_markup=markup)
        return DB_SEARCH
    context.user_data.pop("db_search_topic_pick", None)
    context.user_data.pop("db_search_type_pick", None)
    context.user_data.pop("db_search_words_lists_menu", None)
    context.user_data.pop("db_search_weak_ids", None)
    entry = _select_db_search_primary(text)
    if not entry:
        return await _send_db_search_prompt(update, user_id, "Not found.")
    phrase_matches = _find_phrase_side_matches(text, entry)
    await update.message.reply_text(
        f"```text\n{_format_word_card(entry, title='Word card:', user_id=user_id)}\n```",
        parse_mode="Markdown",
    )
    if phrase_matches:
        lines = ["Phrase matches:"]
        for item in phrase_matches:
            phrase = _safe_text(item.get("german", "")).strip()
            trans = _entry_translation(item, _user_dict_lang(user_id))
            if trans:
                lines.append(f"- {phrase} -> {trans}")
            else:
                lines.append(f"- {phrase}")
        await update.message.reply_text(f"```text\n{chr(10).join(lines)}\n```", parse_mode="Markdown")
    await update.message.reply_text(
        "Choose edit action:",
        reply_markup=_clarify_root_markup(user_id),
    )
    context.user_data["quiz_entry"] = entry
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_source"] = "quiz"
    context.user_data["edit_stage"] = "pick_scope"
    context.user_data["edit_return_state"] = DB_SEARCH_VIEW
    return DB_SEARCH_VIEW


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
    # "send to learning" and editing example/image (personal version, P4) are open to a
    # guest; deleting a shared word stays owner-only.
    if action in {"send_learn", "clarify_fields"}:
        return await clarify_fields_input(update, context)
    if text == t(_user_interface_lang(user_id), "report_card"):
        return await clarify_fields_input(update, context)
    if text == "delete" and is_owner(user_id):
        return await clarify_fields_input(update, context)
    # Any other text is treated as a new search query.
    return await db_search_input(update, context)
