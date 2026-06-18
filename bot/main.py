
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
from .handlers.learning import (
    _send_learning_menu,
    learning_menu_choice,
    learning_quiz,
)
from .handlers.add_words import (
    _process_next_ai_word,
    add_clarify,
    add_confirm,
    add_document,
    add_input,
    add_precheck,
    add_precheck_edit,
)
from .handlers.settings import (
    algo_settings_confirm,
    algo_settings_edit,
    algo_settings_menu_choice,
    drill_settings_menu_choice,
    learning_settings_menu_choice,
    select_dict_lang,
    select_ui_lang,
    settings_menu_choice,
    topic_filter_edit,
)
from .handlers.menu import (
    menu_choice,
    quiz_ru_de_menu_choice,
    start,
    tests_menu_choice,
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
    _add_input_markup,
    _add_keys,
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


# _build_learning_topic_mix_queue / _pick_learning_due_entry moved to bot/handlers/learning.py


# topic-filter + fsrs-report-hour helpers moved to bot/common/settings.py


# settings sub-menus / language select / topic_filter_edit / unit-tests report moved to bot/handlers/settings.py
# menu routers (start, menu_choice, tests_menu_choice, quiz_ru_de_menu_choice) + _load_info_text moved to bot/handlers/menu.py
def _parse_level_toggle(text):
    """Handle parse level toggle."""
    token = (text or "").strip().split(" ", 1)[0].upper()
    return token if token in {"A1", "A2", "B1", "B2", "C1", "C2"} else ""


# _clarify_fields_markup / _type_choice_markup moved to bot/ui/keyboards.py


# add-word + AI recognition pipeline moved to bot/handlers/add_words.py
def _is_owner_only(user_id):
    """Handle is owner only."""
    return is_owner(user_id)


# settings sub-menus / language select / topic_filter_edit / unit-tests report moved to bot/handlers/settings.py
# menu routers (start, menu_choice, tests_menu_choice, quiz_ru_de_menu_choice) + _load_info_text moved to bot/handlers/menu.py
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


# menu routers (start, menu_choice, tests_menu_choice, quiz_ru_de_menu_choice) + _load_info_text moved to bot/handlers/menu.py
def _safe_text(value):
    return str(value or "")


# vf parse/fetch/compare/apply moved to bot/verb_forms_parsing/parser.py


# _vf_run_dry_task / _send_vf_menu moved to bot/verb_forms_parsing/handlers.py


# settings_menu_choice (Settings root router) moved to bot/handlers/settings.py
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


# add-word + AI recognition pipeline moved to bot/handlers/add_words.py
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


# add-word + AI recognition pipeline moved to bot/handlers/add_words.py
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
