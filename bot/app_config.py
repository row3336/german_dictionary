import os
import re
from pathlib import Path

MENU, TESTS_MENU, SETTINGS_MENU, DUMP_MENU, AUTO_DUMP_MENU, LEVELS_MENU, ADD_INPUT, ADD_PRECHECK, ADD_PRECHECK_EDIT, ADD_CONFIRM, ADD_CLARIFY, ADD_EDIT, FLAG_COMMENT, REVIEW_FLAGS, QUIZ_RU_DE, QUIZ_DE_RU, QUIZ_PL_SG, QUIZ_ARTICLE, SELECT_UI_LANG, SELECT_DICT_LANG, SYN_MENU, SYN_ADD_INPUT, SYN_REMOVE_INPUT, SYN_AUTO_CONFIRM, SYN_CLEAR_CONFIRM, QUIZ_ARTICLE_WEAK, QUIZ_PLURAL_WEAK, QUIZ_VERB_WEAK_MENU, QUIZ_PRATERITUM_WEAK, QUIZ_PARTIZIP2_WEAK, QUIZ_RU_DE_MENU, ALGO_SETTINGS_MENU, ALGO_SETTINGS_EDIT, ALGO_SETTINGS_CONFIRM, TOPIC_FILTER_EDIT, DRILL_SETTINGS_MENU, LEARNING_MENU, LEARNING_QUIZ, LEARNING_SETTINGS_MENU, DB_SEARCH, DB_SEARCH_VIEW, ACCESS_REJECTED, ACCESS_MESSAGE, FEEDBACK_INPUT = range(44)

BACK_TEXT = "back"
STOP_TEXT = "stop"
START_TEXT = "Start"
AI_CONFIRM = "Confirm"
AI_CLARIFY_AI = "Clarify AI"
AI_CLARIFY_FIELD = "clarify_fields"
AI_REJECT = "Reject"
AI_SKIP = "Skip"
AI_BAN_WORD = "Ban"
PRECHECK_CONFIRM = "Approve"
PRECHECK_EDIT = "Edit list"
PRECHECK_REJECT = "Reject list"
AI_KEYS = [[AI_CONFIRM, AI_CLARIFY_AI, AI_CLARIFY_FIELD], [AI_REJECT, AI_BAN_WORD, STOP_TEXT]]
PRECHECK_KEYS = [[PRECHECK_CONFIRM, PRECHECK_EDIT, PRECHECK_REJECT], [STOP_TEXT]]
REVIEW_FLAGS_TEXT = "Review Flags \u26A0\uFE0F"
FLAG_TEXT = "\u26A0\uFE0F"
I_DONT_KNOW_TEXT = "idk ;("
INPUT_ERR_TEXT = "input_err"
AUTO_DUMP_SETTING_KEY = "auto_dump_mode"
AUTO_DUMP_JOB_NAME = "auto_db_dump"
SESSION_RETRY_GAP_KEY = "session_retry_gap"
SESSION_RETRY_IT_KEY = "session_retry_iterations"
TOPIC_FILTER_KEY = "topic_filter"
WEAK_DRILL_GAP_KEY = "weak_drill_gap"
WEAK_DRILL_REPEATS_KEY = "weak_drill_repeats"
LEARNING_DAILY_QTY_KEY = "learning_daily_qty"
LEARNING_INTERVALS_KEY = "learning_intervals_sec"
LEARNING_REMINDER_INTERVAL_KEY = "learning_reminder_interval_sec"
LEARNING_REMINDER_COOLDOWN_KEY = "learning_reminder_cooldown_sec"
LEARNING_READY_QUORUM_KEY = "learning_ready_quorum_pct"
FSRS_REPORT_HOUR_KEY = "fsrs_report_utc_hour"
FSRS_REPORT_DATE_KEY = "fsrs_report_date"
FSRS_PEAK_NOTIFY_DATE_KEY = "fsrs_peak_notify_date"
FSRS_WEEKLY_REPORT_WEEK_KEY = "fsrs_weekly_report_week"

OWNER_INTERFACE_LANG_KEY = "owner_interface_lang"
OWNER_DICT_LANG_KEY = "owner_dict_lang"

RETRY_GAP_QUESTIONS = 3
RETRY_MAX_REPEATS = 4
WEAK_DRILL_GAP_DEFAULT = 4
WEAK_DRILL_REPEATS_DEFAULT = 10
LEARNING_DAILY_QTY_DEFAULT = 20
LEARNING_INTERVALS_DEFAULT = "30,120,600,3600,10800,18000"
LEARNING_REMINDER_INTERVAL_DEFAULT = 60
LEARNING_REMINDER_COOLDOWN_DEFAULT = 900
LEARNING_READY_QUORUM_DEFAULT = 70
LEARNING_MIX_MODE_DEFAULT = "0"
LEARNING_REMINDER_JOB_NAME = "learning_reminder_job"
FSRS_AFTERNOON_REMINDER_JOB_NAME = "fsrs_afternoon_reminder_job"
# Owner reminders about pending access requests (P3.1).
ACCESS_REMINDER_JOB_NAME = "access_pending_reminder_job"
ACCESS_REMINDER_INTERVAL_SECONDS = 2 * 60 * 60  # every 2 hours
# Hashtags: an open request is searchable by REQUEST, a handled one by DONE
# (so it drops out of the open-requests search).
ACCESS_REQUEST_HASHTAG = "#user_confirmation_request"
ACCESS_DONE_HASHTAG = "#user_confirmation_done"
# callback_data prefix of approval inline buttons: "access:approve:<uid>" / "access:reject:<uid>".
ACCESS_CALLBACK_PREFIX = "access"
FSRS_REPORT_UTC_HOUR_DEFAULT = 3
FSRS_REPORT_JOB_NAME = "fsrs_daily_report_job"
FSRS_PEAK_JOB_NAME = "fsrs_peak_notify_job"
LEARNING_WARMUP_INTERVALS = (0, 0, 0)

ARTICLE_SET = {
    "der",
    "die",
    "das",
    "den",
    "dem",
    "des",
    "ein",
    "eine",
    "einen",
    "einem",
    "eines",
    "kein",
    "keine",
    "keinen",
    "keinem",
    "keines",
}

NUMERAL_SET = {
    "null",
    "eins",
    "zwei",
    "drei",
    "vier",
    "fuenf",
    "fuenfzehn",
    "fuenfzig",
    "funf",
    "funfzehn",
    "funfzig",
    "sechs",
    "sieben",
    "acht",
    "neun",
    "zehn",
    "elf",
    "zwoelf",
    "dreizehn",
    "vierzehn",
    "sechzehn",
    "siebzehn",
    "achtzehn",
    "neunzehn",
    "zwanzig",
    "dreissig",
    "vierzig",
    "sechzig",
    "siebzig",
    "achtzig",
    "neunzig",
    "hundert",
    "tausend",
    "million",
    "milliarde",
}

NUMERIC_RE = re.compile(r"^\s*[+-]?(?:(?:\d{1,3}(?:[.,]\d{3})+)|\d+)(?:[.,]\d+)?\s*%?\s*$")

ALLOWED_AI_TYPES = {
    "nomen",
    "verb",
    "phrase",
    "adjektiv",
    "pronomen",
    "adverb",
    "praeposition",
    "konjunktion",
    "partikel",
}
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


def get_db_path():
    """Handle get db path."""
    return Path(os.getenv("DB_PATH", "./data/words.db"))


def get_dump_dir():
    """Handle get dump dir."""
    return Path(os.getenv("DUMP_DIR", "./data/backups"))


def get_log_path():
    """Handle get log path."""
    return Path(os.getenv("LOG_PATH", "./data/bot.log"))


def get_suggestions_path():
    """Handle get suggestions path."""
    return Path(os.getenv("SUGGESTIONS_PATH", "./data/suggested_words.txt"))


def get_info_path():
    """Handle get info path."""
    return Path(os.getenv("INFO_PATH", "./docs/info_ru.md"))


def get_owner_id():
    """Handle get owner id."""
    try:
        return int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        return 0


def is_owner(user_id):
    """Handle is owner."""
    owner_id = get_owner_id()
    return owner_id != 0 and user_id == owner_id
