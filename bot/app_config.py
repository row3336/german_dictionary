import os
import re
from pathlib import Path

MENU, TESTS_MENU, SETTINGS_MENU, AUTO_DUMP_MENU, LEVELS_MENU, ADD_INPUT, ADD_CONFIRM, ADD_CLARIFY, ADD_EDIT, FLAG_COMMENT, REVIEW_FLAGS, QUIZ_RU_DE, QUIZ_DE_RU, QUIZ_ARTICLE, SELECT_UI_LANG, SELECT_DICT_LANG, SYN_MENU, SYN_ADD_INPUT, SYN_REMOVE_INPUT, SYN_AUTO_CONFIRM, SYN_CLEAR_CONFIRM, QUIZ_ARTICLE_WEAK, QUIZ_PLURAL_WEAK, QUIZ_VERB_WEAK_MENU, QUIZ_PRATERITUM_WEAK, QUIZ_PARTIZIP2_WEAK, QUIZ_RU_DE_MENU = range(27)

BACK_TEXT = "back"
STOP_TEXT = "stop"
START_TEXT = "Start"
AI_CONFIRM = "Confirm"
AI_CLARIFY_AI = "Clarify AI"
AI_CLARIFY_FIELD = "Clarify field"
AI_REJECT = "Reject"
AI_SKIP = "Skip"
AI_BAN_WORD = "Ban"

OWNER_MAIN_KEYS = [["Add words", "Quiz"], ["Settings", "Statistics"]]
USER_MAIN_KEYS = [["Add words", "Quiz"], ["Info", "Statistics"]]
WAIT_KEYS = [[START_TEXT]]
ADD_KEYS = [[BACK_TEXT]]
AI_KEYS = [[AI_CONFIRM, AI_CLARIFY_AI, AI_CLARIFY_FIELD], [AI_REJECT, AI_BAN_WORD, STOP_TEXT]]
TESTS_KEYS = [["Ru > De", "De > Ru"], ["Articles", BACK_TEXT]]
REVIEW_FLAGS_TEXT = "Review Flags \u26A0\uFE0F"
SETTINGS_KEYS = [
    ["Suggested Words", REVIEW_FLAGS_TEXT],
    ["Levels", "Auto Dump"],
    ["Dump Logs", "Dump DB"],
    [BACK_TEXT],
]
AUTO_DUMP_KEYS = [["Daily", "Weekly"], ["Off", BACK_TEXT]]
LEVELS_BACK_TEXT = "Back"
FLAG_TEXT = "\u26A0\uFE0F"
I_DONT_KNOW_TEXT = "i dont know ;("
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
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


def get_db_path():
    return Path(os.getenv("DB_PATH", "./data/words.db"))


def get_dump_dir():
    return Path(os.getenv("DUMP_DIR", "./data/backups"))


def get_log_path():
    return Path(os.getenv("LOG_PATH", "./data/bot.log"))


def get_suggestions_path():
    return Path(os.getenv("SUGGESTIONS_PATH", "./data/suggested_words.txt"))


def get_info_path():
    return Path(os.getenv("INFO_PATH", "./docs/info_ru.md"))


def get_owner_id():
    try:
        return int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        return 0


def is_owner(user_id):
    owner_id = get_owner_id()
    return owner_id != 0 and user_id == owner_id
