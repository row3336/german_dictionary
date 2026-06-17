"""Shared navigation/dispatch helpers used across handlers.

Leaf module: depends only on i18n/lang/keyboards/session/labels/app_config,
never on handler modules — so handler modules can import these without cycles.
"""

import asyncio

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from ..app_config import MENU, TESTS_MENU
from ..common.lang import _user_interface_lang
from ..common.session import (
    _reset_fsrs_cycle_state,
    _reset_retry_state,
    _reset_weak_drill_state,
)
from ..i18n import t
from ..labels import (
    LEARNING_ADD_TEXT,
    LEARNING_MIX_MODE_TEXT,
    LEARNING_NEXT_TEXT,
    LEARNING_RESET_TEXT,
    LEARNING_SKIP_TEXT,
    LEARNING_START_TEXT,
    LEARNING_TEXT,
    PARSE_VF_TEXT,
    QUIZ_DE_RU_TEXT,
    QUIZ_PL_SG_TEXT,
    QUIZ_RU_DE_TEXT,
    SEND_LEARN_TEXT,
)
from ..ui.keyboards import _main_menu_markup, _tests_menu_keys


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
        ("db_search_plural_match", "db_search_plural_match"),
        ("db_search_topic_words", "db_search_topic_words"),
        ("db_search_topics_lists", "db_search_topics_lists"),
        ("db_search_verbs_lists", "db_search_verbs_lists"),
        ("db_search_topics_lern", "db_search_topics_lern"),
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
    mapping[QUIZ_PL_SG_TEXT] = "quiz_pl_sg"
    mapping[LEARNING_TEXT] = "learning"
    mapping[LEARNING_START_TEXT] = "learning_start"
    mapping[LEARNING_ADD_TEXT] = "learning_add"
    mapping[LEARNING_SKIP_TEXT] = "learning_skip"
    mapping[LEARNING_NEXT_TEXT] = "learning_next"
    mapping[LEARNING_RESET_TEXT] = "learning_reset"
    mapping[LEARNING_MIX_MODE_TEXT] = "learning_mix_mode"
    mapping[SEND_LEARN_TEXT] = "send_learn"
    mapping["flags!"] = "review_flags"
    mapping[PARSE_VF_TEXT] = "review_flags"
    mapping["Back"] = "back"
    mapping["Stop"] = "stop"
    return mapping.get(text.strip(), "")


async def _reply_text_with_retry(message, text, max_retries=3, **kwargs):
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
        except (TimedOut, NetworkError):
            attempts += 1
            if attempts > max_retries:
                raise
            await asyncio.sleep(min(5, attempts * 2))


def _clear_last_answer(context):
    """Handle clear last answer."""
    context.user_data.pop("last_answered_entry", None)
    context.user_data.pop("last_answered_mode", None)
    context.user_data.pop("last_answered_ok", None)
    context.user_data.pop("last_answered_srs", None)


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
        _reset_fsrs_cycle_state(context)
        _reset_weak_drill_state(context)
        _clear_last_answer(context)
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "tests") + ":",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU
