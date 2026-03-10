
import asyncio
import logging
import os
import random
import re
import shutil
import subprocess
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, time, timezone

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import RetryAfter
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
    SM2_REPORT_HOUR_KEY,
    SM2_REPORT_DATE_KEY,
    SM2_PEAK_NOTIFY_DATE_KEY,
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
    list_sm2_weak_words,
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
    get_daily_first_touch_retention,
    _parse_learning_intervals,
    sm2_due_forecast,
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
    md_escape as _md_escape,
    normalize_answer as _normalize_answer,
    normalize_lookup_text as _normalize_lookup_text,
    normalize_russian_singular as _normalize_russian_singular,
    russian_variants as _russian_variants,
    try_parse_json as _try_parse_json,
    type_label as _type_label,
)
from .retry_logic import maybe_take_retry_entry, register_retry_result


OWNER_INTERFACE_LANG_KEY = "owner_interface_lang"
OWNER_DICT_LANG_KEY = "owner_dict_lang"
TEST_ARTICLE_WEAK_TEXT = "Article Weak Mode"
TEST_PLURAL_WEAK_TEXT = "Plural Weak Mode"
TEST_VERB_WEAK_TEXT = "Verb Forms Weak Mode"
TEST_PRATERITUM_WEAK_TEXT = "Prateritum Weak Mode"
TEST_PARTIZIP2_WEAK_TEXT = "Partizip2 Weak Mode"
STANDARD_SM2_TEXT = "Standart mode SM2"
SYNONYMS_TEXT = "synonyms"
SYN_ADD_TEXT = "add"
SYN_REMOVE_TEXT = "remove"
SYN_AUTO_TEXT = "auto by"
SYN_CLEAR_TEXT = "clear group"
SYN_FLAG_TEXT = "flag!"
SYN_CONFIRM_TEXT = "Confirm"
SYN_CANCEL_TEXT = "Cancel"
QUIZ_FIELDS_TEXT = "fields"
QUIZ_RU_DE_TEXT = "Ru > De"
QUIZ_DE_RU_TEXT = "De > Ru"
LEARNING_TEXT = "Learning"
LEARNING_START_TEXT = "learning session"
LEARNING_ADD_TEXT = "add for learning"
LEARNING_SKIP_TEXT = "skip_learning"
LEARNING_NEXT_TEXT = "next"
LEARNING_RESET_TEXT = "reset learning"
SEND_LEARN_TEXT = "send_learn"

RETRY_GAP_QUESTIONS = 3
RETRY_MAX_REPEATS = 4
WEAK_DRILL_GAP_DEFAULT = 4
WEAK_DRILL_REPEATS_DEFAULT = 10
LEARNING_DAILY_QTY_DEFAULT = 20
LEARNING_INTERVALS_DEFAULT = "30,120,600,3600,10800,18000"
LEARNING_REMINDER_INTERVAL_DEFAULT = 60
LEARNING_REMINDER_COOLDOWN_DEFAULT = 900
LEARNING_READY_QUORUM_DEFAULT = 70
LEARNING_REMINDER_JOB_NAME = "learning_reminder_job"
SM2_REPORT_UTC_HOUR_DEFAULT = 3
SM2_REPORT_JOB_NAME = "sm2_daily_report_job"
SM2_PEAK_JOB_NAME = "sm2_peak_notify_job"


def _main_menu_keys(user_id):
    """Handle main menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "add_word"), t(lang, "tests"), t(lang, "db_search")],
        [t(lang, "settings"), t(lang, "stat")],
    ]


def _main_menu_markup(user_id):
    """Handle main menu markup."""
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _user_interface_lang(user_id):
    """Handle user interface lang."""
    lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "en")
    return normalize_interface_lang(lang, default="en")


def _user_dict_lang(user_id):
    """Handle user dict lang."""
    dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "ru")
    return normalize_dict_lang(dict_lang, default="ru")


def _dict_label(user_id):
    """Handle dict label."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "Ua"
    if dict_lang == "en":
        return "En"
    return "Ru"


def _mode_l1_de(user_id):
    """Handle mode l1 de."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_RU_DE
    return f"{dict_lang}_de"


def _mode_de_l1(user_id):
    """Handle mode de l1."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_DE_RU
    return f"de_{dict_lang}"


def _tests_menu_keys(user_id):
    """Handle tests menu keys."""
    return [
        [LEARNING_TEXT, QUIZ_RU_DE_TEXT, QUIZ_DE_RU_TEXT],
        [t(_user_interface_lang(user_id), "back")],
    ]


def _ru_de_menu_keys(user_id):
    """Handle ru de menu keys."""
    return [
        [STANDARD_SM2_TEXT, TEST_ARTICLE_WEAK_TEXT],
        [TEST_PLURAL_WEAK_TEXT, TEST_VERB_WEAK_TEXT],
        [t(_user_interface_lang(user_id), "topic_filter"), t(_user_interface_lang(user_id), "back")],
    ]


def _learning_menu_keys(user_id):
    """Handle learning menu keys."""
    return [
        [LEARNING_START_TEXT, LEARNING_ADD_TEXT, LEARNING_RESET_TEXT],
        [t(_user_interface_lang(user_id), "back")],
    ]


def _settings_menu_keys(user_id):
    """Handle settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        ["flags!", t(lang, "algo_settings")],
        [t(lang, "interface_lang_btn"), t(lang, "dict_lang_btn")],
        [t(lang, "dump"), t(lang, "back")],
    ]


def _algo_settings_menu_keys(user_id):
    """Handle algo settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "topic_filter"), t(lang, "ses_rtr_gap"), t(lang, "ses_rtr_it")],
        [t(lang, "drill_sett"), t(lang, "learning_sett"), t(lang, "unit_tests")],
        [t(lang, "reset_periodicity")],
        [t(lang, "back")],
    ]


def _drill_settings_menu_keys(user_id):
    """Handle drill settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "weak_drill_gap"), t(lang, "weak_drill_repeats")],
        [t(lang, "back")],
    ]


def _learning_settings_menu_keys(user_id):
    """Handle learning settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "learning_daily_qty"), t(lang, "learning_intervals"), t(lang, "learning_ready_quorum")],
        [t(lang, "learning_reminder_interval"), t(lang, "learning_reminder_cooldown"), t(lang, "back")],
    ]


def _get_int_setting(db_path, key, default):
    """Handle get int setting."""
    raw = get_setting(db_path, key, None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return value


def _session_retry_gap(db_path):
    """Handle session retry gap."""
    value = _get_int_setting(db_path, SESSION_RETRY_GAP_KEY, RETRY_GAP_QUESTIONS)
    return max(1, value)


def _session_retry_iterations(db_path):
    """Handle session retry iterations."""
    value = _get_int_setting(db_path, SESSION_RETRY_IT_KEY, RETRY_MAX_REPEATS)
    return max(1, value)


def _weak_drill_gap(db_path):
    """Handle weak drill gap."""
    value = _get_int_setting(db_path, WEAK_DRILL_GAP_KEY, WEAK_DRILL_GAP_DEFAULT)
    return max(1, value)


def _weak_drill_repeats(db_path):
    """Handle weak drill repeats."""
    value = _get_int_setting(db_path, WEAK_DRILL_REPEATS_KEY, WEAK_DRILL_REPEATS_DEFAULT)
    return max(1, value)


def _learning_daily_qty(db_path):
    """Handle learning daily qty."""
    value = _get_int_setting(db_path, LEARNING_DAILY_QTY_KEY, LEARNING_DAILY_QTY_DEFAULT)
    return max(1, value)


def _learning_intervals_seconds(db_path):
    """Handle learning intervals seconds."""
    raw = get_setting(db_path, LEARNING_INTERVALS_KEY, LEARNING_INTERVALS_DEFAULT)
    values = _parse_learning_intervals(raw)
    if not values:
        values = _parse_learning_intervals(LEARNING_INTERVALS_DEFAULT)
    return values


def _learning_intervals_text(db_path):
    """Handle learning intervals text."""
    return ",".join(str(v) for v in _learning_intervals_seconds(db_path))


def _learning_reminder_interval(db_path):
    """Handle learning reminder interval."""
    value = _get_int_setting(db_path, LEARNING_REMINDER_INTERVAL_KEY, LEARNING_REMINDER_INTERVAL_DEFAULT)
    return max(10, value)


def _learning_reminder_cooldown(db_path):
    """Handle learning reminder cooldown."""
    value = _get_int_setting(db_path, LEARNING_REMINDER_COOLDOWN_KEY, LEARNING_REMINDER_COOLDOWN_DEFAULT)
    return max(60, value)


def _learning_ready_quorum(db_path):
    """Handle learning ready quorum."""
    value = _get_int_setting(db_path, LEARNING_READY_QUORUM_KEY, LEARNING_READY_QUORUM_DEFAULT)
    return min(100, max(1, value))


def _sm2_report_hour_utc(db_path):
    """Handle sm2 report hour utc."""
    value = _get_int_setting(db_path, SM2_REPORT_HOUR_KEY, SM2_REPORT_UTC_HOUR_DEFAULT)
    return min(23, max(0, value))


def _topic_filter_value():
    """Handle topic filter value."""
    raw = str(get_setting(get_db_path(), TOPIC_FILTER_KEY, "1") or "").strip()
    return raw or "1"


def _parse_topic_filter(raw):
    """Handle parse topic filter."""
    if not raw:
        return []
    normalized = raw.strip().lower()
    if normalized in {"all", "0", "*"}:
        return []
    tokens = []
    for chunk in raw.replace(",", "+").split("+"):
        token = chunk.strip()
        if token == "0":
            continue
        if token:
            tokens.append(token)
    return tokens


def _topic_filter_for_query():
    """Handle topic filter for query."""
    raw = _topic_filter_value()
    topics = _parse_topic_filter(raw)
    return topics or None


def _topic_filter_label():
    """Handle topic filter label."""
    raw = _topic_filter_value()
    topics = _parse_topic_filter(raw)
    if not topics:
        return "all"
    return "+".join(topics)


def _topic_filter_banner():
    """Handle topic filter banner."""
    label = _topic_filter_label()
    return f"Topic filter:\n\n🟥 *{_md_escape(label)}* 🟥"


def _current_topic_value():
    """Handle current topic value."""
    raw = _topic_filter_value()
    topics = _parse_topic_filter(raw)
    if not topics:
        return "1"
    return topics[0]


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
    """Handle run unit tests report."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [
        "python3",
        "-m",
        "unittest",
        "-v",
        "tests.test_retry_logic",
        "tests.test_sm2_logic",
        "tests.test_weak_modes",
        "tests.test_learning_logic",
    ]
    try:
        env = dict(os.environ)
        env["PYTHONWARNINGS"] = "ignore::ResourceWarning"
        proc = subprocess.run(
            cmd,
            cwd=root_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
    except Exception as exc:
        return f"unit tests failed to run: {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    text = out
    if err:
        text = f"{text}\n{err}".strip()
    if not text:
        text = f"exit_code={proc.returncode}"
    if len(text) > 3500:
        text = text[:3500] + "\n...truncated..."
    return text.replace("```", "'''")


def _dump_menu_keys(user_id):
    """Handle dump menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "dump_logs"), t(lang, "dump_db")],
        [t(lang, "auto_dump"), t(lang, "back")],
    ]


def _action_key_from_text(text, user_id):
    """Handle action key from text."""
    lang = _user_interface_lang(user_id)
    keys = [
        ("start", "start"),
        ("back", "back"),
        ("stop", "stop"),
        ("add_word", "add_word"),
        ("tests", "tests"),
        ("db_search", "db_search"),
        ("db_search_matches", "db_search_matches"),
        ("db_search_weak_words", "db_search_weak_words"),
        ("settings", "settings"),
        ("info", "info"),
        ("stat", "stat"),
        ("quiz_article", "quiz_article"),
        ("dump_db", "dump_db"),
        ("dump_logs", "dump_logs"),
        ("dump", "dump"),
        ("suggested_words", "suggested_words"),
        ("review_flags", "review_flags"),
        ("algo_settings", "algo_settings"),
        ("topic_filter", "topic_filter"),
        ("drill_sett", "drill_sett"),
        ("learning_sett", "learning_sett"),
        ("learning_daily_qty", "learning_daily_qty"),
        ("learning_intervals", "learning_intervals"),
        ("learning_reminder_interval", "learning_reminder_interval"),
        ("learning_reminder_cooldown", "learning_reminder_cooldown"),
        ("learning_ready_quorum", "learning_ready_quorum"),
        ("auto_dump", "auto_dump"),
        ("daily", "daily"),
        ("weekly", "weekly"),
        ("off", "off"),
        ("interface_lang_btn", "interface_lang"),
        ("dict_lang_btn", "dict_lang"),
    ]
    mapping = {}
    for key, action in keys:
        mapping[t(lang, key)] = action
        mapping[t("en", key)] = action
    mapping[QUIZ_RU_DE_TEXT] = "quiz_l1_de"
    mapping[QUIZ_DE_RU_TEXT] = "quiz_de_l1"
    mapping[LEARNING_TEXT] = "learning"
    mapping[LEARNING_START_TEXT] = "learning_start"
    mapping[LEARNING_ADD_TEXT] = "learning_add"
    mapping[LEARNING_SKIP_TEXT] = "learning_skip"
    mapping[LEARNING_NEXT_TEXT] = "learning_next"
    mapping[LEARNING_RESET_TEXT] = "learning_reset"
    mapping[SEND_LEARN_TEXT] = "send_learn"
    mapping["flags!"] = "review_flags"
    mapping["Back"] = "back"
    mapping["Stop"] = "stop"
    return mapping.get(text.strip(), "")


def _auto_dump_keys(user_id):
    """Handle auto dump keys."""
    lang = _user_interface_lang(user_id)
    return [[t(lang, "daily"), t(lang, "weekly")], [t(lang, "off"), t(lang, "back")]]


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


def _db_search_markup(user_id):
    """Handle db search markup."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup([[t(lang, "db_search_matches"), t(lang, "db_search_weak_words"), t(lang, "back")]], resize_keyboard=True)


def _db_search_weak_markup(user_id):
    """Handle db search weak markup."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup([[SEND_LEARN_TEXT, t(lang, "back")]], resize_keyboard=True)


def _split_text_chunks(text, max_size=3500):
    """Handle split text chunks."""
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


def _quiz_markup(user_id, article=False, allow_idk=True):
    """Handle quiz markup."""
    lang = _user_interface_lang(user_id)
    rows = []
    if article:
        rows.append(["der", "die", "das"])
    elif allow_idk:
        rows.append([SEND_LEARN_TEXT, I_DONT_KNOW_TEXT, INPUT_ERR_TEXT])
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, t(lang, "stop")])
    else:
        rows.append([t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _quiz_idle_markup(user_id):
    """Handle quiz idle markup."""
    lang = _user_interface_lang(user_id)
    rows = [[SEND_LEARN_TEXT, INPUT_ERR_TEXT]]
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, t(lang, "back")])
    else:
        rows.append([t(lang, "back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _weak_article_markup(user_id):
    """Handle weak article markup."""
    lang = _user_interface_lang(user_id)
    rows = [["der", "die", "das"]]
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, INPUT_ERR_TEXT, t(lang, "stop")])
    else:
        rows.append([INPUT_ERR_TEXT, t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _parse_level_toggle(text):
    """Handle parse level toggle."""
    token = (text or "").strip().split(" ", 1)[0].upper()
    return token if token in {"A1", "A2", "B1", "B2", "C1", "C2"} else ""


def _edit_fields_markup(source):
    """Handle edit fields markup."""
    if source == "add":
        return ReplyKeyboardMarkup(
            [
                ["ru", "ukr", "en", "de"],
                ["article", "plural", "type", "infinitive"],
                ["prateritum", "partizip2", "komparativ", "superlativ"],
                [BACK_TEXT],
            ],
            resize_keyboard=True,
        )
    if source == "quiz":
        return ReplyKeyboardMarkup(
            [
                ["ru", "ukr", "de", SYNONYMS_TEXT],
                ["article", "plural", "type", "infinitive"],
                ["prateritum", "partizip2", "komparativ", "superlativ"],
                [BACK_TEXT],
            ],
            resize_keyboard=True,
        )
    return ReplyKeyboardMarkup([["russian", BACK_TEXT]], resize_keyboard=True)


def _allowed_edit_fields(source):
    """Handle allowed edit fields."""
    if source == "add":
        return {
            "ru", "ukr", "en", "de", "article", "plural", "type",
            "prateritum", "partizip2", "infinitive", "komparativ", "superlativ",
        }
    if source == "quiz":
        return {
            "ru", "ukr", "de", "article", "plural", "prateritum",
            "partizip2", "infinitive", "type", SYNONYMS_TEXT, "komparativ", "superlativ",
        }
    return {"russian", "level"}


def _quiz_edit_root_markup(user_id):
    """Handle quiz edit root markup."""
    return ReplyKeyboardMarkup([[QUIZ_FIELDS_TEXT], ["delete", t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


def _dict_lang_tag(user_id):
    """Handle dict lang tag."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "UA"
    if dict_lang == "en":
        return "EN"
    return "RU"


def _syn_auto_text(user_id):
    """Handle syn auto text."""
    return f"{SYN_AUTO_TEXT} {_dict_lang_tag(user_id)}"


def _set_entry_field(entry, raw_update):
    """Handle set entry field."""
    if "=" not in raw_update:
        return None, "Use format: field=value"
    field, value = raw_update.split("=", 1)
    field = field.strip().lower()
    value = value.strip()

    alias = {
        "ru": "russian",
        "ukr": "ukrainian",
        "en": "english",
        "de": "german",
    }
    field = alias.get(field, field)

    allowed = {
        "russian",
        "ukrainian",
        "english",
        "article",
        "plural",
        "prateritum",
        "partizip2",
        "komparativ",
        "superlativ",
        "german",
        "infinitive",
        "type",
    }
    if field not in allowed:
        return None, f"Unknown field: {field}"

    updated = dict(entry)
    if field == "type":
        normalized_type = _type_label(value)
        if normalized_type not in ALLOWED_AI_TYPES:
            return None, "Invalid type."
        updated["type"] = normalized_type
    elif field == "russian":
        updated["russian"] = value
    else:
        updated[field] = value
    return updated, None


def _set_entry_field_value(entry, field, value):
    """Handle set entry field value."""
    return _set_entry_field(entry, f"{field}={value}")


def _get_quiz_action_entry(context: ContextTypes.DEFAULT_TYPE, prefer_current=False):
    """Handle get quiz action entry."""
    current = context.user_data.get("quiz_entry")
    last_answered = context.user_data.get("quiz_last_answered_entry")
    if prefer_current and current:
        return current, "Current quiz word:"
    if last_answered:
        return last_answered, "Last answered word:"
    if current:
        return current, "Current quiz word:"
    return None, ""


async def _start_quiz_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state, prefer_current=False):
    """Handle start quiz edit."""
    entry, title = _get_quiz_action_entry(context, prefer_current=prefer_current)
    if not entry:
        return await _back_to_tests(update, context)
    context.user_data["edit_source"] = "quiz"
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_stage"] = "pick_scope"
    context.user_data["edit_return_state"] = return_state
    await update.message.reply_text(_format_word_card(entry, title))
    await update.message.reply_text(
        "Choose edit action:",
        reply_markup=_quiz_edit_root_markup(update.effective_user.id),
    )
    return ADD_EDIT


def _remember_last_answer(context, entry, mode, ok):
    """Handle remember last answer."""
    context.user_data["last_answered_entry"] = entry
    context.user_data["last_answered_mode"] = mode
    context.user_data["last_answered_ok"] = bool(ok)
    context.user_data["last_answered_srs"] = dict(entry.get("srs") or {})


def _clear_last_answer(context):
    """Handle clear last answer."""
    context.user_data.pop("last_answered_entry", None)
    context.user_data.pop("last_answered_mode", None)
    context.user_data.pop("last_answered_ok", None)
    context.user_data.pop("last_answered_srs", None)


def _session_key(mode):
    """Handle session key."""
    return f"session_seen_{mode}"


def _reset_session(context, mode):
    """Handle reset session."""
    context.user_data["session_mode"] = mode
    context.user_data[_session_key(mode)] = set()


def _ensure_session_mode(context, mode):
    """Handle ensure session mode."""
    if context.user_data.get("session_mode") != mode:
        _reset_session(context, mode)


def _mark_session_word(context, mode, word_id):
    """Handle mark session word."""
    if not word_id:
        return
    _ensure_session_mode(context, mode)
    seen = context.user_data.get(_session_key(mode))
    if isinstance(seen, set):
        seen.add(word_id)


def _session_count(context, mode):
    """Handle session count."""
    seen = context.user_data.get(_session_key(mode))
    if isinstance(seen, set):
        return len(seen)
    return 0


async def _stop_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, mode):
    """Handle stop with summary."""
    count = _session_count(context, mode)
    await update.message.reply_text(f"session words = {count}")
    return await _back_to_tests(update, context)


def _remember_last_weak_answer(context, entry, field, ok):
    """Handle remember last weak answer."""
    context.user_data["last_weak_answered_entry"] = entry
    context.user_data["last_weak_answered_field"] = field
    context.user_data["last_weak_answered_ok"] = bool(ok)


def _apply_input_err_weak(update, context, field):
    """Handle apply input error for weak modes."""
    last_entry = context.user_data.get("last_weak_answered_entry")
    last_field = context.user_data.get("last_weak_answered_field")
    last_ok = context.user_data.get("last_weak_answered_ok")
    if not last_entry or last_field != field or last_ok is None:
        return False
    if last_ok:
        return False
    if not apply_weak_input_err(get_db_path(), update.effective_user.id, last_entry.get("id"), field):
        return False
    state = get_form_state(get_db_path(), update.effective_user.id, last_entry.get("id"), field)
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(
            get_db_path(), update.effective_user.id, last_entry.get("id"), field, True
        )
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, last_entry.get("id"), field)
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_entry.get("id"),
        mode=f"weak_{field}",
        is_correct=True,
        review_stage_snapshot=stage_snapshot,
    )
    context.user_data["last_weak_answered_ok"] = True
    return True


def _remove_from_retry(context, mode, word_id):
    """Handle remove from retry."""
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    if word_id in queue:
        queue[:] = [x for x in queue if x != word_id]
    counts.pop(word_id, None)


async def _repeat_current_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle repeat current ru de."""
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, prefix_text)
    prompt_text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_RU_DE


async def _repeat_current_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle repeat current de ru."""
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, prefix_text)
    prompt_text = _format_de_ru_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_DE_RU


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
        first_delay_seconds=_learning_intervals_seconds(db_path)[0],
    )
    return bool(added)


def _apply_input_err(update, context, mode):
    """Handle apply input error correction."""
    last_entry = context.user_data.get("last_answered_entry")
    last_mode = context.user_data.get("last_answered_mode")
    last_ok = context.user_data.get("last_answered_ok")
    last_srs = context.user_data.get("last_answered_srs")
    if not last_entry or last_mode != mode or last_ok is None:
        return False
    if last_ok:
        return False
    if not last_srs:
        return False
    set_progress(get_db_path(), update.effective_user.id, last_entry.get("id"), mode, last_srs)
    update_progress(
        get_db_path(),
        update.effective_user.id,
        last_entry.get("id"),
        mode,
        quality=5,
        interval_divisor=_interval_divisor_for_mode(update.effective_user.id, mode),
    )
    log_quiz_event(get_db_path(), user_id=update.effective_user.id, word_id=last_entry.get("id"), mode=mode, is_correct=True)
    _remove_from_retry(context, mode, last_entry.get("id"))
    _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    context.user_data["last_answered_ok"] = True
    return True


def _apply_input_err_learning(update, context):
    """Handle apply input error for learning mode."""
    last_id = context.user_data.get("last_learning_entry_id")
    last_ok = context.user_data.get("last_learning_ok")
    last_prev_step = context.user_data.get("last_learning_prev_step")
    if not last_id or last_ok is None:
        return False
    if last_ok:
        return False
    result = learning_apply_input_err(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_id,
        prev_step_index=last_prev_step,
        intervals_seconds=_learning_intervals_seconds(get_db_path()),
    )
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=last_id,
        mode="learning",
        is_correct=True,
    )
    context.user_data["last_learning_ok"] = True
    return bool(result)


async def _show_quiz_prompt_after_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, state_override=None):
    """Handle show quiz prompt after edit."""
    context.user_data.pop("quiz_edit_entry", None)
    state = state_override or context.user_data.get("edit_return_state")
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _back_to_tests(update, context)

    if state == QUIZ_RU_DE:
        text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE

    if state == QUIZ_DE_RU:
        text = _format_de_ru_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_DE_RU

    if state == QUIZ_ARTICLE:
        text = _format_article_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=True),
            parse_mode="Markdown",
        )
        return QUIZ_ARTICLE

    if state == QUIZ_ARTICLE_WEAK:
        return await _send_article_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PLURAL_WEAK:
        return await _send_plural_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PRATERITUM_WEAK:
        return await _send_prateritum_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PARTIZIP2_WEAK:
        return await _send_partizip2_weak_prompt(update, context, prefix_text)
    if state == LEARNING_QUIZ:
        return await _send_learning_prompt(update, context, prefix_text)

    if state == DB_SEARCH_VIEW:
        text = _format_db_search_card(entry, _user_dict_lang(update.effective_user.id))
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(text, parse_mode="Markdown")
        context.user_data["edit_stage"] = "pick_scope"
        await update.message.reply_text(
            "Choose edit action:",
            reply_markup=_quiz_edit_root_markup(update.effective_user.id),
        )
        return ADD_EDIT

    return await _back_to_tests(update, context)


def _format_word_card(entry, title="Word card:"):
    """Handle format word card."""
    lines = [
        title,
        f"id: {entry.get('id', '')}",
        f"type: {entry.get('type', '')}",
        f"article: {entry.get('article', '') or '-'}",
        f"german: {entry.get('german', '') or '-'}",
        f"infinitive: {entry.get('infinitive', '') or '-'}",
        f"plural: {entry.get('plural', '') or '-'}",
        f"prateritum: {entry.get('prateritum', '') or '-'}",
        f"partizip2: {entry.get('partizip2', '') or '-'}",
        f"komparativ: {entry.get('komparativ', '') or '-'}",
        f"superlativ: {entry.get('superlativ', '') or '-'}",
        f"russian: {entry.get('russian', '') or '-'}",
        f"ukrainian: {entry.get('ukrainian', '') or '-'}",
        f"english: {entry.get('english', '') or '-'}",
    ]
    return "\n".join(lines)


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


def _format_sm2_weak_line(entry, dict_lang):
    """Handle format sm2 weak line."""
    streak = int(entry.get("wrong_streak") or 0)
    return f"{_format_match_line(entry, dict_lang)} | wrong_streak={streak}"


def _syn_menu_markup(user_id):
    """Handle syn menu markup."""
    return ReplyKeyboardMarkup(
        [
            [SYN_ADD_TEXT, SYN_REMOVE_TEXT, _syn_auto_text(user_id)],
            [SYN_CLEAR_TEXT, SYN_FLAG_TEXT, BACK_TEXT],
        ],
        resize_keyboard=True,
    )


def _syn_confirm_markup():
    """Handle syn confirm markup."""
    return ReplyKeyboardMarkup([[SYN_CONFIRM_TEXT, SYN_CANCEL_TEXT]], resize_keyboard=True)


def _format_syn_group(anchor_entry, group_entries, dict_lang):
    """Handle format syn group."""
    anchor_type = anchor_entry.get("type", "")
    anchor_base = anchor_entry.get("infinitive", "") if anchor_type == "verb" else anchor_entry.get("german", "")
    lines = [f"Synonyms for: {anchor_base} ({anchor_type})"]
    if not group_entries:
        lines.append("- no linked synonyms")
        return "\n".join(lines)
    for idx, item in enumerate(group_entries, start=1):
        item_type = item.get("type", "")
        base = item.get("infinitive", "") if item_type == "verb" else item.get("german", "")
        article = (item.get("article", "") or "").strip()
        left = f"{article} {base}".strip()
        right = _entry_translation(item, dict_lang)
        lines.append(f"{idx}. {left} >> {right}")
    return "\n".join(lines)


async def _open_synonyms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, anchor_entry, return_state):
    """Handle open synonyms menu."""
    if not anchor_entry or not anchor_entry.get("id"):
        return await _back_to_tests(update, context)
    context.user_data["syn_anchor_id"] = anchor_entry.get("id")
    context.user_data["syn_return_state"] = return_state
    group_entries = get_synonym_group(get_db_path(), anchor_entry.get("id"))
    await update.message.reply_text(
        _format_syn_group(anchor_entry, group_entries, _user_dict_lang(update.effective_user.id)),
        reply_markup=_syn_menu_markup(update.effective_user.id),
    )
    return SYN_MENU

async def _reply_text_with_retry(message, text, max_retries=2, **kwargs):
    """Handle reply text with retry."""
    attempts = 0
    while True:
        try:
            return await message.reply_text(text, **kwargs)
        except RetryAfter as exc:
            attempts += 1
            if attempts > max_retries:
                raise
            await asyncio.sleep(int(getattr(exc, "retry_after", 1)) + 1)


def _scan_words_before_ai(lines):
    """Handle scan words before ai."""
    filtered = []
    existing = []
    recognition = []
    for raw in lines:
        word = raw.strip()
        if not word:
            continue
        reason = _prefilter_reject_reason(word)
        if reason:
            filtered.append(word)
            continue
        if find_existing_word(get_db_path(), word):
            existing.append(word)
            continue
        recognition.append(word)
    return filtered, existing, recognition


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


async def _send_db_search_prompt(update: Update, user_id, prefix_text=None):
    """Handle send db search prompt."""
    text = "Send a word to search in DB:"
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(text, reply_markup=_db_search_markup(user_id))
    return DB_SEARCH


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
            f"today complete sm2 Ru > De = {stats['today_complete_sm2_ru_de']}",
            f"today complete sm2 De > Ru = {stats['today_complete_sm2_de_ru']}",
            "",
            f"today complete weak article = {stats['today_complete_weak_article']}",
            f"today complete weak plural = {stats['today_complete_weak_plural']}",
            f"today complete weak partizip2 = {stats['today_complete_weak_partizip2']}",
            f"today complete weak preteritum = {stats['today_complete_weak_prateritum']}",
            "",
            f"3+ correct streak = {stats['streak_3p']}",
            f"2 correct streak = {stats['streak_2']}",
            f"1 correct = {stats['streak_1']}",
            f"wrong sm2 = {stats['wrong_sm2']}",
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
            f"learning->sm2 2+ (week) = {stats['learning_to_sm2_week']}",
            "",
            "_word types:_",
            *[f"_{k} = {word_types[k]}_" for k in sorted(word_types.keys())],
        ]
        await update.message.reply_text(
            f"```text\n{chr(10).join(lines)}\n```",
            reply_markup=_main_menu_markup(user_id),
            parse_mode="Markdown",
        )
        return MENU

    await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
    return MENU


async def db_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle db search input."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        context.user_data.pop("db_search_weak_ids", None)
        await update.message.reply_text(t(_user_interface_lang(user_id), "main_menu"), reply_markup=_main_menu_markup(user_id))
        return MENU
    if text == SEND_LEARN_TEXT and context.user_data.get("db_search_weak_ids"):
        ids = list(dict.fromkeys(context.user_data.get("db_search_weak_ids") or []))
        db_path = get_db_path()
        batch_id = learning_get_or_create_batch(db_path, user_id=user_id, daily_qty=_learning_daily_qty(db_path))
        added = 0
        for wid in ids:
            added += int(
                learning_add_word_by_id(
                    db_path,
                    user_id=user_id,
                    word_id=wid,
                    batch_id=batch_id,
                    first_delay_seconds=_learning_intervals_seconds(db_path)[0],
                )
            )
        context.user_data.pop("db_search_weak_ids", None)
        return await _send_db_search_prompt(update, user_id, f"Added to learning: {added}")
    if action == "db_search_matches":
        context.user_data.pop("db_search_weak_ids", None)
        entries = list_all_words(get_db_path())
        blocks = _list_match_pairs(entries, _user_dict_lang(user_id))
        if not blocks:
            return await _send_db_search_prompt(update, user_id, "No matches found.")
        chunks = _split_text_chunks("\n\n".join(blocks))
        for idx, chunk in enumerate(chunks):
            markup = _db_search_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup)
        return DB_SEARCH
    if action == "db_search_weak_words":
        mode = _mode_l1_de(user_id)
        entries = list_sm2_weak_words(get_db_path(), user_id=user_id, mode=mode, min_wrong_streak=4)
        if not entries:
            context.user_data.pop("db_search_weak_ids", None)
            return await _send_db_search_prompt(update, user_id, "No weak words found.")
        context.user_data["db_search_weak_ids"] = [x.get("id") for x in entries if x.get("id")]
        lines = [_format_sm2_weak_line(entry, _user_dict_lang(user_id)) for entry in entries]
        chunks = _split_text_chunks("\n\n".join(lines))
        for idx, chunk in enumerate(chunks):
            markup = _db_search_weak_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(
                f"```text\n{chunk}\n```",
                reply_markup=markup,
                parse_mode="Markdown",
            )
        return DB_SEARCH
    context.user_data.pop("db_search_weak_ids", None)
    entry = search_word(get_db_path(), text)
    if not entry:
        return await _send_db_search_prompt(update, user_id, "Not found.")
    await update.message.reply_text(
        _format_db_search_card(entry, _user_dict_lang(user_id)),
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        "Choose edit action:",
        reply_markup=ReplyKeyboardMarkup(
            [
                [QUIZ_FIELDS_TEXT],
                ["delete", t(_user_interface_lang(user_id), "back")],
                [t(_user_interface_lang(user_id), "db_search_matches"), t(_user_interface_lang(user_id), "db_search")],
            ],
            resize_keyboard=True,
        ),
    )
    context.user_data["quiz_entry"] = entry
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_source"] = "quiz"
    context.user_data["edit_stage"] = "pick_scope"
    context.user_data["edit_return_state"] = DB_SEARCH_VIEW
    return DB_SEARCH_VIEW


async def db_search_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle db search view."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action in {"back", "db_search", "stop"}:
        context.user_data.pop("db_search_weak_ids", None)
        return await _send_db_search_prompt(update, user_id, None)
    if action in {"db_search_matches", "db_search_weak_words"} or text == SEND_LEARN_TEXT:
        return await db_search_input(update, context)
    if text in {QUIZ_FIELDS_TEXT, "delete"}:
        return await add_edit(update, context)
    # Any other text is treated as a new search query.
    return await db_search_input(update, context)


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
    msg = "\n".join(
        [
            f"{LEARNING_TEXT}:",
            "",
            _topic_filter_banner(),
            "",
            f"{t(_user_interface_lang(user_id), 'learning_daily_qty')} = {_learning_daily_qty(get_db_path())}",
            f"{t(_user_interface_lang(user_id), 'learning_intervals')} = {_learning_intervals_text(get_db_path())}",
            f"active = {stats.get('active_count', 0)}",
            f"preview_pending = {stats.get('preview_pending', 0)}",
            f"due_now = {stats.get('due_count', 0)} / need {stats.get('ready_needed', 0)}",
            f"completed_in_batch = {stats.get('completed_count', 0)}",
            f"next_due = {_format_eta_from_iso(stats.get('next_due'))}",
        ]
    )
    await update.message.reply_text(
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
        _reset_session(context, _mode_de_l1(user_id))
        return await _send_de_ru_prompt(
            update,
            context,
            f"{QUIZ_DE_RU_TEXT}:\n\n{_topic_filter_banner()}",
        )
    if action == "learning":
        return await _send_learning_menu(update, user_id)

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
    if text == STANDARD_SM2_TEXT:
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
    first_delay = _learning_intervals_seconds(db_path)[0]
    added = learning_add_words(
        db_path,
        user_id=user_id,
        words=picked,
        batch_id=batch_id,
        first_delay_seconds=first_delay,
    )
    await update.message.reply_text(
        f"Added for learning: {added}\nStart learning session for first cycle preview.",
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


async def _send_learning_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send learning prompt."""
    user_id = update.effective_user.id
    preview_entry = learning_preview_entry(get_db_path(), user_id=user_id)
    if preview_entry:
        context.user_data["quiz_entry"] = preview_entry
        context.user_data["learning_preview_mode"] = True
        prompt_text = _format_learning_preview_prompt(preview_entry, _user_dict_lang(user_id))
        if prefix_text:
            prompt_text = f"{prefix_text}\n\n{prompt_text}"
        await update.message.reply_text(
            prompt_text,
            reply_markup=ReplyKeyboardMarkup(
                [[LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT], [AI_CLARIFY_FIELD, t(_user_interface_lang(user_id), "back")]],
                resize_keyboard=True,
            ),
            parse_mode="Markdown",
        )
        return LEARNING_QUIZ

    entry = learning_due_entry(get_db_path(), user_id=user_id)
    if not entry:
        context.user_data["learning_preview_mode"] = False
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            f"No due learning words right now.\nnext_due = {_format_eta_from_iso(learning_status_snapshot(get_db_path(), user_id, _learning_ready_quorum(get_db_path())).get('next_due'))}",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    context.user_data["quiz_entry"] = entry
    context.user_data["learning_preview_mode"] = False
    prompt_text = _format_ru_question(entry, _user_dict_lang(user_id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await update.message.reply_text(
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
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU
    if action == "learning_add":
        return await _learning_add_words(update, context)
    if action == "learning_reset":
        learning_reset_session(get_db_path(), user_id=user_id)
        await update.message.reply_text(
            "Learning session reset. Words returned to new.",
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
        )
        return LEARNING_MENU
    if action == "learning_start":
        return await _send_learning_prompt(update, context, None)
    await update.message.reply_text(
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
        return await _start_quiz_edit(update, context, LEARNING_QUIZ, prefer_current=True)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_learning_prompt(update, context, None)

    is_preview = bool(context.user_data.get("learning_preview_mode"))
    if is_preview:
        if action == "learning_skip" or text == LEARNING_SKIP_TEXT:
            learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
            return await _send_learning_prompt(update, context, "Skipped for learning.")
        if action == "learning_next" or text == LEARNING_NEXT_TEXT:
            learning_mark_preview_next(
                get_db_path(),
                user_id=user_id,
                word_id=entry.get("id"),
                first_interval_seconds=_learning_intervals_seconds(get_db_path())[0],
            )
            return await _send_learning_prompt(update, context, "Saved. Next preview word.")
        await update.message.reply_text(
            "Use next/skip/back.",
            reply_markup=ReplyKeyboardMarkup([[LEARNING_NEXT_TEXT, LEARNING_SKIP_TEXT], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return LEARNING_QUIZ

    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_learning(update, context)
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_learning_prompt(update, context, prefix)

    if action == "learning_skip" or text == LEARNING_SKIP_TEXT:
        learning_skip_word(get_db_path(), user_id=user_id, word_id=entry.get("id"))
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
    result = learning_update_result(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        ok=ok,
        intervals_seconds=_learning_intervals_seconds(get_db_path()),
    )
    feedback = _format_feedback_ru_de(entry, ok)
    if result.get("completed"):
        feedback = f"{feedback}\n\nLearning done ✅, sent to sm2"
    return await _send_learning_prompt(update, context, feedback)


async def quiz_verb_weak_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz verb weak menu choice."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        await update.message.reply_text(
            f"{QUIZ_RU_DE_TEXT}:",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    if text == TEST_PRATERITUM_WEAK_TEXT:
        _reset_weak_drill_state(context, "weak_prateritum")
        _reset_session(context, "weak_prateritum")
        return await _send_prateritum_weak_prompt(update, context, None)
    if text == TEST_PARTIZIP2_WEAK_TEXT:
        _reset_weak_drill_state(context, "weak_partizip2")
        _reset_session(context, "weak_partizip2")
        return await _send_partizip2_weak_prompt(update, context, None)
    await update.message.reply_text(
        "Use verb weak mode buttons.",
        reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
    )
    return QUIZ_VERB_WEAK_MENU


async def settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "back":
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
        items = list_pending_word_flags(get_db_path(), limit=500)
        if not items:
            await update.message.reply_text(
                "✅ No flagged words.",
                reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
            )
            return SETTINGS_MENU
        lines = []
        for row in items:
            word_type = row.get("type", "")
            base = row.get("infinitive", "") if word_type == "verb" else row.get("german", "")
            article = (row.get("article", "") or "").strip()
            left = f"{article} {base}".strip()
            right = _entry_translation(row, _user_dict_lang(user_id))
            lines.append(f"{left} >> {right}")
        chunks = _split_text_chunks("\n".join(lines))
        for idx, chunk in enumerate(chunks):
            markup = ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True) if idx == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup)
        return SETTINGS_MENU

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
            "Enter ses.rtr_gap (number of SM2 questions between retries):",
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

    await update.message.reply_text(
        f"✅ Topic filter set to: {value}",
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def dump_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle dump menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "dump_db":
        dump_path = _create_dump_file()
        await update.message.reply_document(
            document=dump_path.open("rb"),
            filename=dump_path.name,
            caption="✅ SQLite dump created.",
        )
        return DUMP_MENU

    if action == "dump_logs":
        logs_path = _create_logs_dump_file()
        if logs_path is None:
            await update.message.reply_text(
                "Log file is empty or not found yet.",
                reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
            )
            return DUMP_MENU
        await update.message.reply_document(
            document=logs_path.open("rb"),
            filename=logs_path.name,
            caption="✅ Logs dump created.",
        )
        return DUMP_MENU

    if action == "auto_dump":
        mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
        await update.message.reply_text(
            f"Current auto dump mode: {mode}",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    await update.message.reply_text(
        "Use dump menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU


async def auto_dump_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle auto dump choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            "dump:",
            reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
        )
        return DUMP_MENU

    mode_map = {"daily": "daily", "weekly": "weekly", "off": "off"}
    if action not in mode_map:
        await update.message.reply_text(
            "Use auto dump buttons.",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    mode = mode_map[action]
    set_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, mode)
    _configure_auto_dump(context.application, mode)
    await update.message.reply_text(
        f"✅ Auto dump mode set: {mode}",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU

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
    context.user_data["precheck_items"] = items
    await _reply_text_with_retry(
        update.message,
        _format_precheck_list(items),
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
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
    recognition = [x.get("word") for x in items if x.get("word")]
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
        if "[" in raw and "]" in raw:
            raw = raw.split("[", 1)[0].strip()
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
    recognition = _parse_precheck_edit_lines(lines)
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
    context.user_data["precheck_items"] = items
    await _reply_text_with_retry(
        update.message,
        _format_precheck_list(items),
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
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
            await update.message.reply_text(_format_word_card(entry, "AI word card:"))
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup("add"),
        )
        return ADD_EDIT

    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)

    if text != AI_CONFIRM:
        await update.message.reply_text(
            "Use: Confirm, Clarify AI, Clarify field, Reject, Ban, or Stop.",
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
            await update.message.reply_text(_format_word_card(entry, "AI word card:"))
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup("add"),
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


async def add_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add edit."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)

    source = context.user_data.get("edit_source", "add")
    if source == "add":
        entry_key = "pending_entry"
    else:
        entry_key = "quiz_edit_entry"
    entry = context.user_data.get(entry_key)
    if not entry:
        await update.message.reply_text("No entry to edit.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    stage = context.user_data.get("edit_stage", "pick_scope" if source == "quiz" else "pick_field")
    if _action_key_from_text(text, update.effective_user.id) == "back":
        if source == "quiz":
            if stage == "pick_scope" and context.user_data.get("edit_return_state") == DB_SEARCH_VIEW:
                context.user_data.pop("quiz_edit_entry", None)
                return await _send_db_search_prompt(update, update.effective_user.id, None)
            if stage == "pick_scope":
                return await _show_quiz_prompt_after_edit(update, context, "❌ Edit canceled.")
            if stage == "pick_field":
                context.user_data["edit_stage"] = "pick_scope"
                await update.message.reply_text(
                    "Choose edit action:",
                    reply_markup=_quiz_edit_root_markup(update.effective_user.id),
                )
                return ADD_EDIT
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_edit_fields_markup(source),
            )
            return ADD_EDIT

        if stage == "pick_field":
            return await _show_ai_preview(update, entry)
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup(source),
        )
        return ADD_EDIT

    if source == "quiz" and stage == "pick_scope":
        token = text.strip().lower()
        if token == QUIZ_FIELDS_TEXT:
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_edit_fields_markup("quiz"),
            )
            return ADD_EDIT
        if token == "delete":
            word_id = entry.get("id")
            if not word_id:
                await update.message.reply_text(
                    "❌ Cannot delete: missing word id.",
                    reply_markup=_quiz_edit_root_markup(update.effective_user.id),
                )
                return ADD_EDIT
            removed = delete_word(get_db_path(), word_id)
            context.user_data.pop("quiz_edit_entry", None)
            current_quiz_entry = context.user_data.get("quiz_entry")
            if current_quiz_entry and current_quiz_entry.get("id") == word_id:
                context.user_data.pop("quiz_entry", None)
            last_answered = context.user_data.get("quiz_last_answered_entry")
            if last_answered and last_answered.get("id") == word_id:
                context.user_data.pop("quiz_last_answered_entry", None)
            context.user_data["edit_stage"] = "pick_scope"
            context.user_data.pop("edit_field", None)
            state = context.user_data.get("edit_return_state")
            prefix = "✅ Deleted." if removed else "❌ Word not found."
            if state == QUIZ_RU_DE:
                return await _send_ru_de_prompt(update, context, prefix)
            if state == QUIZ_DE_RU:
                return await _send_de_ru_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE:
                return await _send_article_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE_WEAK:
                return await _send_article_weak_prompt(update, context, prefix)
            if state == QUIZ_PLURAL_WEAK:
                return await _send_plural_weak_prompt(update, context, prefix)
            if state == QUIZ_PRATERITUM_WEAK:
                return await _send_prateritum_weak_prompt(update, context, prefix)
            if state == QUIZ_PARTIZIP2_WEAK:
                return await _send_partizip2_weak_prompt(update, context, prefix)
            if state == DB_SEARCH_VIEW:
                return await _send_db_search_prompt(update, update.effective_user.id, prefix)
            return await _back_to_tests(update, context)
        await update.message.reply_text(
            "Use buttons: fields, delete, back.",
            reply_markup=_quiz_edit_root_markup(update.effective_user.id),
        )
        return ADD_EDIT

    if stage == "pick_field":
        field = text.strip().lower()
        allowed_fields = _allowed_edit_fields(source)
        if field not in allowed_fields:
            await update.message.reply_text(
                "Use field buttons.",
                reply_markup=_edit_fields_markup(source),
            )
            return ADD_EDIT
        if source == "quiz" and field == SYNONYMS_TEXT:
            state = context.user_data.get("edit_return_state")
            return await _open_synonyms_menu(update, context, entry, state)
        context.user_data["edit_field"] = field

        context.user_data["edit_stage"] = "enter_value"
        await update.message.reply_text(
            f"Enter new value for {field}:",
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True),
        )
        return ADD_EDIT

    field = context.user_data.get("edit_field", "")
    if not field:
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup(source),
        )
        return ADD_EDIT

    updated, error = _set_entry_field_value(entry, field, text)
    if error:
        await update.message.reply_text(
            error,
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True),
        )
        return ADD_EDIT

    context.user_data[entry_key] = updated
    context.user_data["edit_stage"] = "pick_scope" if source == "quiz" else "pick_field"
    context.user_data.pop("edit_field", None)
    if source == "quiz":
        update_word_fields(get_db_path(), updated.get("id"), updated)
        current_quiz_entry = context.user_data.get("quiz_entry")
        if current_quiz_entry and current_quiz_entry.get("id") == updated.get("id"):
            context.user_data["quiz_entry"] = updated
        last_answered = context.user_data.get("quiz_last_answered_entry")
        if last_answered and last_answered.get("id") == updated.get("id"):
            context.user_data["quiz_last_answered_entry"] = updated
        if context.user_data.get("edit_return_state") == DB_SEARCH_VIEW:
            context.user_data.pop("quiz_edit_entry", None)
            return await _send_db_search_prompt(update, update.effective_user.id, "✅ Updated.")
        return await _show_quiz_prompt_after_edit(update, context, "✅ Updated.")
    return await _show_ai_preview(update, updated)

async def _return_from_synonyms(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix="✅ Done."):
    """Handle return from synonyms."""
    anchor_id = context.user_data.get("syn_anchor_id")
    return_state = context.user_data.get("syn_return_state")
    context.user_data.pop("syn_auto_candidates", None)
    context.user_data.pop("syn_anchor_id", None)
    context.user_data.pop("syn_return_state", None)
    entry = get_word_by_id(get_db_path(), anchor_id) if anchor_id else None
    if entry:
        context.user_data["quiz_entry"] = entry
    if return_state == QUIZ_RU_DE:
        return await _send_ru_de_prompt(update, context, prefix)
    if return_state == QUIZ_DE_RU:
        return await _send_de_ru_prompt(update, context, prefix)
    if return_state == QUIZ_ARTICLE:
        return await _send_article_prompt(update, context, prefix)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _send_article_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _send_plural_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _send_prateritum_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _send_partizip2_weak_prompt(update, context, prefix)
    if return_state == DB_SEARCH_VIEW:
        return await _send_db_search_prompt(update, update.effective_user.id, prefix)
    return await _back_to_tests(update, context)


async def synonyms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms menu."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if _action_key_from_text(text, update.effective_user.id) == "back":
        return await _return_from_synonyms(update, context, None)
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor_entry = get_word_by_id(get_db_path(), anchor_id) if anchor_id else None
    if not anchor_entry:
        return await _back_to_tests(update, context)
    if text == SYN_ADD_TEXT:
        await update.message.reply_text("Send German word or id to add:", reply_markup=ReplyKeyboardRemove())
        return SYN_ADD_INPUT
    if text == SYN_REMOVE_TEXT:
        group = get_synonym_group(get_db_path(), anchor_id)
        await update.message.reply_text(
            _format_syn_group(anchor_entry, group, _user_dict_lang(update.effective_user.id)) + "\n\nSend number, word, or id to remove.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SYN_REMOVE_INPUT
    if text == _syn_auto_text(update.effective_user.id):
        candidates = list_synonym_candidates_by_ru(get_db_path(), anchor_id)
        if not candidates:
            await update.message.reply_text(f"No {_syn_auto_text(update.effective_user.id)} candidates.", reply_markup=_syn_menu_markup(update.effective_user.id))
            return SYN_MENU
        context.user_data["syn_auto_candidates"] = [x.get("id") for x in candidates]
        preview = _format_syn_group(anchor_entry, candidates, _user_dict_lang(update.effective_user.id))
        await update.message.reply_text(
            f"Will add {len(candidates)} candidate(s):\n\n{preview}",
            reply_markup=_syn_confirm_markup(),
        )
        return SYN_AUTO_CONFIRM
    if text == SYN_CLEAR_TEXT:
        await update.message.reply_text("Clear whole synonym group?", reply_markup=_syn_confirm_markup())
        return SYN_CLEAR_CONFIRM
    if text == SYN_FLAG_TEXT:
        add_word_flag(get_db_path(), word_id=anchor_id, user_id=update.effective_user.id, comment="synonym_review")
        await update.message.reply_text("✅ Flag saved.", reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    await update.message.reply_text("Use synonyms buttons.", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms add input."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        anchor_entry = get_word_by_id(get_db_path(), context.user_data.get("syn_anchor_id"))
        group = get_synonym_group(get_db_path(), context.user_data.get("syn_anchor_id"))
        await update.message.reply_text(_format_syn_group(anchor_entry or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor_entry = get_word_by_id(get_db_path(), anchor_id)
    target = find_word_by_value(get_db_path(), text)
    if not anchor_entry or not target:
        await update.message.reply_text("Target not found. Send German word or id.")
        return SYN_ADD_INPUT
    if (anchor_entry.get("type", "") or "").lower() != (target.get("type", "") or "").lower():
        await update.message.reply_text("Type mismatch. Only same type is allowed.")
        return SYN_ADD_INPUT
    changed, _ = add_synonym_link(get_db_path(), anchor_id, target.get("id"))
    msg = "✅ Linked." if changed else "✅ Already linked."
    group = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"{msg}\n\n{_format_syn_group(anchor_entry, group, _user_dict_lang(update.effective_user.id))}", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_remove_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms remove input."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        return SYN_MENU
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor = get_word_by_id(get_db_path(), anchor_id)
    group = get_synonym_group(get_db_path(), anchor_id)
    if not anchor or not group:
        return await _return_from_synonyms(update, context, None)
    target = None
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(group):
            target = group[idx - 1]
    if target is None:
        target = find_word_by_value(get_db_path(), text)
    if not target:
        await update.message.reply_text("Target not found in group.")
        return SYN_REMOVE_INPUT
    if target.get("id") == anchor_id:
        await update.message.reply_text("Cannot remove anchor word from itself. Use Clear group.")
        return SYN_REMOVE_INPUT
    removed = remove_synonym_link(get_db_path(), target.get("id"))
    msg = "✅ Removed." if removed else "❌ Not linked."
    group_new = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"{msg}\n\n{_format_syn_group(anchor, group_new, _user_dict_lang(update.effective_user.id))}", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_auto_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms auto confirm."""
    text = update.message.text.strip()
    if text == SYN_CANCEL_TEXT or _action_key_from_text(text, update.effective_user.id) == "back":
        anchor_id = context.user_data.get("syn_anchor_id")
        anchor = get_word_by_id(get_db_path(), anchor_id)
        group = get_synonym_group(get_db_path(), anchor_id)
        await update.message.reply_text(_format_syn_group(anchor or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    if text != SYN_CONFIRM_TEXT:
        await update.message.reply_text("Use Confirm or Cancel.", reply_markup=_syn_confirm_markup())
        return SYN_AUTO_CONFIRM
    anchor_id = context.user_data.get("syn_anchor_id")
    ids = context.user_data.get("syn_auto_candidates") or []
    added = 0
    for wid in ids:
        changed, _ = add_synonym_link(get_db_path(), anchor_id, wid)
        if changed:
            added += 1
    anchor = get_word_by_id(get_db_path(), anchor_id)
    group = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"✅ Auto linked: {added}", reply_markup=_syn_menu_markup(update.effective_user.id))
    await update.message.reply_text(_format_syn_group(anchor or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms clear confirm."""
    text = update.message.text.strip()
    if text == SYN_CANCEL_TEXT or _action_key_from_text(text, update.effective_user.id) == "back":
        return SYN_MENU
    if text != SYN_CONFIRM_TEXT:
        await update.message.reply_text("Use Confirm or Cancel.", reply_markup=_syn_confirm_markup())
        return SYN_CLEAR_CONFIRM
    anchor_id = context.user_data.get("syn_anchor_id")
    cleared = clear_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text("✅ Group cleared." if cleared else "✅ Group already empty.", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def _flag_current_word(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    """Handle flag current word."""
    entry, _ = _get_quiz_action_entry(context)
    if not entry:
        return await _back_to_tests(update, context)
    word_id = entry.get("id")
    if not word_id:
        return await _back_to_tests(update, context)
    add_word_flag(get_db_path(), word_id=word_id, user_id=update.effective_user.id, comment="")
    if return_state == QUIZ_RU_DE:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_RU_DE)
    if return_state == QUIZ_DE_RU:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_DE_RU)
    if return_state == QUIZ_ARTICLE:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE_WEAK)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_PLURAL_WEAK)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_PRATERITUM_WEAK)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_PARTIZIP2_WEAK)
    return await _back_to_tests(update, context)


async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz ru de."""
    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, _mode_l1_de(update.effective_user.id))
    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err(update, context, _mode_l1_de(update.effective_user.id))
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _repeat_current_ru_de(update, context, prefix)
    if text == SEND_LEARN_TEXT:
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        prefix = "✅ Added to learning." if added else "Already in learning."
        return await _repeat_current_ru_de(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_RU_DE)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_RU_DE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))
    if text == I_DONT_KNOW_TEXT:
        ok = False
        form_checks = {}
    else:
        ok, form_checks = _check_ru_de_strict(entry, text)
    mode = _mode_l1_de(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        if form_checks:
            update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), form_checks)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)

    feedback = _format_feedback_ru_de(entry, ok)
    return await _send_ru_de_prompt(update, context, feedback)


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz de ru."""
    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, _mode_de_l1(update.effective_user.id))
    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err(update, context, _mode_de_l1(update.effective_user.id))
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _repeat_current_de_ru(update, context, prefix)
    if text == SEND_LEARN_TEXT:
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        prefix = "✅ Added to learning." if added else "Already in learning."
        return await _repeat_current_de_ru(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_DE_RU)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_DE_RU)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        answer = _normalize_answer(text)
        variants = _russian_variants(_entry_translation(entry, _user_dict_lang(update.effective_user.id)))
        ok = answer in variants
    mode = _mode_de_l1(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)

    feedback = _format_feedback_de_ru(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, MODE_ARTICLE)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_ARTICLE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        expected = entry.get("article", "")
        ok = _normalize_answer(text) == _normalize_answer(expected)
    mode = MODE_ARTICLE
    if not is_retry:
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry)
    if not is_retry:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)

    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_article_prompt(update, context, feedback)


def _weak_status(ok):
    """Handle weak status."""
    return "Correct ✅" if ok else "Wrong ❌"


def _format_feedback_plural_weak(entry, ok):
    """Handle format feedback plural weak."""
    article = str(entry.get("article", "")).strip()
    german = _cap_word(entry.get("german", ""))
    plural = _cap_word(entry.get("plural", ""))
    headline = f"{_weak_status(ok)} >>>     {_md_escape((article + ' ' + german).strip())}"
    if plural:
        return f"{headline}\n\n_pl. die {_md_escape(plural)}_"
    return headline


def _format_feedback_prateritum_weak(entry, ok):
    """Handle format feedback prateritum weak."""
    infinitive = _low_word(entry.get("infinitive", ""))
    pr = _low_word(entry.get("prateritum", ""))
    headline = f"{_weak_status(ok)} >>>     {_md_escape(infinitive)}"
    if pr:
        return f"{headline}\n\n_{_md_escape(pr)}_"
    return headline


def _format_feedback_partizip2_weak(entry, ok):
    """Handle format feedback partizip2 weak."""
    infinitive = _low_word(entry.get("infinitive", ""))
    p2 = _expected_partizip2(entry)
    headline = f"{_weak_status(ok)} >>>     {_md_escape(infinitive)}"
    if p2:
        return f"{headline}\n\n_{_md_escape(p2)}_"
    return headline


async def quiz_article_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_article")
    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_weak(update, context, "article")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_article_weak_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_ARTICLE_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "article")
    ok = _normalize_answer(text) == _normalize_answer(entry.get("article", ""))
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"article": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "article", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "article")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_article",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    _remember_last_weak_answer(context, entry, "article", ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _mark_session_word(context, "weak_article", entry.get("id"))
    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_article_weak_prompt(update, context, feedback)


async def quiz_plural_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz plural weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_plural")
    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_weak(update, context, "plural")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_plural_weak_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PLURAL_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_PLURAL_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_plural_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "plural")
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_plural_value(text) == _normalize_plural_value(entry.get("plural", ""))
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"plural": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "plural", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "plural")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_plural",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    _remember_last_weak_answer(context, entry, "plural", ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _mark_session_word(context, "weak_plural", entry.get("id"))
    feedback = _format_feedback_plural_weak(entry, ok)
    return await _send_plural_weak_prompt(update, context, feedback)


async def quiz_prateritum_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz prateritum weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_prateritum")
    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_weak(update, context, "prateritum")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_prateritum_weak_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PRATERITUM_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_PRATERITUM_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_prateritum_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum")
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_answer(text) == _normalize_answer(entry.get("prateritum", ""))
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"prateritum": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_prateritum",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    _remember_last_weak_answer(context, entry, "prateritum", ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _mark_session_word(context, "weak_prateritum", entry.get("id"))
    feedback = _format_feedback_prateritum_weak(entry, ok)
    return await _send_prateritum_weak_prompt(update, context, feedback)


async def quiz_partizip2_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz partizip2 weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, "weak_partizip2")
    if text == INPUT_ERR_TEXT:
        applied = _apply_input_err_weak(update, context, "partizip2")
        prefix = "✅ Fixed previous answer." if applied else "Nothing to fix."
        return await _send_partizip2_weak_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PARTIZIP2_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_PARTIZIP2_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_partizip2_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2")
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_answer(text) == _expected_partizip2(entry)
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"partizip2": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_partizip2",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    _remember_last_weak_answer(context, entry, "partizip2", ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _mark_session_word(context, "weak_partizip2", entry.get("id"))
    feedback = _format_feedback_partizip2_weak(entry, ok)
    return await _send_partizip2_weak_prompt(update, context, feedback)


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


def _split_answer_parts(text):
    """Handle split answer parts."""
    raw = str(text or "").strip()
    if not raw:
        return []
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [p.strip() for p in raw.split() if p.strip()]


def _normalize_plural_value(value):
    """Handle normalize plural value."""
    token = _normalize_answer(value)
    if token.startswith("die "):
        token = token[4:].strip()
    return token


def _expected_partizip2(entry):
    """Handle expected partizip2."""
    part = _normalize_answer(entry.get("partizip2", ""))
    aux = _normalize_answer(
        entry.get("perfect_aux")
        or entry.get("hilfsverb")
        or entry.get("aux")
        or ""
    )
    if aux in {"hat", "ist"}:
        if part.startswith("hat ") or part.startswith("ist "):
            return part
        return f"{aux} {part}".strip()
    return part


def _synonym_entries(entry):
    """Handle synonym entries."""
    wid = entry.get("id")
    if not wid:
        return [entry]
    group = get_synonym_group(get_db_path(), wid)
    if not group:
        return [entry]
    return group


def _check_ru_de_strict(entry, answer_text):
    """Handle check ru de strict."""
    entry_type = (entry.get("type") or "").strip().lower()
    parts = _split_answer_parts(answer_text)
    synonyms = _synonym_entries(entry)

    if entry_type == "nomen":
        if len(parts) < 2:
            return False, {"article": False, "plural": False}
        article_in = _normalize_answer(parts[0])
        german_in = _normalize_answer(parts[1])
        plural_in = _normalize_plural_value(" ".join(parts[2:])) if len(parts) >= 3 else german_in
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "nomen":
                continue
            article_ok = article_in == _normalize_answer(cand.get("article", ""))
            german_ok = german_in == _normalize_answer(cand.get("german", ""))
            plural_ok = plural_in == _normalize_plural_value(cand.get("plural", ""))
            if article_ok and german_ok and plural_ok:
                return True, {"article": True, "plural": True}
        return False, {
            "article": article_in == _normalize_answer(entry.get("article", "")),
            "plural": plural_in == _normalize_plural_value(entry.get("plural", "")),
        }

    if entry_type == "verb":
        if len(parts) < 2:
            return False, {"partizip2": False, "prateritum": False}
        infinitive_in = _normalize_answer(parts[0])
        prateritum_in = ""
        prateritum_provided = False
        if len(parts) == 2:
            p2_in = _normalize_answer(parts[1])
        else:
            prateritum_in = _normalize_answer(parts[1])
            prateritum_provided = True
            p2_in = _normalize_answer(" ".join(parts[2:]))
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "verb":
                continue
            inf_ok = infinitive_in == _normalize_answer(cand.get("infinitive", ""))
            p2_ok = p2_in == _expected_partizip2(cand)
            pr_ok = prateritum_provided and prateritum_in == _normalize_answer(cand.get("prateritum", ""))
            if inf_ok and p2_ok:
                return True, {"partizip2": True, "prateritum": pr_ok}
        return False, {
            "partizip2": p2_in == _expected_partizip2(entry),
            "prateritum": prateritum_provided and prateritum_in == _normalize_answer(entry.get("prateritum", "")),
        }

    answer = _normalize_answer(answer_text)
    expected_values = []
    for cand in synonyms:
        if (cand.get("type") or "").strip().lower() == "verb":
            expected_values.append(_normalize_answer(cand.get("infinitive", "")))
        else:
            expected_values.append(_normalize_answer(cand.get("german", "")))
    return answer in expected_values, {}


def _entry_translation(entry, dict_lang):
    """Handle entry translation."""
    ru = str(entry.get("russian", "") or "").strip()
    ukr = str(entry.get("ukrainian", "") or "").strip()
    eng = str(entry.get("english", "") or "").strip()
    if dict_lang == "uk":
        return ukr or ru
    if dict_lang == "en":
        return eng or ru
    return ru


def _cap_word(word):
    """Handle cap word."""
    word = str(word or "").strip()
    if not word:
        return ""
    return word[:1].upper() + word[1:].lower()


def _low_word(word):
    """Handle low word."""
    return str(word or "").strip().lower()


def _format_ru_question(entry, dict_lang):
    """Handle format ru question."""
    value = _normalize_russian_singular(_entry_translation(entry, dict_lang))
    word_type = _low_word(entry.get("type", ""))
    type_hint = f" ({word_type})" if word_type else ""
    return f">>    {_md_escape(value)}{_md_escape(type_hint)} ?"


def _format_ru_question_no_q(entry, dict_lang):
    """Handle format ru question no q."""
    value = _normalize_russian_singular(_entry_translation(entry, dict_lang))
    word_type = _low_word(entry.get("type", ""))
    type_hint = f" ({word_type})" if word_type else ""
    return f">>    {_md_escape(value)}{_md_escape(type_hint)}"


def _format_de_ru_question(entry):
    """Handle format de ru question."""
    word_type = _low_word(entry.get("type", ""))
    lines = []
    if word_type == "nomen":
        article = str(entry.get("article", "")).strip()
        german = _cap_word(entry.get("german", ""))
        answer = f"{article} {german}".strip() if article else german
        lines.append(_md_escape(answer))
        plural = _cap_word(entry.get("plural", ""))
        if plural:
            lines.append(f"_  pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        infinitive = _low_word(entry.get("infinitive", ""))
        lines.append(_md_escape(infinitive))
        pr = _low_word(entry.get("prateritum", ""))
        if pr:
            lines.append(f"_  {_md_escape(pr)}_")
        p2 = _expected_partizip2(entry)
        if p2:
            lines.append(f"_  {_md_escape(p2)}_")
    elif word_type == "adjektiv":
        german = _low_word(entry.get("german", ""))
        lines.append(_md_escape(german))
        komp = _low_word(entry.get("komparativ", ""))
        if komp:
            lines.append(f"_  {_md_escape(komp)}_")
        supr = _low_word(entry.get("superlativ", ""))
        if supr:
            lines.append(f"_  {_md_escape(supr)}_")
    else:
        german = _low_word(entry.get("german", ""))
        lines.append(_md_escape(german))
    lines.append(">>    ❓")
    return "\n".join(lines)


def _format_article_question(entry):
    """Handle format article question."""
    return f">>     {_md_escape(_cap_word(entry.get('german', '')))} ?"


def _format_feedback_ru_de(entry, ok):
    """Handle format feedback ru de."""
    status = "Correct ✅" if ok else "Wrong ❌"
    word_type = entry.get("type", "")
    article = str(entry.get("article", "")).strip()
    if word_type == "nomen":
        base = _cap_word(entry.get("german", ""))
    elif word_type == "verb":
        base = _low_word(entry.get("infinitive", ""))
    else:
        base = _low_word(entry.get("german", ""))

    answer = f"{article} {base}".strip() if article else base
    headline = f"{status} >>>     *{_md_escape(answer)}*"
    lines = [headline]

    if word_type == "nomen":
        plural = _cap_word(entry.get("plural", ""))
        if plural:
            lines.append("")
            lines.append(f"_pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        pr = _low_word(entry.get("prateritum", ""))
        p2 = str(entry.get("partizip2", "") or "").strip().lower()
        aux = str(
            entry.get("perfect_aux")
            or entry.get("hilfsverb")
            or entry.get("aux")
            or ""
        ).strip().lower()
        if p2 and aux in {"hat", "ist"} and not p2.startswith("hat ") and not p2.startswith("ist "):
            p2 = f"{aux} {p2}"
        forms = ", ".join([x for x in [pr, p2] if x])
        if forms:
            lines.append("")
            lines.append(f"_{_md_escape(forms)}_")
    elif word_type == "adjektiv":
        komp = _low_word(entry.get("komparativ", ""))
        supr = _low_word(entry.get("superlativ", ""))
        forms = ", ".join([x for x in [komp, supr] if x])
        if forms:
            lines.append("")
            lines.append(f"_  {_md_escape(forms)}_")

    return "\n".join(lines)


def _format_learning_preview_prompt(entry, dict_lang):
    """Handle format learning preview prompt."""
    word_type = _low_word(entry.get("type", ""))
    lines = []
    if word_type == "nomen":
        article = str(entry.get("article", "")).strip()
        german = _cap_word(entry.get("german", ""))
        answer = f"{article} {german}".strip() if article else german
        lines.append(f"*{_md_escape(answer)}*")
        plural = _cap_word(entry.get("plural", ""))
        if plural:
            lines.append(f"_pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        infinitive = _low_word(entry.get("infinitive", ""))
        lines.append(f"*{_md_escape(infinitive)}*")
        pr = _low_word(entry.get("prateritum", ""))
        p2 = _expected_partizip2(entry)
        forms = ", ".join(x for x in [pr, p2] if x)
        if forms:
            lines.append(f"_{_md_escape(forms)}_")
    elif word_type == "adjektiv":
        german = _low_word(entry.get("german", ""))
        lines.append(f"*{_md_escape(german)}*")
        komp = _low_word(entry.get("komparativ", ""))
        supr = _low_word(entry.get("superlativ", ""))
        forms = ", ".join(x for x in [komp, supr] if x)
        if forms:
            lines.append(f"_  {_md_escape(forms)}_")
    else:
        german = _low_word(entry.get("german", ""))
        lines.append(f"*{_md_escape(german)}*")
    lines.append(_format_ru_question_no_q(entry, dict_lang))
    return "\n".join(lines)


def _format_feedback_de_ru(entry, ok, dict_lang):
    """Handle format feedback de ru."""
    status = "Correct ✅" if ok else "Wrong ❌"
    return f"{status} >>>     *{_md_escape(_low_word(_entry_translation(entry, dict_lang)))}*"


def _format_feedback_article(entry, ok, dict_lang):
    """Handle format feedback article."""
    status = "Correct ✅" if ok else "Wrong ❌"
    article = str(entry.get("article", "")).strip()
    german = _cap_word(entry.get("german", ""))
    plural = _cap_word(entry.get("plural", ""))
    headline = f"{status} >>>     {_md_escape((article + ' ' + german).strip())}"
    lines = [headline.strip()]
    if plural:
        lines.append("")
        lines.append(f"_pl. die {_md_escape(plural)}_")
    return "\n".join(lines)


def _pick_due_entry(context, user_id, mode, word_type=None):
    """Handle pick due entry."""
    due = select_due(
        get_db_path(),
        user_id=user_id,
        mode=mode,
        word_type=word_type,
        topics=_topic_filter_for_query(),
    )
    if not due:
        return None

    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    last_id = last_ids.get(mode)
    pool = [w for w in due if w.get("id") != last_id] if len(due) > 1 else due
    if not pool:
        pool = due

    # SM2 modes use ordered queue: learning->SM2 first, then normal due order.
    if mode in {_mode_l1_de(user_id), _mode_de_l1(user_id)}:
        entry = pool[0]
    else:
        entry = random.choice(pool)
    last_ids[mode] = entry.get("id")
    return entry


def _retry_queue(context, mode):
    """Handle retry queue."""
    queues = context.user_data.setdefault("retry_queue", {})
    return queues.setdefault(mode, [])


def _retry_counts(context, mode):
    """Handle retry counts."""
    counts = context.user_data.setdefault("retry_counts", {})
    return counts.setdefault(mode, {})


def _retry_since_last(context, mode):
    """Handle retry since last."""
    counters = context.user_data.setdefault("retry_since_last", {})
    return counters.get(mode, 0)


def _set_retry_since_last(context, mode, value):
    """Handle set retry since last."""
    counters = context.user_data.setdefault("retry_since_last", {})
    counters[mode] = int(value)


def _maybe_take_retry_entry(context, mode):
    """Handle maybe take retry entry."""
    queue = _retry_queue(context, mode)
    if not queue:
        return None
    counts = _retry_counts(context, mode)
    gap = _session_retry_gap(get_db_path())
    since_last = _retry_since_last(context, mode)
    entry, new_since, took = maybe_take_retry_entry(
        queue,
        counts,
        since_last,
        gap,
        lambda word_id: get_word_by_id(get_db_path(), word_id),
    )
    if not entry:
        return None
    context.user_data["quiz_is_retry"] = True
    if took:
        _set_retry_since_last(context, mode, new_since)
    return entry


def _register_retry_result(context, mode, word_id, ok, was_retry):
    """Handle register retry result."""
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    max_repeats = _session_retry_iterations(get_db_path())
    register_retry_result(queue, counts, word_id, ok, was_retry, max_repeats)
    if not was_retry and not ok:
        _set_retry_since_last(context, mode, 0)


def _reset_retry_state(context):
    """Handle reset retry state."""
    context.user_data.pop("retry_queue", None)
    context.user_data.pop("retry_counts", None)
    context.user_data.pop("retry_since_last", None)
    context.user_data.pop("quiz_is_retry", None)


def _weak_drill_queue(context, mode):
    """Handle weak drill queue."""
    queues = context.user_data.setdefault("weak_drill_queue", {})
    return queues.setdefault(mode, [])


def _weak_drill_counts(context, mode):
    """Handle weak drill counts."""
    counts = context.user_data.setdefault("weak_drill_counts", {})
    return counts.setdefault(mode, {})


def _weak_drill_since_last(context, mode):
    """Handle weak drill since last."""
    counters = context.user_data.setdefault("weak_drill_since_last", {})
    return counters.get(mode, 0)


def _set_weak_drill_since_last(context, mode, value):
    """Handle set weak drill since last."""
    counters = context.user_data.setdefault("weak_drill_since_last", {})
    counters[mode] = int(value)


def _reset_weak_drill_state(context, mode=None):
    """Handle reset weak drill state."""
    if mode:
        for key in ("weak_drill_queue", "weak_drill_counts", "weak_drill_since_last"):
            bucket = context.user_data.get(key)
            if isinstance(bucket, dict):
                bucket.pop(mode, None)
        return
    context.user_data.pop("weak_drill_queue", None)
    context.user_data.pop("weak_drill_counts", None)
    context.user_data.pop("weak_drill_since_last", None)
    context.user_data.pop("quiz_is_weak_drill", None)


def _maybe_take_weak_drill_entry(context, mode, force=False):
    """Handle maybe take weak drill entry."""
    queue = _weak_drill_queue(context, mode)
    if not queue:
        return None
    gap = _weak_drill_gap(get_db_path())
    if not force and _weak_drill_since_last(context, mode) < gap:
        return None
    counts = _weak_drill_counts(context, mode)
    while queue:
        word_id = queue.pop(0)
        entry = get_word_by_id(get_db_path(), word_id)
        if not entry:
            counts.pop(word_id, None)
            continue
        _set_weak_drill_since_last(context, mode, 0)
        context.user_data["quiz_is_weak_drill"] = True
        return entry
    return None


def _track_weak_drill_show(context, mode, word_id):
    """Handle track weak drill show."""
    if not word_id:
        return
    queue = _weak_drill_queue(context, mode)
    counts = _weak_drill_counts(context, mode)
    repeats = _weak_drill_repeats(get_db_path())
    shown = int(counts.get(word_id, 0)) + 1
    if shown >= repeats:
        counts.pop(word_id, None)
        while word_id in queue:
            queue.remove(word_id)
        return
    counts[word_id] = shown
    if word_id not in queue:
        queue.append(word_id)


def _pick_weak_entry(context, user_id, weak_field, word_type=None):
    """Handle pick weak entry."""
    due = list_weak_words(
        get_db_path(),
        user_id=user_id,
        weak_field=weak_field,
        word_type=word_type,
        topics=_topic_filter_for_query(),
        limit=300,
    )
    if not due:
        return None
    active = [w for w in due if w.get("is_active")]
    pool_src = active if active else due
    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    mode = f"weak_{weak_field}"
    last_id = last_ids.get(mode)
    pool = [w for w in pool_src if w.get("id") != last_id] if len(pool_src) > 1 else pool_src
    if not pool:
        pool = pool_src
    entry = random.choice(pool)
    last_ids[mode] = entry.get("id")
    return entry


def _pick_weak_or_drill_entry(context, user_id, weak_field, word_type=None):
    """Handle pick weak or drill entry."""
    mode = f"weak_{weak_field}"
    entry = _pick_weak_entry(context, user_id, weak_field, word_type=word_type)
    is_drill = False

    drill = _maybe_take_weak_drill_entry(context, mode, force=False)
    if drill:
        entry = drill
        is_drill = True
    elif not entry:
        drill = _maybe_take_weak_drill_entry(context, mode, force=True)
        if drill:
            entry = drill
            is_drill = True

    if not entry:
        return None

    _track_weak_drill_show(context, mode, entry.get("id"))
    if not is_drill:
        _set_weak_drill_since_last(context, mode, _weak_drill_since_last(context, mode) + 1)
        context.user_data["quiz_is_weak_drill"] = False
    return entry


async def _send_ru_de_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send ru de prompt."""
    mode = _mode_l1_de(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=_quiz_idle_markup(update.effective_user.id),
        )
        return QUIZ_RU_DE

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_RU_DE


async def _send_de_ru_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send de ru prompt."""
    mode = _mode_de_l1(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=_quiz_idle_markup(update.effective_user.id),
        )
        return QUIZ_DE_RU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_de_ru_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_DE_RU


async def _send_article_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send article prompt."""
    mode = MODE_ARTICLE
    entry = _maybe_take_retry_entry(context, mode)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode, word_type="nomen")
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No due nouns. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return TESTS_MENU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_article_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=True),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE


async def _send_article_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send article weak prompt."""
    entry = _pick_weak_or_drill_entry(context, update.effective_user.id, "article", word_type="nomen")
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No weak article words.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    context.user_data["quiz_entry"] = entry
    text = f">>    {_normalize_russian_singular(_entry_translation(entry, _user_dict_lang(update.effective_user.id)))} -> ____ {_cap_word(entry.get('german', ''))}?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await update.message.reply_text(
        prompt,
        reply_markup=_weak_article_markup(update.effective_user.id),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE_WEAK


async def _send_plural_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send plural weak prompt."""
    entry = _pick_weak_or_drill_entry(context, update.effective_user.id, "plural", word_type="nomen")
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No weak plural words.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    context.user_data["quiz_entry"] = entry
    article = (entry.get("article", "") or "").strip()
    text = f">>    {article} {_cap_word(entry.get('german', ''))} -> plural?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await update.message.reply_text(
        prompt,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PLURAL_WEAK


async def _send_prateritum_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send prateritum weak prompt."""
    entry = _pick_weak_or_drill_entry(context, update.effective_user.id, "prateritum", word_type="verb")
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No weak prateritum words.",
            reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    context.user_data["quiz_entry"] = entry
    text = f">>    {_low_word(entry.get('infinitive', ''))} -> Prateritum?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await update.message.reply_text(
        prompt,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PRATERITUM_WEAK


async def _send_partizip2_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send partizip2 weak prompt."""
    entry = _pick_weak_or_drill_entry(context, update.effective_user.id, "partizip2", word_type="verb")
    if not entry:
        if prefix_text:
            await update.message.reply_text(prefix_text, parse_mode="Markdown")
        await update.message.reply_text(
            "No weak partizip2 words.",
            reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    context.user_data["quiz_entry"] = entry
    text = f">>    {_low_word(entry.get('infinitive', ''))} -> Partizip2?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    prompt = text
    await update.message.reply_text(
        prompt,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PARTIZIP2_WEAK


def _ai_prompt(word, clarification):
    """Handle ai prompt."""
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "For nomen return: type, article, german, plural, russian. "
        "For verb return: type, infinitive, prateritum, partizip2, russian. "
        "For adjektiv return: type, german, komparativ, superlativ, russian. "
        "For all other allowed types return: type, german, russian. "
        "Use Russian translations in singular only. No extra text."
        f"\nWord: {word}{extra}"
    )


def _ai_precheck_prompt(word):
    """Handle ai precheck prompt."""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
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
    return {"type": entry_type, "word": value}


def _format_precheck_list(items):
    """Handle format precheck list."""
    lines = [f"Precheck list (topic: {_current_topic_value()}):"]
    for idx, item in enumerate(items, start=1):
        word = item.get("word", "")
        item_type = item.get("type", "?")
        original = item.get("original", "")
        if original and _normalize_lookup_text(original) != _normalize_lookup_text(word):
            lines.append(f"{idx}. {word} [{item_type}] (from {original})")
        else:
            lines.append(f"{idx}. {word} [{item_type}]")
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
    for raw in words:
        prompt_text = _ai_precheck_prompt(raw)
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
        items.append({"original": raw, "word": parsed["word"], "type": parsed["type"]})
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
    entry_type = entry.get("type")
    translation = _entry_translation(entry, dict_lang)
    warn = "❗ has a synonym in DB" if has_synonym else ""
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        extras = f" ({plural})" if plural else ""
        lines = [
            "Recognized Nomen",
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
            "Recognized Verb",
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
            "Recognized Adjektiv",
            "",
            f"*{_md_escape(ger)}*{_md_escape(extras)} - {_md_escape(translation)}",
        ]
        if warn:
            lines += ["", warn]
        return "\n".join(lines)

    ger = entry.get("german", "")
    lines = [
        f"Recognized {entry_type}",
        "",
        f"*{_md_escape(ger)}* - {_md_escape(translation)}",
    ]
    if warn:
        lines += ["", warn]
    return "\n".join(lines)


async def _process_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word, clarification):
    """Handle process ai word."""
    has_queue = bool(context.user_data.get("add_queue"))

    reject_reason = _prefilter_reject_reason(word)
    if reject_reason:
        if has_queue:
            logging.info("Queue skip filtered word: %s", word)
            return ADD_INPUT
        await _reply_text_with_retry(
            update.message,
            f"{reject_reason} Skipped: {word}",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    existing = find_existing_word(get_db_path(), word)
    if existing:
        existing_type = existing.get("type", "word")
        existing_value = existing.get("infinitive", "") if existing_type == "verb" else existing.get("german", "")
        if has_queue:
            logging.info("Queue skip existing word: %s", word)
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

    context.user_data["pending_word"] = word
    prompt_text = _ai_prompt(word, clarification)
    context.user_data["last_ai_prompt"] = prompt_text
    try:
        response_text = generate_fields(prompt_text)
    except Exception as exc:
        logging.exception("AI request failed for word: %s", word)
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
            word,
            clarification,
            (response_text or "")[:500],
        )
        await _reply_text_with_retry(
            update.message,
            (
                f"Error: AI failed to parse for word: {word}\n"
                f"Clarification: {clarification or '-'}\n"
                "Use Confirm, Clarify AI, Clarify field, Reject, Ban, or Stop."
            ),
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _normalize_ai_entry(entry)
    if _is_non_infinitive_verb_input(word, entry):
        inf = entry.get("infinitive", "")
        existing_inf = find_existing_word(get_db_path(), inf) if inf else None
        if existing_inf:
            msg = f"❌ Verb form detected: {word}. Base infinitive: {inf}. Already in database, not saved."
            if has_queue:
                await _reply_text_with_retry(update.message, msg)
                logging.info("Queue skip non-infinitive verb input (existing infinitive): %s -> %s", word, inf)
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


async def _back_to_main_menu(update: Update):
    """Handle back to main menu."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "main_menu"),
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


async def _back_to_tests(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    """Handle back to tests."""
    if context is not None:
        _reset_retry_state(context)
        _reset_weak_drill_state(context)
        _clear_last_answer(context)
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "tests") + ":",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU




def _interval_divisor_for_mode(user_id, mode):
    """Handle interval divisor for mode."""
    if mode == _mode_de_l1(user_id):
        return 2.0
    return 1.0


def _update_srs_for_entry(user_id, entry, mode, ok):
    """Handle update srs for entry."""
    quality = 5 if ok else 2
    update_progress(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        mode=mode,
        quality=quality,
        interval_divisor=_interval_divisor_for_mode(user_id, mode),
    )
    log_quiz_event(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, is_correct=ok)


def _create_dump_file():
    """Handle create dump file."""
    db_path = get_db_path()
    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump_path = dump_dir / f"words-{stamp}.db"
    shutil.copy2(db_path, dump_path)
    return dump_path


def _create_logs_dump_file():
    """Handle create logs dump file."""
    log_path = get_log_path()
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None

    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = dump_dir / f"bot-logs-{stamp}.txt"
    shutil.copy2(log_path, out_path)
    return out_path


def _create_suggestions_dump_file():
    """Handle create suggestions dump file."""
    src = get_suggestions_path()
    if not src.exists() or src.stat().st_size == 0:
        return None

    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = dump_dir / f"suggested-words-{stamp}.txt"
    shutil.copy2(src, out_path)
    return out_path


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


async def _auto_dump_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle auto dump job."""
    try:
        dump_path = _create_dump_file()
    except Exception:
        return

    owner_id = get_owner_id()
    if owner_id:
        try:
            await context.bot.send_document(
                chat_id=owner_id,
                document=dump_path.open("rb"),
                filename=dump_path.name,
                caption=f"✅ Auto dump created: {dump_path.name}",
            )
            report_day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            retention = get_daily_first_touch_retention(
                get_db_path(),
                user_id=owner_id,
                target_date=report_day,
                modes=(MODE_RU_DE, MODE_DE_RU),
            )
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"```text\n{_format_retention_daily_text(retention)}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            pass


def _format_retention_daily_text(retention):
    """Handle format daily first-touch retention text."""
    day = retention.get("date", "")
    total = int(retention.get("total") or 0)
    correct = int(retention.get("correct") or 0)
    rate = float(retention.get("rate") or 0.0)
    if total <= 0:
        return f"Retention rate ({day}, first touch): no data."
    if rate < 70.0:
        zone = "<70%: overload"
    elif 80.0 <= rate <= 90.0:
        zone = "80-90%: optimal"
    elif rate > 95.0:
        zone = ">95%: too easy (you can add more new words)"
    else:
        zone = "outside optimal range"
    return (
        f"Retention rate ({day}, first touch): {rate:.1f}%\n"
        f"Correct: {correct}/{total}\n"
        f"{zone}"
    )


def _configure_auto_dump(app: Application, mode: str):
    """Handle configure auto dump."""
    job_queue = app.job_queue
    if job_queue is None:
        return

    for job in job_queue.get_jobs_by_name(AUTO_DUMP_JOB_NAME):
        job.schedule_removal()

    mode = (mode or "off").lower()
    if mode == "daily":
        job_queue.run_daily(
            _auto_dump_job,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            name=AUTO_DUMP_JOB_NAME,
        )
    elif mode == "weekly":
        job_queue.run_daily(
            _auto_dump_job,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            days=(0,),
            name=AUTO_DUMP_JOB_NAME,
        )


def _parse_utc_iso(token):
    """Handle parse utc iso."""
    raw = str(token or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _learning_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle learning reminder job."""
    db_path = get_db_path()
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    reminder_interval = _learning_reminder_interval(db_path)
    reminder_cooldown = _learning_reminder_cooldown(db_path)
    quorum_pct = _learning_ready_quorum(db_path)

    for batch in learning_active_batches(db_path):
        user_id = int(batch.get("user_id") or 0)
        batch_id = str(batch.get("id") or "")
        if not user_id or not batch_id:
            continue
        stats = learning_status_snapshot(db_path, user_id=user_id, quorum_pct=quorum_pct)
        if stats.get("batch_id") != batch_id:
            continue
        active_count = int(stats.get("active_count") or 0)
        if active_count <= 0:
            continue
        ready = bool(stats.get("ready"))
        learning_batch_update_ready_state(db_path, batch_id=batch_id, ready=ready, now_iso=now_iso)
        if not ready:
            continue
        ready_since = _parse_utc_iso(stats.get("ready_since")) or now
        last_notified = _parse_utc_iso(stats.get("last_notified_at"))
        ready_elapsed = (now - ready_since).total_seconds()
        notify_elapsed = (now - last_notified).total_seconds() if last_notified else 10**9
        if ready_elapsed < reminder_interval:
            continue
        if notify_elapsed < reminder_cooldown:
            continue
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Learning reminder:\n"
                    f"due_now = {stats.get('due_count', 0)} / need {stats.get('ready_needed', 0)}\n"
                    f"Use Learning -> learning session."
                ),
            )
            learning_batch_mark_notified(db_path, batch_id=batch_id, now_iso=now_iso)
        except Exception:
            continue


def _configure_learning_reminders(app: Application):
    """Handle configure learning reminders."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(LEARNING_REMINDER_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        _learning_reminder_job,
        interval=60,
        first=60,
        name=LEARNING_REMINDER_JOB_NAME,
    )


def _format_sm2_report_text(total, peak_hour, peak_count):
    """Handle format sm2 report text."""
    if total <= 0:
        return "SM2 today: 0 due."
    if peak_hour is None:
        return f"SM2 today: {total} due."
    return (
        f"SM2 today: {total} due.\n"
        f"Peak hour ~{peak_hour:02d}:00 UTC (approx), count {peak_count}."
    )


def _should_send_sm2_daily_report(db_path, now_utc):
    """Handle should send sm2 daily report."""
    today = now_utc.date().isoformat()
    last_date = get_setting(db_path, SM2_REPORT_DATE_KEY, "")
    if last_date == today:
        return False
    report_hour = _sm2_report_hour_utc(db_path)
    return now_utc.hour >= report_hour


async def _sm2_daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle sm2 daily report job."""
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return
    now = datetime.now(timezone.utc)
    if not _should_send_sm2_daily_report(db_path, now):
        return
    today = now.date().isoformat()
    mode = _mode_l1_de(owner_id)
    forecast = sm2_due_forecast(db_path, owner_id, mode)
    total = int(forecast.get("total", 0))
    peak_hour = forecast.get("peak_hour")
    peak_count = int(forecast.get("peak_count", 0))
    text = _format_sm2_report_text(total, peak_hour, peak_count)
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"```text\n{text}\n```",
            parse_mode="Markdown",
        )
        set_setting(db_path, SM2_REPORT_DATE_KEY, today)
        set_setting(db_path, "sm2_peak_hour_today", "")
        set_setting(db_path, SM2_PEAK_NOTIFY_DATE_KEY, "")
        if peak_hour is not None:
            set_setting(db_path, "sm2_peak_hour_today", str(int(peak_hour)))
    except Exception:
        logging.exception("SM2 daily report failed")
        return


async def _sm2_peak_notify_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle sm2 peak notify job."""
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return
    today = date.today().isoformat()
    peak_notified = get_setting(db_path, SM2_PEAK_NOTIFY_DATE_KEY, "")
    if peak_notified == today:
        return
    report_date = get_setting(db_path, SM2_REPORT_DATE_KEY, "")
    if report_date != today:
        return
    peak_hour_raw = get_setting(db_path, "sm2_peak_hour_today", "")
    try:
        peak_hour = int(peak_hour_raw)
    except (TypeError, ValueError):
        return
    now = datetime.now(timezone.utc)
    if now.hour < peak_hour:
        return
    mode = _mode_l1_de(owner_id)
    forecast = sm2_due_forecast(db_path, owner_id, mode)
    total = int(forecast.get("total", 0))
    if total <= 0:
        return
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"SM2 peak window now. Due today: {total}.",
        )
        set_setting(db_path, SM2_PEAK_NOTIFY_DATE_KEY, today)
    except Exception:
        logging.exception("SM2 peak notify failed")
        return


def _configure_sm2_reports(app: Application):
    """Handle configure sm2 reports."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(SM2_REPORT_JOB_NAME):
        job.schedule_removal()
    for job in job_queue.get_jobs_by_name(SM2_PEAK_JOB_NAME):
        job.schedule_removal()
    # Run frequently and guard by date/hour so reports still work after restarts.
    job_queue.run_repeating(
        _sm2_daily_report_job,
        interval=600,
        first=30,
        name=SM2_REPORT_JOB_NAME,
    )
    job_queue.run_repeating(
        _sm2_peak_notify_job,
        interval=900,
        first=900,
        name=SM2_PEAK_JOB_NAME,
    )


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
    if not get_setting(db_path, SM2_REPORT_HOUR_KEY):
        set_setting(db_path, SM2_REPORT_HOUR_KEY, str(SM2_REPORT_UTC_HOUR_DEFAULT))


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
    _configure_sm2_reports(app)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{START_TEXT}$") & ~filters.COMMAND, start),
        ],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            SELECT_UI_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_ui_lang)],
            SELECT_DICT_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_dict_lang)],
            TESTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, tests_menu_choice)],
            DB_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, db_search_input)],
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
            ADD_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_edit)],
            QUIZ_RU_DE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de)],
            QUIZ_DE_RU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_de_ru)],
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
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
