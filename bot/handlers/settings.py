"""Settings handlers: the algo/drill/learning settings sub-menus and their edit
flows, the interface/dictionary language selection, the in-bot unit-test report,
and the topic-filter editor. The Settings root router (settings_menu_choice,
verbformen-heavy) is added in a follow-up step.
"""

import os
import re
import subprocess

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes, ConversationHandler

from ..app_config import (
    ADD_INPUT,
    ALGO_SETTINGS_CONFIRM,
    ALGO_SETTINGS_EDIT,
    ALGO_SETTINGS_MENU,
    DRILL_SETTINGS_MENU,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_KEY,
    LEARNING_READY_QUORUM_KEY,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_REMINDER_INTERVAL_KEY,
    LEARNING_SETTINGS_MENU,
    LEARNING_WARMUP_INTERVALS,
    MENU,
    OWNER_DICT_LANG_KEY,
    OWNER_INTERFACE_LANG_KEY,
    QUIZ_RU_DE_MENU,
    SELECT_DICT_LANG,
    SELECT_UI_LANG,
    SESSION_RETRY_GAP_KEY,
    SESSION_RETRY_IT_KEY,
    SETTINGS_MENU,
    TESTS_MENU,
    TOPIC_FILTER_EDIT,
    TOPIC_FILTER_KEY,
    WEAK_DRILL_GAP_KEY,
    WEAK_DRILL_REPEATS_KEY,
    get_db_path,
    is_owner as _is_owner_only,
)
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_main_menu
from ..common.settings import (
    _learning_daily_qty,
    _learning_intervals_text,
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
    _session_retry_gap,
    _session_retry_iterations,
    _topic_filter_banner,
    _topic_filter_value,
    _weak_drill_gap,
    _weak_drill_repeats,
)
from ..i18n import dict_choices, interface_choices, parse_dict_choice, parse_interface_choice, t
from ..storage import (
    WEAK_ACTIVE_CLEAR_STREAK,
    WEAK_ACTIVE_MIN_ERRORS,
    WEAK_ACTIVE_MIN_STREAK_WRONG,
    WEAK_REVIEW_DAYS_STAGE1,
    WEAK_REVIEW_DAYS_STAGE2,
    WEAK_REVIEW_DAYS_STAGE3,
    _parse_learning_intervals,
    list_topics,
    reset_periodicity,
    set_setting,
)
from ..ui.keyboards import (
    _add_keys,
    _algo_settings_menu_keys,
    _drill_settings_menu_keys,
    _learning_settings_menu_keys,
    _main_menu_markup,
    _ru_de_menu_keys,
    _settings_menu_keys,
    _tests_menu_keys,
)


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
    from ..handlers.add_words import _process_next_ai_word

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
