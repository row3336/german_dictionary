
import asyncio
import html
import logging
import os
import random
import re
import shutil
import subprocess
from logging.handlers import TimedRotatingFileHandler
from datetime import date, datetime, time, timedelta, timezone

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .ai import generate_fields
from .app_config import (
    ADD_CLARIFY,
    ADD_CONFIRM,
    ADD_EDIT,
    ADD_INPUT,
    ADD_PRECHECK,
    ADD_PRECHECK_EDIT,
    DB_SEARCH,
    DB_SEARCH_VIEW,
    AI_CLARIFY_AI,
    AI_BAN_WORD,
    AI_CLARIFY_FIELD,
    AI_CONFIRM,
    AI_KEYS,
    AI_REJECT,
    AI_SKIP,
    ALLOWED_AI_TYPES,
    ALGO_SETTINGS_CONFIRM,
    ALGO_SETTINGS_EDIT,
    ALGO_SETTINGS_MENU,
    DRILL_SETTINGS_MENU,
    LEARNING_MENU,
    LEARNING_QUIZ,
    LEARNING_SETTINGS_MENU,
    TOPIC_FILTER_EDIT,
    ARTICLE_SET,
    AUTO_DUMP_JOB_NAME,
    DUMP_MENU,
    AUTO_DUMP_MENU,
    AUTO_DUMP_SETTING_KEY,
    BACK_TEXT,
    FLAG_TEXT,
    I_DONT_KNOW_TEXT,
    INPUT_ERR_TEXT,
    MENU,
    NUMERAL_SET,
    NUMERIC_RE,
    QUIZ_ARTICLE,
    QUIZ_ARTICLE_WEAK,
    QUIZ_DE_RU,
    QUIZ_PL_SG,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PLURAL_WEAK,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE_MENU,
    QUIZ_RU_DE,
    QUIZ_VERB_WEAK_MENU,
    SETTINGS_MENU,
    SELECT_DICT_LANG,
    SELECT_UI_LANG,
    SYN_ADD_INPUT,
    SYN_AUTO_CONFIRM,
    SYN_CLEAR_CONFIRM,
    SYN_MENU,
    SYN_REMOVE_INPUT,
    PRECHECK_CONFIRM,
    PRECHECK_EDIT,
    PRECHECK_KEYS,
    PRECHECK_REJECT,
    START_TEXT,
    STOP_TEXT,
    TESTS_MENU,
    get_db_path,
    get_dump_dir,
    get_info_path,
    get_log_path,
    get_owner_id,
    SESSION_RETRY_GAP_KEY,
    SESSION_RETRY_IT_KEY,
    TOPIC_FILTER_KEY,
    WEAK_DRILL_GAP_KEY,
    WEAK_DRILL_REPEATS_KEY,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_KEY,
    LEARNING_REMINDER_INTERVAL_KEY,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_READY_QUORUM_KEY,
    FSRS_REPORT_HOUR_KEY,
    FSRS_REPORT_DATE_KEY,
    FSRS_PEAK_NOTIFY_DATE_KEY,
    FSRS_WEEKLY_REPORT_WEEK_KEY,
    get_suggestions_path,
    is_owner,
)
from .storage import (
    QUIZ_ARTICLE as MODE_ARTICLE,
    QUIZ_DE_RU as MODE_DE_RU,
    QUIZ_RU_DE as MODE_RU_DE,
    add_word_flag,
    add_word,
    add_banned_word,
    add_synonym_link,
    get_statistics_snapshot,
    get_form_state,
    get_form_review_stage,
    list_weak_words,
    delete_word,
    find_word_by_value,
    find_existing_word,
    get_word_by_id,
    search_word,
    has_translation_collision,
    list_all_words,
    list_learning_queue_words,
    list_learning_active_words,
    get_word_study_status,
    list_words_by_topic,
    list_fsrs_weak_words,
    get_synonym_group,
    get_synonym_group_id,
    get_setting,
    init_db,
    list_synonym_candidates_by_ru,
    select_due,
    remove_synonym_link,
    clear_synonym_group,
    set_setting,
    update_form_stats,
    apply_weak_input_err,
    update_weak_review_state,
    update_progress,
    set_progress,
    update_word_fields,
    list_pending_word_flags,
    list_vf_candidates,
    save_vf_check_result,
    list_latest_vf_mismatches,
    approve_vf_checks,
    list_approved_vf_checks,
    mark_vf_check_applied,
    list_latest_vf_retryable,
    is_banned_word,
    log_quiz_event,
    reset_periodicity,
    list_topics,
    learning_active_batches,
    learning_active_count,
    learning_add_words,
    learning_add_word_by_id,
    learning_batch_mark_notified,
    learning_batch_update_ready_state,
    learning_due_entries,
    learning_due_entry,
    learning_get_or_create_batch,
    learning_pick_new_words,
    learning_skip_word,
    learning_mark_preview_next,
    learning_preview_entry,
    learning_reset_session,
    learning_status_snapshot,
    learning_update_result,
    learning_apply_input_err,
    snapshot_user_word_state,
    restore_user_word_state,
    add_transaction_log,
    list_transaction_logs,
    delete_transaction_logs,
    cleanup_transaction_logs,
    get_daily_first_touch_retention,
    get_weekly_first_touch_retention,
    _parse_learning_intervals,
    fsrs_due_forecast,
    WEAK_ACTIVE_MIN_ERRORS,
    WEAK_ACTIVE_MIN_STREAK_WRONG,
    WEAK_ACTIVE_CLEAR_STREAK,
    WEAK_REVIEW_DAYS_STAGE1,
    WEAK_REVIEW_DAYS_STAGE2,
    WEAK_REVIEW_DAYS_STAGE3,
)
from .i18n import (
    dict_choices,
    interface_choices,
    normalize_dict_lang,
    normalize_interface_lang,
    parse_dict_choice,
    parse_interface_choice,
    t,
)
from .text_utils import (
    cap_word as _cap_word,
    low_word as _low_word,
    md_escape as _md_escape,
    normalize_answer as _normalize_answer,
    normalize_lookup_text as _normalize_lookup_text,
    normalize_russian_singular as _normalize_russian_singular,
    russian_variants as _russian_variants,
    split_text_chunks as _split_text_chunks,
    try_parse_json as _try_parse_json,
    type_label as _type_label,
)
from .retry_logic import maybe_take_retry_entry, register_retry_result
from .app_config import (
    LEARNING_DAILY_QTY_DEFAULT,
    LEARNING_INTERVALS_DEFAULT,
    LEARNING_MIX_MODE_DEFAULT,
    LEARNING_READY_QUORUM_DEFAULT,
    LEARNING_REMINDER_COOLDOWN_DEFAULT,
    LEARNING_REMINDER_INTERVAL_DEFAULT,
    LEARNING_REMINDER_JOB_NAME,
    LEARNING_WARMUP_INTERVALS,
    OWNER_DICT_LANG_KEY,
    OWNER_INTERFACE_LANG_KEY,
    RETRY_GAP_QUESTIONS,
    RETRY_MAX_REPEATS,
    FSRS_PEAK_JOB_NAME,
    FSRS_REPORT_JOB_NAME,
    FSRS_REPORT_UTC_HOUR_DEFAULT,
    WEAK_DRILL_GAP_DEFAULT,
    WEAK_DRILL_REPEATS_DEFAULT,
)
from .labels import *  # noqa: F401,F403  (UI button-text constants)
from .common.lang import (
    _dict_label,
    _dict_lang_tag,
    _mode_de_l1,
    _mode_l1_de,
    _mode_pl_sg,
    _user_dict_lang,
    _user_interface_lang,
)
from .common.txlog import _capture_transaction_snapshot, _log_transaction
from .common.session import (
    _ensure_session_mode,
    _mark_session_word,
    _maybe_take_retry_entry,
    _maybe_take_weak_drill_entry,
    _pick_due_entry,
    _register_retry_result,
    _remove_from_retry,
    _reset_retry_state,
    _reset_session,
    _reset_fsrs_cycle_state,
    _reset_weak_drill_state,
    _retry_counts,
    _retry_queue,
    _retry_since_last,
    _session_count,
    _session_key,
    _set_retry_since_last,
    _set_weak_drill_since_last,
    _track_weak_drill_show,
    _weak_drill_counts,
    _weak_drill_queue,
    _weak_drill_since_last,
)
from .common.navigation import (
    _action_key_from_text,
    _back_to_main_menu,
    _back_to_tests,
    _clear_last_answer,
    _reply_text_with_retry,
)
from .common.dumps import (
    _create_dump_file,
    _create_logs_dump_file,
    _create_suggestions_dump_file,
)
from .handlers.dump import (
    _auto_dump_keys,
    _dump_menu_keys,
    auto_dump_choice,
    dump_menu_choice,
)
from .handlers.db_search import (
    _send_db_search_prompt,
    db_search_input,
    db_search_view,
)
from .handlers.synonyms import (
    synonyms_add_input,
    synonyms_auto_confirm,
    synonyms_clear_confirm,
    synonyms_menu,
    synonyms_remove_input,
)
from .handlers.weak import (
    _send_article_weak_prompt,
    _send_plural_weak_prompt,
    quiz_article_weak,
    quiz_partizip2_weak,
    quiz_plural_weak,
    quiz_prateritum_weak,
    quiz_verb_weak_menu_choice,
)
from .handlers.clarify_fields import (
    _get_clarify_target,
    _resume_after_clarify,
    _start_clarify_fields,
    clarify_fields_input,
)
from .handlers.quiz import (
    _apply_input_err,
    _check_ru_de_strict,
    _interval_divisor_for_mode,
    _normalize_plural_value,
    _remember_last_answer,
    _repeat_current_de_ru,
    _repeat_current_ru_de,
    _send_article_prompt,
    _send_de_ru_prompt,
    _send_pl_sg_prompt,
    _send_ru_de_prompt,
    _split_answer_parts,
    _stop_with_summary,
    _synonym_entries,
    _update_srs_for_entry,
    quiz_article,
    quiz_de_ru,
    quiz_pl_sg,
    quiz_ru_de,
)
from .jobs.auto_dump import _configure_auto_dump
from .jobs.reminders import _configure_learning_reminders
from .jobs.reports import _configure_fsrs_reports
from .verb_forms_parsing.parser import (
    VF_JITTER_KEY,
    VF_PROXY_KEY,
    VF_SLEEP_KEY,
    _normalize_proxy_input,
    _vf_apply_single,
    _vf_compare,
    _vf_fetch_word,
    _vf_settings_snapshot,
    _vf_user_agent,
)
from .verb_forms_parsing.handlers import (
    VF_RUNNING_TASKS,
    VF_STOP_FLAGS,
    _send_vf_menu,
    _vf_menu_markup,
    _vf_run_dry_task,
)
from .ui.formatting import (
    _append_hint_lines,
    _article_matches,
    _article_variants,
    _article_with_emoji,
    _entry_translation,
    _expected_partizip2,
    _format_article_question,
    _format_de_ru_question,
    _format_feedback_article,
    _format_feedback_de_ru,
    _format_feedback_partizip2_weak,
    _format_feedback_pl_sg,
    _format_feedback_plural_weak,
    _format_feedback_prateritum_weak,
    _format_feedback_ru_de,
    _format_learning_preview_prompt,
    _format_pl_sg_question,
    _format_ru_question,
    _format_ru_question_no_q,
    _format_topic_learning_line,
    _format_word_card,
    _is_pl_sg_eligible,
    _nomen_with_edge_emoji,
    _status_text,
    _weak_status,
)
from .ui.keyboards import (
    _algo_settings_menu_keys,
    _clarify_fields_markup,
    _clarify_root_markup,
    _quiz_idle_markup,
    _quiz_markup,
    _type_choice_markup,
    _weak_article_markup,
    _drill_settings_menu_keys,
    _learning_menu_keys,
    _learning_settings_menu_keys,
    _main_menu_keys,
    _main_menu_markup,
    _ru_de_menu_keys,
    _settings_menu_keys,
    _tests_menu_keys,
)
from .common.settings import (
    _current_topic_value,
    _get_int_setting,
    _learning_daily_qty,
    _learning_intervals_seconds,
    _learning_intervals_text,
    _learning_mix_mode_enabled,
    _learning_mix_mode_key,
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
    _learning_schedule_seconds,
    _parse_topic_filter,
    _session_retry_gap,
    _session_retry_iterations,
    _set_learning_mix_mode,
    _fsrs_report_hour_utc,
    _topic_filter_banner,
    _topic_filter_for_query,
    _topic_filter_label,
    _topic_filter_value,
    _weak_drill_gap,
    _weak_drill_repeats,
)


# UI button-text labels moved to bot/labels.py.
# Owner/lang setting keys + algorithm defaults moved to bot/app_config.py.
# VF keys + runtime task/stop dicts moved to bot/verb_forms_parsing/


# core menu keyboard builders moved to bot/ui/keyboards.py
# language/mode helpers moved to bot/common/lang.py


# settings getters moved to bot/common/settings.py


def _build_learning_topic_mix_queue(entries):
    """Build weighted mixed queue by topic."""
    buckets = {}
    for entry in entries:
        topic = str(entry.get("topic") or "").strip() or "-"
        buckets.setdefault(topic, []).append(entry)
    for values in buckets.values():
        random.shuffle(values)
    mixed = []
    last_topic = None
    while True:
        candidates = [(topic, values) for topic, values in buckets.items() if values]
        if not candidates:
            break
        topic_pool = candidates
        if last_topic and len(candidates) > 1:
            alt = [(topic, values) for topic, values in candidates if topic != last_topic]
            if alt:
                topic_pool = alt
        topics = [topic for topic, _ in topic_pool]
        weights = [len(values) for _, values in topic_pool]
        selected_topic = random.choices(topics, weights=weights, k=1)[0]
        mixed.append(buckets[selected_topic].pop(0))
        last_topic = selected_topic
    return mixed


def _pick_learning_due_entry(context, user_id, exclude_word_id=None):
    """Pick next due learning entry with optional topic-mix mode."""
    if not _learning_mix_mode_enabled(user_id):
        return learning_due_entry(get_db_path(), user_id=user_id, exclude_word_id=exclude_word_id)
    queue_key = "learning_mix_queue"
    queue = context.user_data.get(queue_key)
    if not isinstance(queue, list) or not queue:
        due_entries = learning_due_entries(get_db_path(), user_id=user_id, exclude_word_id=exclude_word_id)
        if not due_entries and exclude_word_id:
            due_entries = learning_due_entries(get_db_path(), user_id=user_id, exclude_word_id=None)
        queue = _build_learning_topic_mix_queue(due_entries)
        context.user_data[queue_key] = queue
    while queue:
        entry = queue.pop(0)
        wid = entry.get("id")
        if exclude_word_id and wid == exclude_word_id:
            continue
        if not wid:
            continue
        return entry
    context.user_data.pop(queue_key, None)
    return None


# topic-filter + fsrs-report-hour helpers moved to bot/common/settings.py


async def _send_algo_settings_menu(update: Update, user_id):
    """Handle send algo settings menu."""
    lang = _user_interface_lang(user_id)
    gap = _session_retry_gap(get_db_path())
    iterations = _session_retry_iterations(get_db_path())
    weak_drill_gap = _weak_drill_gap(get_db_path())
    weak_drill_repeats = _weak_drill_repeats(get_db_path())
    topics = list_topics(get_db_path())
    topics_line = ", ".join(topics) if topics else "-"
    msg = "\n".join(
        [
            f"{t(lang, 'algo_settings')}:",
            f"{t(lang, 'ses_rtr_gap')} = {gap}",
            f"{t(lang, 'ses_rtr_it')} = {iterations}",
            f"{t(lang, 'weak_drill_gap')} = {weak_drill_gap}",
            f"{t(lang, 'weak_drill_repeats')} = {weak_drill_repeats}",
            f"{t(lang, 'topic_filter')} = {_topic_filter_value()}",
            f"weak.active = err>={WEAK_ACTIVE_MIN_ERRORS} or wrong_streak>={WEAK_ACTIVE_MIN_STREAK_WRONG}",
            f"weak.to_review = {WEAK_ACTIVE_CLEAR_STREAK} correct in row",
            f"weak.review_days = {WEAK_REVIEW_DAYS_STAGE1}/{WEAK_REVIEW_DAYS_STAGE2}/{WEAK_REVIEW_DAYS_STAGE3}",
            f"topics = {topics_line}",
        ]
    )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return ALGO_SETTINGS_MENU


async def _send_drill_settings_menu(update: Update, user_id):
    """Handle send drill settings menu."""
    lang = _user_interface_lang(user_id)
    msg = "\n".join(
        [
            f"{t(lang, 'drill_sett')}:",
            f"{t(lang, 'weak_drill_gap')} = {_weak_drill_gap(get_db_path())}",
            f"{t(lang, 'weak_drill_repeats')} = {_weak_drill_repeats(get_db_path())}",
        ]
    )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(_drill_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return DRILL_SETTINGS_MENU


async def _send_learning_settings_menu(update: Update, user_id):
    """Handle send learning settings menu."""
    lang = _user_interface_lang(user_id)
    msg = "\n".join(
        [
            f"{t(lang, 'learning_sett')}:",
            f"{t(lang, 'learning_daily_qty')} = {_learning_daily_qty(get_db_path())}",
            f"{t(lang, 'learning_intervals')} = {_learning_intervals_text(get_db_path())}",
            f"learning_warmup = {','.join(str(v) for v in LEARNING_WARMUP_INTERVALS)}",
            f"{t(lang, 'learning_ready_quorum')} = {_learning_ready_quorum(get_db_path())}",
            f"{t(lang, 'learning_reminder_interval')} = {_learning_reminder_interval(get_db_path())}",
            f"{t(lang, 'learning_reminder_cooldown')} = {_learning_reminder_cooldown(get_db_path())}",
            "learning.on_wrong = reset_step_0",
        ]
    )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(_learning_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_SETTINGS_MENU


def _run_unit_tests_report():
    """Run the unit test suite + DB smoke check; return a compact summary."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONWARNINGS"] = "ignore::ResourceWarning"

    cmd = ["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    try:
        proc = subprocess.run(
            cmd, cwd=root_dir, capture_output=True, text=True, timeout=120, check=False, env=env
        )
    except Exception as exc:
        return f"unit tests failed to run: {exc}"

    out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    m = re.search(r"Ran (\d+) test", out)
    total = int(m.group(1)) if m else 0
    failed = [s.strip() for s in out.splitlines() if s.strip().startswith(("FAIL:", "ERROR:"))]

    if proc.returncode == 0:
        lines = [f"✅ unit tests: {total}/{total} OK"]
    else:
        lines = [f"❌ unit tests: {max(0, total - len(failed))}/{total} OK, {len(failed)} failed:"]
        lines += [f"  {x}" for x in failed[:30]]
        if not failed:
            lines += [f"  {t}" for t in out.strip().splitlines()[-5:]]

    smoke_cmd = ["python3", "-m", "scripts.smoke_check_db", "--db", str(get_db_path())]
    try:
        sp = subprocess.run(
            smoke_cmd, cwd=root_dir, capture_output=True, text=True, timeout=60, check=False, env=env
        )
        if sp.returncode == 0:
            lines.append("✅ db smoke: OK")
        else:
            tail = ((sp.stdout or "") + (sp.stderr or "")).strip().splitlines()[-3:]
            lines.append("❌ db smoke: FAIL")
            lines += [f"  {t}" for t in tail]
    except Exception as exc:
        lines.append(f"❌ db smoke: failed to run: {exc}")

    return "\n".join(lines).replace("```", "'''")


# _dump_menu_keys moved to bot/handlers/dump.py


# _action_key_from_text moved to bot/common/navigation.py


# _auto_dump_keys moved to bot/handlers/dump.py


def _add_keys(user_id):
    """Handle add keys."""
    return [[t(_user_interface_lang(user_id), "back")]]


def _stop_keys(user_id):
    """Handle stop keys."""
    return [[t(_user_interface_lang(user_id), "stop")]]


def _clarify_keys(user_id):
    """Handle clarify keys."""
    return AI_KEYS


def _add_input_markup(user_id):
    """Handle add input markup."""
    return ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


# db_search markups + _topic_sort_key + _format_topic_words_line moved to bot/handlers/db_search.py


# _format_topic_learning_line moved to bot/ui/formatting.py
# _split_text_chunks moved to bot/text_utils.py (split_text_chunks)


def _load_info_text(lang="en"):
    """Handle load info text."""
    lang = normalize_interface_lang(lang, default="en")
    path = get_info_path().parent / f"info_{lang}.md"
    if not path.exists():
        path = get_info_path()
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "Info file not found."


# quiz keyboards moved to bot/ui/keyboards.py


def _parse_level_toggle(text):
    """Handle parse level toggle."""
    token = (text or "").strip().split(" ", 1)[0].upper()
    return token if token in {"A1", "A2", "B1", "B2", "C1", "C2"} else ""


# _clarify_fields_markup / _type_choice_markup moved to bot/ui/keyboards.py


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


# _allowed_clarify_fields moved to bot/handlers/clarify_fields.py


# _clarify_root_markup moved to bot/ui/keyboards.py


# _dict_lang_tag moved to bot/common/lang.py


# synonyms menu/markups/handlers (_syn_auto_text, _syn_menu_markup, _syn_confirm_markup, _format_syn_group, _open_synonyms_menu, _return_from_synonyms, synonyms_*) moved to bot/handlers/synonyms.py


# _set_entry_field / _set_entry_field_value / _get_clarify_target / _start_clarify_fields moved to bot/handlers/clarify_fields.py


# _remember_last_answer moved to bot/handlers/quiz.py
# _clear_last_answer moved to bot/common/navigation.py


# _capture_transaction_snapshot / _log_transaction moved to bot/common/txlog.py


# session/FSRS-cycle/retry/weak-drill glue moved to bot/common/session.py


# _stop_with_summary moved to bot/handlers/quiz.py


# _remember_last_weak_answer / _apply_input_err_weak moved to bot/handlers/weak.py


# _remove_from_retry moved to bot/common/session.py


# _repeat_current_ru_de / _repeat_current_de_ru moved to bot/handlers/quiz.py


def _send_entry_to_learning(update: Update, context: ContextTypes.DEFAULT_TYPE, entry_key="quiz_entry"):
    """Handle send entry to learning."""
    entry = context.user_data.get(entry_key)
    if not entry:
        return False
    db_path = get_db_path()
    user_id = update.effective_user.id
    batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=_learning_daily_qty(db_path))
    added = learning_add_word_by_id(
        db_path,
        user_id=user_id,
        word_id=entry.get("id"),
        batch_id=batch_id,
        first_delay_seconds=_learning_schedule_seconds(db_path)[0],
    )
    return bool(added)


# _apply_input_err moved to bot/handlers/quiz.py


def _apply_input_err_learning(update, context):
    """Handle apply input error for learning mode."""
    last_id = context.user_data.get("last_learning_entry_id")
    last_ok = context.user_data.get("last_learning_ok")
    last_prev_step = context.user_data.get("last_learning_prev_step")
    if not last_id or last_ok is None:
        return False
    if last_ok:
        return False
    snapshot = _capture_transaction_snapshot(update.effective_user.id, last_id, ["learning"])
    result = learning_apply_input_err(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_id,
        prev_step_index=last_prev_step,
        intervals_seconds=_learning_schedule_seconds(get_db_path()),
    )
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_id,
        mode="learning",
        is_correct=True,
    )
    _log_transaction(update.effective_user.id, "input_err:learning", snapshot)
    context.user_data["last_learning_ok"] = True
    return bool(result)


# _resume_after_clarify moved to bot/handlers/clarify_fields.py


# _format_word_card moved to bot/ui/formatting.py


# db_search card/match/weak formatters + _select_db_search_primary/_find_phrase_side_matches/_list_match_pairs moved to bot/handlers/db_search.py


# _reply_text_with_retry moved to bot/common/navigation.py


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


def _is_owner_only(user_id):
    """Handle is owner only."""
    return is_owner(user_id)


async def _ask_interface_language(update: Update):
    """Handle ask interface language."""
    await update.message.reply_text(
        "Choose interface language:",
        reply_markup=ReplyKeyboardMarkup([interface_choices()], resize_keyboard=True),
    )
    return SELECT_UI_LANG


async def _ask_dictionary_language(update: Update, user_id):
    """Handle ask dictionary language."""
    lang = _user_interface_lang(user_id)
    choices = dict_choices(lang)
    await update.message.reply_text(
        t(lang, "choose_dict_lang"),
        reply_markup=ReplyKeyboardMarkup([[choices[0]], [choices[1]], [choices[2]]], resize_keyboard=True),
    )
    return SELECT_DICT_LANG


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle start."""
    user = update.effective_user
    user_id = user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END
    ui_lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "")
    dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "")
    if not ui_lang or not dict_lang:
        return await _ask_interface_language(update)
    await update.message.reply_text(t(_user_interface_lang(user_id), "main_menu"), reply_markup=_main_menu_markup(user_id))
    return MENU


async def undo_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /undo [N] rollback by transaction log."""
    user_id = update.effective_user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END
    steps = 1
    if context.args:
        try:
            steps = int(context.args[0])
        except (TypeError, ValueError):
            steps = 1
    steps = max(1, min(20, steps))
    logs = list_transaction_logs(get_db_path(), user_id=user_id, limit=steps)
    if not logs:
        await update.message.reply_text("Nothing to undo.")
        return MENU
    restored = 0
    processed_ids = []
    for item in logs:
        payload = item.get("payload") or {}
        snapshot = payload.get("snapshot")
        if restore_user_word_state(get_db_path(), snapshot):
            restored += 1
        processed_ids.append(int(item.get("id")))
    delete_transaction_logs(get_db_path(), user_id=user_id, ids=processed_ids)
    await update.message.reply_text(f"Undo done: {restored}/{len(logs)} step(s).")
    return MENU


# _send_db_search_prompt moved to bot/handlers/db_search.py


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "start":
        return await start(update, context)

    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END

    if action == "add_word":
        await update.message.reply_text(
            f"Send a word or a list (one per line), or upload a .txt file.\n\n{_topic_filter_banner()}",
            reply_markup=_add_input_markup(user_id),
            parse_mode="Markdown",
        )
        return ADD_INPUT

    if action == "tests":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "choose_test"),
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU

    if action == "settings":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "db_search":
        return await _send_db_search_prompt(update, user_id, None)

    if action == "info":
        chunks = _split_text_chunks(_load_info_text(_user_interface_lang(user_id)))
        if not chunks:
            await update.message.reply_text("Info is empty.", reply_markup=_main_menu_markup(user_id))
            return MENU
        for idx, chunk in enumerate(chunks):
            markup = _main_menu_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup)
        return MENU

    if action == "stat":
        stats = get_statistics_snapshot(get_db_path(), user_id=user_id, mode=_mode_l1_de(user_id))
        word_types = stats.get("word_types") or {}
        lines = [
            f"today complete fsrs Ru > De = {stats['today_complete_fsrs_ru_de']}",
            f"today complete fsrs De > Ru = {stats['today_complete_fsrs_de_ru']}",
            f"today complete pl > sing = {stats.get('today_complete_pl_sg', 0)}",
            "",
            f"today complete weak article = {stats['today_complete_weak_article']}",
            f"today complete weak plural = {stats['today_complete_weak_plural']}",
            f"today complete weak partizip2 = {stats['today_complete_weak_partizip2']}",
            f"today complete weak preteritum = {stats['today_complete_weak_prateritum']}",
            "",
            f"5+ correct streak = {stats.get('streak_5p', 0)}",
            f"4 correct streak = {stats.get('streak_4', 0)}",
            f"3 correct streak = {stats.get('streak_3', stats.get('streak_3p', 0))}",
            f"2 correct streak = {stats.get('streak_2', 0)}",
            f"1 correct = {stats.get('streak_1', 0)}",
            f"wrong fsrs = {stats.get('wrong_fsrs', 0)}",
            "",
            (
                f"weak article = {stats['weak_article']} "
                f"({stats['weak_article_stage1']}) "
                f"({stats['weak_article_stage2']}) "
                f"({stats['weak_article_stage3']})"
            ),
            (
                f"weak plural = {stats['weak_plural']} "
                f"({stats['weak_plural_stage1']}) "
                f"({stats['weak_plural_stage2']}) "
                f"({stats['weak_plural_stage3']})"
            ),
            (
                f"weak partizip2 = {stats['weak_partizip2']} "
                f"({stats['weak_partizip2_stage1']}) "
                f"({stats['weak_partizip2_stage2']}) "
                f"({stats['weak_partizip2_stage3']})"
            ),
            (
                f"weak preterium = {stats['weak_prateritum']} "
                f"({stats['weak_prateritum_stage1']}) "
                f"({stats['weak_prateritum_stage2']}) "
                f"({stats['weak_prateritum_stage3']})"
            ),
            "",
            f"words in dict = {stats['total_words']}",
            f"never touched = {stats['never_answered']}",
            f"words weekly = +{stats['weekly_words']}",
            f"learning weekly = {stats.get('learning_weekly', 0)}",
            "",
            "word types:",
            *[f"{k} = {word_types[k]}" for k in sorted(word_types.keys())],
        ]
        await update.message.reply_text(
            f"```text\n{chr(10).join(lines)}\n```",
            reply_markup=_main_menu_markup(user_id),
            parse_mode="Markdown",
        )
        return MENU

    await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
    return MENU


# db_search_input / db_search_view moved to bot/handlers/db_search.py


async def select_ui_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle select ui lang."""
    user_id = update.effective_user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END

    interface_lang = parse_interface_choice(update.message.text.strip())
    if not interface_lang:
        await update.message.reply_text(
            "Choose interface language:",
            reply_markup=ReplyKeyboardMarkup([interface_choices()], resize_keyboard=True),
        )
        return SELECT_UI_LANG

    set_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, interface_lang)
    return await _ask_dictionary_language(update, user_id)


async def select_dict_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle select dict lang."""
    user_id = update.effective_user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END

    lang = _user_interface_lang(user_id)
    dict_lang = parse_dict_choice(update.message.text.strip(), lang)
    if not dict_lang:
        return await _ask_dictionary_language(update, user_id)

    set_setting(get_db_path(), OWNER_DICT_LANG_KEY, dict_lang)
    await update.message.reply_text(
        t(lang, "main_menu"),
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


def _format_eta_from_iso(next_due_iso):
    """Handle format eta from iso."""
    token = str(next_due_iso or "").strip()
    if not token:
        return "-"
    try:
        when = datetime.strptime(token, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return token
    now = datetime.now(timezone.utc)
    delta = int((when - now).total_seconds())
    if delta <= 0:
        return "now"
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


async def _send_learning_menu(update: Update, user_id):
    """Handle send learning menu."""
    stats = learning_status_snapshot(
        get_db_path(),
        user_id=user_id,
        quorum_pct=_learning_ready_quorum(get_db_path()),
    )
    active_entries = list_learning_active_words(get_db_path(), user_id=user_id)
    active_lines = [_format_topic_learning_line(item) for item in active_entries]
    active_preview = active_lines[:30]
    if len(active_lines) > 30:
        active_preview.append(f"... +{len(active_lines) - 30} more")
    msg = "\n".join(
        [
            f"{LEARNING_TEXT}:",
            "",
            _topic_filter_banner(),
            "",
            f"{t(_user_interface_lang(user_id), 'learning_daily_qty')} = {_learning_daily_qty(get_db_path())}",
            f"{t(_user_interface_lang(user_id), 'learning_intervals')} = {_learning_intervals_text(get_db_path())}",
            f"{LEARNING_MIX_MODE_TEXT} = {'ON' if _learning_mix_mode_enabled(user_id) else 'OFF'}",
            f"active = {stats.get('active_count', 0)}",
            f"preview_pending = {stats.get('preview_pending', 0)}",
            f"due_now = {stats.get('due_count', 0)} / need {stats.get('ready_needed', 0)}",
            f"completed_in_batch = {stats.get('completed_count', 0)}",
            f"next_due = {_format_eta_from_iso(stats.get('next_due'))}",
            "",
            "active words:",
            *(active_preview if active_preview else ["-"]),
        ]
    )
    await _reply_text_with_retry(
        update.message,
        msg,
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def tests_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tests menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "stop":
        return await _back_to_main_menu(update)
    if action == "back":
        return await _back_to_main_menu(update)
    if action == "quiz_l1_de":
        await update.message.reply_text(
            f"{QUIZ_RU_DE_TEXT}:\n\n{_topic_filter_banner()}",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE_MENU
    if action == "quiz_de_l1":
        _reset_retry_state(context)
        _clear_last_answer(context)
        _reset_session(context, _mode_de_l1(user_id))
        return await _send_de_ru_prompt(
            update,
            context,
            f"{QUIZ_DE_RU_TEXT}:\n\n{_topic_filter_banner()}",
        )
    if action == "quiz_pl_sg":
        _reset_retry_state(context)
        _clear_last_answer(context)
        _reset_session(context, _mode_pl_sg(user_id))
        return await _send_pl_sg_prompt(
            update,
            context,
            f"{QUIZ_PL_SG_TEXT}:\n\n{_topic_filter_banner()}",
        )
    if action == "learning":
        return await _send_learning_menu(update, user_id)
    if action == "topic_filter":
        current = _topic_filter_value()
        topics = list_topics(get_db_path())
        topics_line = ", ".join(topics) if topics else "-"
        await update.message.reply_text(
            f"Current topic filter: {current}\nTopics: {topics_line}\nSend e.g. 1+2+food or 'all'.",
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        context.user_data["topic_return"] = "tests_menu"
        return TOPIC_FILTER_EDIT

    await update.message.reply_text(
        "Use tests menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU


async def quiz_ru_de_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz ru de menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU
    if action == "topic_filter":
        current = _topic_filter_value()
        topics = list_topics(get_db_path())
        topics_line = ", ".join(topics) if topics else "-"
        await update.message.reply_text(
            f"Current topic filter: {current}\nTopics: {topics_line}\nSend e.g. 1+2+food or 'all'.",
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        context.user_data["topic_return"] = "ru_de_menu"
        return TOPIC_FILTER_EDIT
    if text == STANDARD_FSRS_TEXT:
        _reset_retry_state(context)
        _clear_last_answer(context)
        _reset_session(context, _mode_l1_de(user_id))
        return await _send_ru_de_prompt(update, context, None)
    if text == TEST_ARTICLE_WEAK_TEXT:
        _reset_weak_drill_state(context, "weak_article")
        _reset_session(context, "weak_article")
        return await _send_article_weak_prompt(update, context, None)
    if text == TEST_PLURAL_WEAK_TEXT:
        _reset_weak_drill_state(context, "weak_plural")
        _reset_session(context, "weak_plural")
        return await _send_plural_weak_prompt(update, context, None)
    if text == TEST_VERB_WEAK_TEXT:
        await update.message.reply_text(
            "Choose verb weak mode:",
            reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    await update.message.reply_text(
        "Use Ru > De menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
    )
    return QUIZ_RU_DE_MENU


def _format_learning_preview_line(entry, dict_lang):
    """Handle format learning preview line."""
    word_type = (entry.get("type") or "").strip().lower()
    if word_type == "verb":
        left = _low_word(entry.get("infinitive", "")).strip()
    else:
        article = (entry.get("article") or "").strip()
        left = f"{article} {_cap_word(entry.get('german', ''))}".strip()
    right = _entry_translation(entry, dict_lang)
    return f"- {left} = {right}"


async def _learning_add_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning add words."""
    user_id = update.effective_user.id
    db_path = get_db_path()
    daily_qty = _learning_daily_qty(db_path)
    batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=daily_qty)
    active_count = learning_active_count(db_path, user_id=user_id, batch_id=batch_id)
    need = max(0, daily_qty - active_count)
    if need <= 0:
        await update.message.reply_text(
            f"Learning batch is full: {active_count}/{daily_qty}.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    picked = learning_pick_new_words(db_path, user_id=user_id, limit=need, topics=_topic_filter_for_query())
    if not picked:
        await update.message.reply_text(
            "No new words available for learning.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    first_delay = _learning_schedule_seconds(db_path)[0]
    added = learning_add_words(
        db_path,
        user_id=user_id,
        words=picked,
        batch_id=batch_id,
        first_delay_seconds=first_delay,
    )
    await _reply_text_with_retry(
        update.message,
        f"Added for learning: {added}\nStart learning session for first cycle preview.",
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def _send_learning_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    """Handle send learning prompt."""
    user_id = update.effective_user.id
    preview_entry = learning_preview_entry(get_db_path(), user_id=user_id)
    if preview_entry:
        context.user_data["quiz_entry"] = preview_entry
        context.user_data["learning_preview_mode"] = True
        prompt_text = _format_learning_preview_prompt(preview_entry, _user_dict_lang(user_id))
        if prefix_text:
            prompt_text = f"{prefix_text}\n\n{prompt_text}"
        await _reply_text_with_retry(
            update.message,
            prompt_text,
            reply_markup=ReplyKeyboardMarkup(
                [[LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT], [AI_CLARIFY_FIELD, t(_user_interface_lang(user_id), "back")]],
                resize_keyboard=True,
            ),
            parse_mode="Markdown",
        )
        return LEARNING_QUIZ

    last_learning_word_id = context.user_data.get("last_learning_prompt_word_id")
    entry = force_entry
    if not entry:
        entry = _pick_learning_due_entry(context, user_id, exclude_word_id=last_learning_word_id)
    if not entry:
        context.user_data["learning_preview_mode"] = False
        context.user_data.pop("last_learning_prompt_word_id", None)
        context.user_data.pop("learning_mix_queue", None)
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            f"No due learning words right now.\nnext_due = {_format_eta_from_iso(learning_status_snapshot(get_db_path(), user_id, _learning_ready_quorum(get_db_path())).get('next_due'))}",
            reply_markup=ReplyKeyboardMarkup(
                [[INPUT_ERR_TEXT], [t(_user_interface_lang(user_id), "back")]],
                resize_keyboard=True,
            ),
        )
        return LEARNING_QUIZ
    context.user_data["quiz_entry"] = entry
    context.user_data["learning_preview_mode"] = False
    context.user_data["last_learning_prompt_word_id"] = entry.get("id")
    prompt_text = _format_ru_question(entry, _user_dict_lang(user_id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=ReplyKeyboardMarkup(
            [[INPUT_ERR_TEXT, I_DONT_KNOW_TEXT], [LEARNING_SKIP_TEXT, AI_CLARIFY_FIELD, t(_user_interface_lang(user_id), "back")]],
            resize_keyboard=True,
        ),
        parse_mode="Markdown",
    )
    return LEARNING_QUIZ


async def learning_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(user_id), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU
    if action == "learning_add":
        return await _learning_add_words(update, context)
    if action == "learning_mix_mode":
        enabled = _learning_mix_mode_enabled(user_id)
        _set_learning_mix_mode(user_id, not enabled)
        context.user_data.pop("learning_mix_queue", None)
        await _reply_text_with_retry(update.message, f"{LEARNING_MIX_MODE_TEXT} = {'ON' if not enabled else 'OFF'}")
        return await _send_learning_menu(update, user_id)
    if action == "learning_reset":
        learning_reset_session(get_db_path(), user_id=user_id)
        context.user_data.pop("learning_mix_queue", None)
        await _reply_text_with_retry(
            update.message,
            "Learning session reset. Words returned to new.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    if action == "learning_start":
        context.user_data.pop("learning_mix_queue", None)
        return await _send_learning_prompt(update, context, None)
    await _reply_text_with_retry(
        update.message,
        "Use learning menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def learning_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning quiz."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        return await _send_learning_menu(update, user_id)
    if text == AI_CLARIFY_FIELD and is_owner(user_id):
        return await _start_clarify_fields(update, context, LEARNING_QUIZ, prefer_current=True)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_learning_prompt(update, context, None)

    is_preview = bool(context.user_data.get("learning_preview_mode"))
    if is_preview:
        if action == "learning_skip" or text == LEARNING_SKIP_TEXT:
            snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
            learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
            _log_transaction(user_id, "learning:skip", snapshot)
            return await _send_learning_prompt(update, context, "Skipped for learning.")
        if action == "learning_next" or text == LEARNING_NEXT_TEXT:
            snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
            learning_mark_preview_next(
                get_db_path(),
                user_id=user_id,
                word_id=entry.get("id"),
                first_interval_seconds=_learning_schedule_seconds(get_db_path())[0],
            )
            _log_transaction(user_id, "learning:preview_next", snapshot)
            return await _send_learning_prompt(update, context, "Saved. Next preview word.")
        await _reply_text_with_retry(
            update.message,
            "Use next/skip/back.",
            reply_markup=ReplyKeyboardMarkup([[LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return LEARNING_QUIZ

    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_learning(update, context)
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        context.user_data.pop("learning_mix_queue", None)
        return await _send_learning_prompt(update, context, prefix, force_entry=context.user_data.get("quiz_entry"))

    if action == "learning_skip" or text == LEARNING_SKIP_TEXT:
        snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
        learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
        _log_transaction(user_id, "learning:skip", snapshot)
        context.user_data.pop("learning_mix_queue", None)
        return await _send_learning_prompt(update, context, "Skipped for learning.")

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok, _ = _check_ru_de_strict(entry, text)
    context.user_data["last_learning_entry_id"] = entry.get("id")
    context.user_data["last_learning_ok"] = bool(ok)
    context.user_data["last_learning_prev_step"] = int(entry.get("learning_step") or 0)
    log_quiz_event(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        mode="learning",
        is_correct=ok,
    )
    snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), ["learning"])
    result = learning_update_result(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        ok=ok,
        intervals_seconds=_learning_schedule_seconds(get_db_path()),
    )
    _log_transaction(user_id, "learning:answer", snapshot)
    feedback = _format_feedback_ru_de(entry, ok)
    if result.get("completed"):
        feedback = f"{feedback}\n\nLearning done ✅, sent to fsrs"
    context.user_data.pop("learning_mix_queue", None)
    return await _send_learning_prompt(update, context, feedback)


# quiz_verb_weak_menu_choice moved to bot/handlers/weak.py


# _vf_menu_markup / _vf_settings_snapshot moved to bot/verb_forms_parsing/


def _safe_text(value):
    return str(value or "")


# vf parse/fetch/compare/apply moved to bot/verb_forms_parsing/parser.py


# _vf_run_dry_task / _send_vf_menu moved to bot/verb_forms_parsing/handlers.py


async def settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if context.user_data.get("vf_waiting") and action != "back":
        wait = context.user_data.get("vf_waiting")
        context.user_data.pop("vf_waiting", None)
        if wait == "proxy":
            proxy_value = _normalize_proxy_input(text.strip())
            set_setting(get_db_path(), VF_PROXY_KEY, proxy_value)
            return await _send_vf_menu(update, "✅ proxy saved")
        if wait == "sleep":
            try:
                value = max(1, int(text.strip()))
            except ValueError:
                return await _send_vf_menu(update, "❌ sleep must be integer seconds")
            set_setting(get_db_path(), VF_SLEEP_KEY, str(value))
            return await _send_vf_menu(update, "✅ sleep saved")
        if wait == "jitter":
            try:
                value = int(text.strip())
            except ValueError:
                return await _send_vf_menu(update, "❌ jitter must be integer percent")
            value = max(0, min(80, value))
            set_setting(get_db_path(), VF_JITTER_KEY, str(value))
            return await _send_vf_menu(update, "✅ jitter saved")

    if context.user_data.get("vf_menu") and (text == BACK_TEXT or action == "back"):
        context.user_data.pop("vf_menu", None)
        context.user_data.pop("vf_waiting", None)
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "back":
        context.user_data.pop("vf_waiting", None)
        context.user_data.pop("vf_menu", None)
        return await _back_to_main_menu(update)

    if action == "interface_lang":
        return await _ask_interface_language(update)

    if action == "dict_lang":
        return await _ask_dictionary_language(update, user_id)

    if action == "algo_settings":
        return await _send_algo_settings_menu(update, user_id)

    if action == "dump":
        await update.message.reply_text(
            "dump:",
            reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
        )
        return DUMP_MENU

    if action == "review_flags":
        context.user_data["vf_menu"] = True
        return await _send_vf_menu(update)

    if context.user_data.get("vf_menu"):
        db_path = get_db_path()
        proxy, sleep_sec, jitter_pct = _vf_settings_snapshot()

        if text == VF_SET_PROXY_TEXT:
            context.user_data["vf_waiting"] = "proxy"
            await update.message.reply_text("Send proxy url:", reply_markup=ReplyKeyboardRemove())
            return SETTINGS_MENU
        if text == VF_SET_SLEEP_TEXT:
            context.user_data["vf_waiting"] = "sleep"
            await update.message.reply_text("Send sleep in seconds:", reply_markup=ReplyKeyboardRemove())
            return SETTINGS_MENU
        if text == VF_SET_JITTER_TEXT:
            context.user_data["vf_waiting"] = "jitter"
            await update.message.reply_text("Send jitter percent (0-80):", reply_markup=ReplyKeyboardRemove())
            return SETTINGS_MENU

        if text == VF_TEST_PROXY_TEXT:
            if not proxy:
                return await _send_vf_menu(update, "❌ proxy is required")
            try:
                _vf_fetch_word("Haus", proxy, _vf_user_agent(), timeout_sec=20)
                return await _send_vf_menu(update, "✅ proxy ok")
            except Exception as exc:
                return await _send_vf_menu(update, f"❌ proxy failed: {exc}")

        if text in {VF_DRY_RUN_NEW_TEXT, VF_DRY_RUN_ALL_TEXT, VF_RETRY_TEXT}:
            if not proxy:
                return await _send_vf_menu(update, "❌ proxy is required")
            running = VF_RUNNING_TASKS.get(int(user_id))
            if running and not running.done():
                return await _send_vf_menu(update, "Parser is already running. Use stop_parse.")
            if text == VF_RETRY_TEXT:
                retry_rows = list_latest_vf_retryable(db_path, user_id, limit=500)
                ids = {row.get("word_id") for row in retry_rows}
                candidates = [w for w in list_vf_candidates(db_path, user_id, only_unchecked=False, limit=0) if w.get("id") in ids]
            else:
                candidates = list_vf_candidates(
                    db_path,
                    user_id,
                    only_unchecked=(text == VF_DRY_RUN_NEW_TEXT),
                    limit=0,
                )
            if not candidates:
                return await _send_vf_menu(update, "No candidates.")
            VF_STOP_FLAGS[int(user_id)] = False
            label = "retry_failed" if text == VF_RETRY_TEXT else ("dry_run_new" if text == VF_DRY_RUN_NEW_TEXT else "dry_run_all")
            task = asyncio.create_task(
                _vf_run_dry_task(
                    context.bot,
                    update.effective_chat.id,
                    int(user_id),
                    candidates,
                    sleep_sec,
                    jitter_pct,
                    proxy,
                    label,
                )
            )
            VF_RUNNING_TASKS[int(user_id)] = task
            return await _send_vf_menu(update, f"Started {label}. Running in background.")

        if text == VF_STOP_TEXT:
            running = VF_RUNNING_TASKS.get(int(user_id))
            if not running or running.done():
                return await _send_vf_menu(update, "No active parser task.")
            VF_STOP_FLAGS[int(user_id)] = True
            return await _send_vf_menu(update, "Stop requested. Waiting current request to finish...")

        if text == VF_SHOW_DIFF_TEXT:
            rows = list_latest_vf_mismatches(db_path, user_id, limit=300)
            if not rows:
                return await _send_vf_menu(update, "No mismatches.")
            lines = ["mismatches (use: approve id1,id2):"]
            for row in rows:
                diff = row.get("diff_payload") or {}
                fields = ",".join(sorted(diff.keys())) or "-"
                lines.append(f"{row['id']}: {row.get('base_form','')} [{row.get('word_type','')}] -> {fields}")
            chunks = _split_text_chunks("\n".join(lines))
            for idx, chunk in enumerate(chunks):
                markup = _vf_menu_markup() if idx == len(chunks) - 1 else None
                await update.message.reply_text(f"```text\n{chunk}\n```", parse_mode="Markdown", reply_markup=markup)
            return SETTINGS_MENU

        if text.startswith("approve "):
            raw = text.split(" ", 1)[1]
            ids = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
            changed = approve_vf_checks(db_path, user_id, ids)
            return await _send_vf_menu(update, f"approved: {changed}")

        if text == VF_APPLY_TEXT:
            rows = list_approved_vf_checks(db_path, user_id, only_not_applied=True)
            if not rows:
                return await _send_vf_menu(update, "No approved rows.")
            applied = 0
            for row in rows:
                if _vf_apply_single(db_path, row):
                    applied += 1
                mark_vf_check_applied(db_path, row.get("id"))
            return await _send_vf_menu(update, f"applied: {applied}/{len(rows)}")

        if text == VF_VERIFY_TEXT:
            rows = list_approved_vf_checks(db_path, user_id, only_not_applied=False)
            if not rows:
                return await _send_vf_menu(update, "No approved history.")
            ok = 0
            bad = 0
            for row in rows:
                entry = get_word_by_id(db_path, row.get("word_id"))
                if not entry:
                    continue
                diff = _vf_compare(entry, row.get("remote_payload") or {})
                if diff:
                    bad += 1
                else:
                    ok += 1
            return await _send_vf_menu(update, f"verify: ok={ok}, still_diff={bad}")

        return await _send_vf_menu(update, "Use parse_VF buttons.")

    if action == "suggested_words":
        suggestions_path = _create_suggestions_dump_file()
        if suggestions_path is None:
            await update.message.reply_text("No suggested words file yet.")
            return SETTINGS_MENU
        await update.message.reply_document(
            document=suggestions_path.open("rb"),
            filename=suggestions_path.name,
            caption="✅ Suggested words dump created.",
        )
        return SETTINGS_MENU

    await update.message.reply_text(
        "Use settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def algo_settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle algo settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)

    if action == "back":
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "drill_sett":
        return await _send_drill_settings_menu(update, user_id)
    if action == "learning_sett":
        return await _send_learning_settings_menu(update, user_id)

    if text == t(lang, "ses_rtr_gap"):
        context.user_data["algo_setting_edit"] = SESSION_RETRY_GAP_KEY
        context.user_data["algo_edit_return"] = "algo"
        await update.message.reply_text(
            "Enter ses.rtr_gap (number of FSRS questions between retries):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "ses_rtr_it"):
        context.user_data["algo_setting_edit"] = SESSION_RETRY_IT_KEY
        context.user_data["algo_edit_return"] = "algo"
        await update.message.reply_text(
            "Enter ses.rtr_it (retry repetitions per word):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "topic_filter"):
        current = _topic_filter_value()
        topics = list_topics(get_db_path())
        topics_line = ", ".join(topics) if topics else "-"
        await update.message.reply_text(
            f"Current topic filter: {current}\nTopics: {topics_line}\nSend e.g. 1+2+food or 'all'.",
            reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
        )
        context.user_data["topic_return"] = "algo"
        return TOPIC_FILTER_EDIT

    if text == t(lang, "unit_tests"):
        report = _run_unit_tests_report()
        await update.message.reply_text(
            f"```text\n{report}\n```",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return ALGO_SETTINGS_MENU

    if text == t(lang, "reset_periodicity"):
        context.user_data["algo_setting_edit"] = "reset_periodicity"
        await update.message.reply_text(
            'Type "yes" to reset periodicity:',
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_CONFIRM

    await update.message.reply_text(
        "Use algo settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return ALGO_SETTINGS_MENU


async def drill_settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle drill settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)

    if action == "back":
        return await _send_algo_settings_menu(update, user_id)

    if text == t(lang, "weak_drill_gap"):
        context.user_data["algo_setting_edit"] = WEAK_DRILL_GAP_KEY
        context.user_data["algo_edit_return"] = "drill"
        await update.message.reply_text(
            "Enter weak.drill_gap (normal weak questions between drill repeats):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "weak_drill_repeats"):
        context.user_data["algo_setting_edit"] = WEAK_DRILL_REPEATS_KEY
        context.user_data["algo_edit_return"] = "drill"
        await update.message.reply_text(
            "Enter weak.drill_repeats (total weak shows per word in session):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT

    await update.message.reply_text(
        "Use drill settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_drill_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return DRILL_SETTINGS_MENU


async def learning_settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle learning settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)
    if action == "back":
        return await _send_algo_settings_menu(update, user_id)
    if text == t(lang, "learning_daily_qty"):
        context.user_data["algo_setting_edit"] = LEARNING_DAILY_QTY_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text("Enter learning.daily_qty:", reply_markup=ReplyKeyboardRemove())
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_intervals"):
        context.user_data["algo_setting_edit"] = LEARNING_INTERVALS_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text(
            "Enter learning.intervals_sec as comma-separated seconds (example: 30,120,600,3600):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_reminder_interval"):
        context.user_data["algo_setting_edit"] = LEARNING_REMINDER_INTERVAL_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text("Enter learning.reminder_interval_sec:", reply_markup=ReplyKeyboardRemove())
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_reminder_cooldown"):
        context.user_data["algo_setting_edit"] = LEARNING_REMINDER_COOLDOWN_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text("Enter learning.reminder_cooldown_sec:", reply_markup=ReplyKeyboardRemove())
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_ready_quorum"):
        context.user_data["algo_setting_edit"] = LEARNING_READY_QUORUM_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text("Enter learning.ready_quorum_pct (1-100):", reply_markup=ReplyKeyboardRemove())
        return ALGO_SETTINGS_EDIT
    await update.message.reply_text(
        "Use learning settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_learning_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_SETTINGS_MENU


async def algo_settings_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle algo settings edit."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    return_to = context.user_data.get("algo_edit_return", "algo")
    if _action_key_from_text(text, user_id) == "back":
        context.user_data.pop("algo_setting_edit", None)
        context.user_data.pop("algo_edit_return", None)
        if return_to == "drill":
            return await _send_drill_settings_menu(update, user_id)
        if return_to == "learning":
            return await _send_learning_settings_menu(update, user_id)
        return await _send_algo_settings_menu(update, user_id)

    key = context.user_data.get("algo_setting_edit")
    if key not in {
        SESSION_RETRY_GAP_KEY,
        SESSION_RETRY_IT_KEY,
        WEAK_DRILL_GAP_KEY,
        WEAK_DRILL_REPEATS_KEY,
        LEARNING_DAILY_QTY_KEY,
        LEARNING_INTERVALS_KEY,
        LEARNING_REMINDER_INTERVAL_KEY,
        LEARNING_REMINDER_COOLDOWN_KEY,
        LEARNING_READY_QUORUM_KEY,
    }:
        if return_to == "drill":
            return await _send_drill_settings_menu(update, user_id)
        if return_to == "learning":
            return await _send_learning_settings_menu(update, user_id)
        return await _send_algo_settings_menu(update, user_id)

    if key == LEARNING_INTERVALS_KEY:
        values = _parse_learning_intervals(text)
        if not values:
            await update.message.reply_text("Enter comma-separated seconds, for example: 30,120,600")
            return ALGO_SETTINGS_EDIT
        set_setting(get_db_path(), key, ",".join(str(v) for v in values))
    else:
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("Enter a number.")
            return ALGO_SETTINGS_EDIT
        if key == LEARNING_READY_QUORUM_KEY:
            if value < 1 or value > 100:
                await update.message.reply_text("Value must be in range 1..100.")
                return ALGO_SETTINGS_EDIT
        elif value < 1:
            await update.message.reply_text("Value must be >= 1.")
            return ALGO_SETTINGS_EDIT
        set_setting(get_db_path(), key, str(value))
    context.user_data.pop("algo_setting_edit", None)
    context.user_data.pop("algo_edit_return", None)
    if return_to == "drill":
        return await _send_drill_settings_menu(update, user_id)
    if return_to == "learning":
        return await _send_learning_settings_menu(update, user_id)
    return await _send_algo_settings_menu(update, user_id)


async def algo_settings_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle algo settings confirm."""
    text = update.message.text.strip().lower()
    user_id = update.effective_user.id
    if text == "yes":
        reset_periodicity(get_db_path())
        await update.message.reply_text("✅ Periodicity reset.")
        return await _send_algo_settings_menu(update, user_id)

    await update.message.reply_text("Canceled.")
    return await _send_algo_settings_menu(update, user_id)


async def topic_filter_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topic filter edit."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if _action_key_from_text(text, user_id) == "back":
        return_to = context.user_data.pop("topic_return", "")
        if return_to == "add_queue":
            await update.message.reply_text(
                "Topic selection canceled. Send a word or list.",
                reply_markup=ReplyKeyboardMarkup(_add_keys(user_id), resize_keyboard=True),
            )
            context.user_data.pop("pending_add_queue", None)
            context.user_data.pop("pending_add_stats", None)
            return ADD_INPUT
        if return_to == "algo":
            return await _send_algo_settings_menu(update, user_id)
        if return_to == "tests_menu":
            await update.message.reply_text(
                t(_user_interface_lang(user_id), "tests") + ":",
                reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
            )
            return TESTS_MENU
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    value = text.strip()
    if not value:
        await update.message.reply_text("Send a non-empty topic or 'all'.")
        return TOPIC_FILTER_EDIT

    set_setting(get_db_path(), TOPIC_FILTER_KEY, value)
    return_to = context.user_data.pop("topic_return", "")
    if return_to == "add_queue":
        pending = context.user_data.pop("pending_add_queue", [])
        stats = context.user_data.pop("pending_add_stats", {})
        context.user_data["add_queue"] = pending
        return await _process_next_ai_word(update, context)
    if return_to == "ru_de_menu":
        await update.message.reply_text(
            f"✅ Topic filter set to: {value}",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    if return_to == "algo":
        await update.message.reply_text(
            f"✅ Topic filter set to: {value}",
            reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return ALGO_SETTINGS_MENU
    if return_to == "tests_menu":
        await update.message.reply_text(
            f"✅ Topic filter set to: {value}",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU

    await update.message.reply_text(
        f"✅ Topic filter set to: {value}",
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


# dump_menu_choice / auto_dump_choice moved to bot/handlers/dump.py

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
        f"```text\n{_format_precheck_list(items)}\n```",
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_PRECHECK


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
            "Choose topic for this list:"
        ),
        reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
    )
    context.user_data["topic_return"] = "add_queue"
    return TOPIC_FILTER_EDIT


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
            "Choose topic for this list:"
        ),
        reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
    )
    context.user_data["topic_return"] = "add_queue"
    return TOPIC_FILTER_EDIT


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
        f"```text\n{_format_precheck_list(items)}\n```",
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_PRECHECK


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


# clarify_fields_input moved to bot/handlers/clarify_fields.py

# _return_from_synonyms / synonyms_menu / synonyms_add_input / synonyms_remove_input / synonyms_auto_confirm / synonyms_clear_confirm moved to bot/handlers/synonyms.py


async def _flag_current_word(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    """Handle flag current word."""
    entry, _ = _get_clarify_target(context)
    if not entry:
        return await _back_to_tests(update, context)
    word_id = entry.get("id")
    if not word_id:
        return await _back_to_tests(update, context)
    add_word_flag(get_db_path(), word_id=word_id, user_id=update.effective_user.id, comment="")
    if return_state == QUIZ_RU_DE:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_RU_DE)
    if return_state == QUIZ_DE_RU:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_DE_RU)
    if return_state == QUIZ_PL_SG:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PL_SG)
    if return_state == QUIZ_ARTICLE:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE_WEAK)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PLURAL_WEAK)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PRATERITUM_WEAK)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PARTIZIP2_WEAK)
    return await _back_to_tests(update, context)


# quiz handlers (quiz_ru_de/de_ru/pl_sg/article) moved to bot/handlers/quiz.py


# weak-mode feedback formatters moved to bot/ui/formatting.py


# quiz_article_weak / quiz_plural_weak / quiz_prateritum_weak / quiz_partizip2_weak moved to bot/handlers/weak.py


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


def _get_ru_de_answer(entry):
    """Handle get ru de answer."""
    if entry.get("type") == "verb":
        return entry.get("infinitive", "")
    return entry.get("german", "")


# answer-check helpers (_split_answer_parts/_normalize_plural_value/_synonym_entries/_check_ru_de_strict) moved to bot/handlers/quiz.py


# entry_translation/article/status/hint display helpers moved to bot/ui/formatting.py


# ru/de-ru/pl-sg question + pl_sg helpers moved to bot/ui/formatting.py


# article/ru_de/de_ru feedback + learning preview formatters moved to bot/ui/formatting.py


# _pick_due_entry moved to bot/common/session.py


# session-retry glue + _reset_fsrs_cycle_state moved to bot/common/session.py


# weak-drill glue moved to bot/common/session.py


# _pick_weak_entry / _pick_weak_or_drill_entry and the four _send_*_weak_prompt senders moved to bot/handlers/weak.py


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
        "For all other allowed types return: type, german, russian. "
        "Use Russian translations in singular only. No extra text."
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


def _format_precheck_list(items):
    """Handle format precheck list."""
    lines = [f"Precheck list (topic: {_current_topic_value()}):"]
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
        if forced_type in {"nomen", "verb", "phrase"} and lookup_word:
            items.append(
                {
                    "original": raw,
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


def _normalize_ai_entry(entry):
    """Handle normalize ai entry."""
    entry = dict(entry)
    entry_type = _type_label(entry.get("type", ""))
    entry["type"] = entry_type
    topic = _current_topic_value()

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
    if warn:
        lines += ["", warn]
    return "\n".join(lines)


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

    entry = _normalize_ai_entry(entry)
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


# _back_to_main_menu / _back_to_tests moved to bot/common/navigation.py




# _interval_divisor_for_mode / _update_srs_for_entry moved to bot/handlers/quiz.py


# dump-file creation helpers moved to bot/common/dumps.py


def _append_suggested_words(user, words):
    """Handle append suggested words."""
    items = [str(x).strip() for x in (words or []) if str(x).strip()]
    if not items:
        return

    path = get_suggestions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    user_id = getattr(user, "id", 0)
    username = getattr(user, "username", "") or "-"
    first_name = getattr(user, "first_name", "") or "-"

    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n[{ts}] user_id={user_id} username=@{username} name={first_name}\n")
        for word in items:
            f.write(f"{word}\n")


def _configure_logging():
    """Handle configure logging."""
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = TimedRotatingFileHandler(
        filename=log_path,
        when="D",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    _cleanup_old_dump_logs()


def _cleanup_old_dump_logs(days=14):
    """Handle cleanup old dump logs."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 24 * 60 * 60)
    dump_dir = get_dump_dir()
    if not dump_dir.exists():
        return
    for path in dump_dir.glob("bot-logs-*.txt"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


# auto-dump job + _configure_auto_dump moved to bot/jobs/auto_dump.py


# learning reminder + FSRS report/peak jobs moved to bot/jobs/reminders.py and bot/jobs/reports.py


def _prepare_storage():
    """Handle prepare storage."""
    db_path = get_db_path()
    init_db(db_path)
    if not get_setting(db_path, OWNER_INTERFACE_LANG_KEY):
        set_setting(db_path, OWNER_INTERFACE_LANG_KEY, "en")
    if not get_setting(db_path, OWNER_DICT_LANG_KEY):
        set_setting(db_path, OWNER_DICT_LANG_KEY, "ru")
    if not get_setting(db_path, TOPIC_FILTER_KEY):
        set_setting(db_path, TOPIC_FILTER_KEY, "1")
    if not get_setting(db_path, WEAK_DRILL_GAP_KEY):
        set_setting(db_path, WEAK_DRILL_GAP_KEY, str(WEAK_DRILL_GAP_DEFAULT))
    if not get_setting(db_path, WEAK_DRILL_REPEATS_KEY):
        set_setting(db_path, WEAK_DRILL_REPEATS_KEY, str(WEAK_DRILL_REPEATS_DEFAULT))
    if not get_setting(db_path, LEARNING_DAILY_QTY_KEY):
        set_setting(db_path, LEARNING_DAILY_QTY_KEY, str(LEARNING_DAILY_QTY_DEFAULT))
    if not get_setting(db_path, LEARNING_INTERVALS_KEY):
        set_setting(db_path, LEARNING_INTERVALS_KEY, LEARNING_INTERVALS_DEFAULT)
    if not get_setting(db_path, LEARNING_REMINDER_INTERVAL_KEY):
        set_setting(db_path, LEARNING_REMINDER_INTERVAL_KEY, str(LEARNING_REMINDER_INTERVAL_DEFAULT))
    if not get_setting(db_path, LEARNING_REMINDER_COOLDOWN_KEY):
        set_setting(db_path, LEARNING_REMINDER_COOLDOWN_KEY, str(LEARNING_REMINDER_COOLDOWN_DEFAULT))
    if not get_setting(db_path, LEARNING_READY_QUORUM_KEY):
        set_setting(db_path, LEARNING_READY_QUORUM_KEY, str(LEARNING_READY_QUORUM_DEFAULT))
    if not get_setting(db_path, FSRS_REPORT_HOUR_KEY):
        set_setting(db_path, FSRS_REPORT_HOUR_KEY, str(FSRS_REPORT_UTC_HOUR_DEFAULT))
    if not get_setting(db_path, FSRS_WEEKLY_REPORT_WEEK_KEY):
        set_setting(db_path, FSRS_WEEKLY_REPORT_WEEK_KEY, "")
    cleanup_transaction_logs(db_path, days=7)


def main():
    """Handle main."""
    load_dotenv()
    _configure_logging()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing in .env")

    _prepare_storage()

    app = Application.builder().token(token).build()

    mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
    _configure_auto_dump(app, mode)
    _configure_learning_reminders(app)
    _configure_fsrs_reports(app)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("undo", undo_steps),
            MessageHandler(filters.Regex(f"^{START_TEXT}$") & ~filters.COMMAND, start),
        ],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            SELECT_UI_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_ui_lang)],
            SELECT_DICT_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_dict_lang)],
            TESTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, tests_menu_choice)],
            DB_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, db_search_input)],
            DB_SEARCH_VIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, db_search_view)],
            LEARNING_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, learning_menu_choice)],
            LEARNING_QUIZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, learning_quiz)],
            QUIZ_RU_DE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de_menu_choice)],
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_menu_choice)],
            ALGO_SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_menu_choice)],
            DRILL_SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, drill_settings_menu_choice)],
            LEARNING_SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, learning_settings_menu_choice)],
            ALGO_SETTINGS_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_edit)],
            ALGO_SETTINGS_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_confirm)],
            TOPIC_FILTER_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, topic_filter_edit)],
            DUMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, dump_menu_choice)],
            AUTO_DUMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, auto_dump_choice)],
            ADD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_input),
                MessageHandler(filters.Document.ALL, add_document),
            ],
            ADD_PRECHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_precheck)],
            ADD_PRECHECK_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_precheck_edit)],
            ADD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirm)],
            ADD_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_clarify)],
            ADD_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, clarify_fields_input)],
            QUIZ_RU_DE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de)],
            QUIZ_DE_RU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_de_ru)],
            QUIZ_PL_SG: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_pl_sg)],
            QUIZ_ARTICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_article)],
            QUIZ_ARTICLE_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_article_weak)],
            QUIZ_PLURAL_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_plural_weak)],
            QUIZ_VERB_WEAK_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_verb_weak_menu_choice)],
            QUIZ_PRATERITUM_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_prateritum_weak)],
            QUIZ_PARTIZIP2_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_partizip2_weak)],
            SYN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_menu)],
            SYN_ADD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_add_input)],
            SYN_REMOVE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_remove_input)],
            SYN_AUTO_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_auto_confirm)],
            SYN_CLEAR_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_clear_confirm)],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("undo", undo_steps)],
    )

    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
