"""Shared navigation/dispatch helpers used across handlers.

Leaf module: depends only on i18n/lang/keyboards/session/labels/app_config,
never on handler modules — so handler modules can import these without cycles.
"""

import asyncio
import logging

import httpx
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from ..app_config import MENU, TESTS_MENU
from ..common.lang import _QUIZ_DIR_SOURCES, _user_interface_lang
from ..common.session import (
    _reset_fsrs_cycle_state,
    _reset_retry_state,
    _reset_weak_drill_state,
)
from ..i18n import t
from ..labels import (
    PARSE_VF_TEXT,
    QUIZ_PL_SG_TEXT,
)
from ..ui.keyboards import _main_menu_markup, _tests_menu_keys


def _action_key_from_text(text, user_id):
    """Map the pressed button text to an action (honoring the user's interface language)."""
    lang = _user_interface_lang(user_id)
    keys = [
        ("start", "start"),
        ("back", "back"),
        ("stop", "stop"),
        ("add_word", "add_word"),
        ("add_ai", "add_ai"),
        ("add_vf", "add_vf"),
        ("vf_recheck", "vf_recheck"),
        ("tests", "tests"),
        ("db_search", "db_search"),
        ("db_search_matches", "db_search_matches"),
        ("db_search_plural_match", "db_search_plural_match"),
        ("db_search_topic_words", "db_search_topic_words"),
        ("db_search_topics_lists", "db_search_topics_lists"),
        ("db_search_type_lists", "db_search_type_lists"),
        ("db_search_topics_lern", "db_search_topics_lern"),
        ("db_search_weak_words", "db_search_weak_words"),
        ("settings", "settings"),
        ("info", "info"),
        ("stat", "stat"),
        ("quiz_article", "quiz_article"),
        ("dump_db", "dump_db"),
        ("dump_logs", "dump_logs"),
        ("dump", "dump"),
        ("review_flags", "review_flags"),
        ("algo_settings", "algo_settings"),
        ("topic_filter", "topic_filter"),
        ("conjugation", "conjugation"),
        ("conjugation_dice", "conjugation_dice"),
        ("fsrs_sett", "fsrs_sett"),
        ("weak_sett", "weak_sett"),
        ("learning_sett", "learning_sett"),
        ("learning_sett_btn", "learning_sett"),
        ("learning_daily_qty", "learning_daily_qty"),
        ("learning_intervals", "learning_intervals"),
        ("learning_reminder_interval", "learning_reminder_interval"),
        ("learning_reminder_cooldown", "learning_reminder_cooldown"),
        ("learning_ready_quorum", "learning_ready_quorum"),
        # Localized study buttons (P2.5): i18n key -> action.
        ("learning", "learning"),
        ("learning_start", "learning_start"),
        ("add_for_learning", "learning_add"),
        ("learning_skip", "learning_skip"),
        ("learning_next", "learning_next"),
        ("learning_reset", "learning_reset"),
        ("learning_mix_mode", "learning_mix_mode"),
        ("send_learn", "send_learn"),
        ("clarify_fields", "clarify_fields"),
        ("input_err", "input_err"),
        ("standard_fsrs", "standard_fsrs"),
        ("quiz_words", "quiz_words"),
        ("quiz_phrases", "quiz_phrases"),
        ("weak_article_mode", "weak_article"),
        ("weak_plural_mode", "weak_plural"),
        ("weak_verb_mode", "weak_verb"),
        ("weak_prateritum_mode", "weak_prateritum"),
        ("weak_partizip2_mode", "weak_partizip2"),
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
    # Quiz-direction labels carry the dict language now (#17): map every variant
    # ("Ru > De"/"Ua > De"/"En > De" and "De > …") back, so a press routes
    # regardless of which dict language rendered the button.
    for src in _QUIZ_DIR_SOURCES:
        mapping[f"{src} > De"] = "quiz_l1_de"
        mapping[f"De > {src}"] = "quiz_de_l1"
    mapping[QUIZ_PL_SG_TEXT] = "quiz_pl_sg"
    # Study/weak-mode buttons are localized and routed via the keys list above.
    mapping["flags!"] = "review_flags"
    mapping[PARSE_VF_TEXT] = "review_flags"
    mapping["Back"] = "back"
    mapping["Stop"] = "stop"
    return mapping.get(text.strip(), "")


logger = logging.getLogger(__name__)

# Connect-phase httpx failures: the request never left this machine, so a resend
# cannot create a duplicate message. PTB 20.7 chains the original httpx error as
# __cause__ (TimedOut/NetworkError `from err` in _httpxrequest.py). PoolTimeout
# is documented by PTB as "Request was *not* sent to Telegram".
_SAFE_TO_RESEND_CAUSES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _send_may_have_reached_telegram(exc):
    """True unless the failure certainly happened before the request was sent."""
    return not isinstance(exc.__cause__, _SAFE_TO_RESEND_CAUSES)


async def _reply_text_with_retry(message, text, max_retries=3, **kwargs):
    """reply_text with duplicate-safe retries (T2).

    - RetryAfter (flood): wait and retry — Telegram rejected the request, no
      duplicate possible.
    - Connect-phase network failures: retry with backoff; if retries run out,
      raise (the message truly never went out — the error handler nudges the
      user, which IS required action).
    - Read-phase/unknown failures: the message MAY already be delivered, so a
      resend risks a duplicate quiz question. Owner decision (2026-07-11): stay
      silent — log a warning, return None, no retry, no extra notification; the
      user just taps again if nothing arrived.
    """
    attempts = 0
    while True:
        try:
            return await message.reply_text(text, **kwargs)
        except RetryAfter as exc:
            attempts += 1
            if attempts > max_retries:
                raise
            await asyncio.sleep(int(getattr(exc, "retry_after", 1)) + 1)
        except (TimedOut, NetworkError) as exc:
            if _send_may_have_reached_telegram(exc):
                logger.warning(
                    "send may have reached Telegram; not resending to avoid a duplicate (%r)", exc
                )
                return None
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
    context.user_data.pop("last_answered_message_id", None)


async def _back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    """Handle back to main menu. When `context` is given, clears the same transient
    quiz/drill state as `_back_to_tests` (used when exiting a card straight to the
    main menu, e.g. a report-opened clarify card — bug #25)."""
    if context is not None:
        _reset_retry_state(context)
        _reset_fsrs_cycle_state(context)
        _reset_weak_drill_state(context)
        _clear_last_answer(context)
        context.user_data.pop("quiz_mode", None)
        context.user_data.pop("quiz_direction_base", None)
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
        context.user_data.pop("quiz_mode", None)
        context.user_data.pop("quiz_direction_base", None)
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "tests") + ":",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU
