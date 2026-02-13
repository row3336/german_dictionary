import os
import re
from pathlib import Path

MENU, TESTS_MENU, SETTINGS_MENU, AUTO_DUMP_MENU, LEVELS_MENU, ADD_INPUT, ADD_CONFIRM, ADD_CLARIFY, ADD_EDIT, FLAG_COMMENT, REVIEW_FLAGS, QUIZ_RU_DE, QUIZ_DE_RU, QUIZ_ARTICLE = range(14)

BACK_TEXT = "Back"
STOP_TEXT = "Stop"
START_TEXT = "Start"
AI_CONFIRM = "Confirm"
AI_CLARIFY_AI = "Clarify AI"
AI_CLARIFY_FIELD = "Clarify Field"
AI_REJECT = "Reject"

OWNER_MAIN_KEYS = [["Add Word", "Tests"], ["Settings", "Stat"]]
USER_MAIN_KEYS = [["Add Word", "Tests"], ["Info", "Stat"]]
WAIT_KEYS = [[START_TEXT]]
ADD_KEYS = [[BACK_TEXT]]
AI_KEYS = [[AI_CONFIRM, AI_CLARIFY_AI, AI_CLARIFY_FIELD], [AI_REJECT, STOP_TEXT]]
TESTS_KEYS = [["Quiz RU -> DE", "Quiz DE -> RU"], ["Quiz article", BACK_TEXT]]
SETTINGS_KEYS = [["Dump DB", "Dump Logs"], ["Suggested Words", "Review Flags"], ["Auto Dump", "Levels"], [BACK_TEXT]]
AUTO_DUMP_KEYS = [["Daily", "Weekly"], ["Off", BACK_TEXT]]
LEVELS_BACK_TEXT = "Back"
FLAG_TEXT = "\u26A0\uFE0F"
REVIEW_MARK_TEXT = "Mark Reviewed"
REVIEW_SKIP_TEXT = "Skip"
EDIT_FIELDS_KEYS = [["russian", "level"], [BACK_TEXT]]
EDIT_FIELDS_KEYS_FULL = [
    ["russian", "level"],
    ["article", "plural"],
    ["prateritum", "partizip2"],
    ["german", "infinitive"],
    ["type", BACK_TEXT],
]
EDIT_LEVEL_KEYS = [["A1", "A2", "B1"], ["B2", "C1", "C2"], [BACK_TEXT]]

APPROVE_PREFIX = "approve_user:"
REJECT_PREFIX = "reject_user:"
AUTO_DUMP_SETTING_KEY = "auto_dump_mode"
AUTO_DUMP_JOB_NAME = "auto_db_dump"

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
    "adjektiv",
    "pronomen",
    "adverb",
    "praeposition",
    "konjunktion",
    "partikel",
}


def get_db_path():
    return Path(os.getenv("DB_PATH", "./data/words.db"))


def get_dump_dir():
    return Path(os.getenv("DUMP_DIR", "./data/backups"))


def get_log_path():
    return Path(os.getenv("LOG_PATH", "./data/bot.log"))


def get_suggestions_path():
    return Path(os.getenv("SUGGESTIONS_PATH", "./data/suggested_words.txt"))


def get_owner_id():
    try:
        return int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        return 0


def is_owner(user_id):
    owner_id = get_owner_id()
    return owner_id != 0 and user_id == owner_id
