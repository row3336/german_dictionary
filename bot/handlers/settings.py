"""Settings handlers: the algo/drill/learning settings sub-menus and their edit
flows, the interface/dictionary language selection, the in-bot unit-test report,
and the topic-filter editor. The Settings root router (settings_menu_choice,
verbformen-heavy) is added in a follow-up step.
"""

import asyncio
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
    FSRS_SETTINGS_MENU,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_KEY,
    LEARNING_READY_QUORUM_KEY,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_REMINDER_INTERVAL_KEY,
    LEARNING_SETTINGS_MENU,
    LEARNING_WARMUP_INTERVALS,
    MENU,
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
    is_owner,
)
from ..common.lang import _quiz_dir_labels, _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_main_menu
from ..common.settings import (
    _learning_daily_qty,
    _learning_intervals_text,
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
    _session_retry_gap,
    _session_retry_iterations,
    _topic_filter_value,
    _weak_drill_gap,
    _weak_drill_repeats,
)
from ..handlers.access import access_allowed, notify_owner_access_request
from ..i18n import dict_choices, interface_choices, parse_dict_choice, parse_interface_choice, t
from ..storage import (
    WEAK_ACTIVE_CLEAR_STREAK,
    WEAK_ACTIVE_MIN_ERRORS,
    WEAK_ACTIVE_MIN_STREAK_WRONG,
    WEAK_REVIEW_DAYS_STAGE1,
    WEAK_REVIEW_DAYS_STAGE2,
    WEAK_REVIEW_DAYS_STAGE3,
    _parse_learning_intervals,
    get_user_status,
    list_topics,
    reset_periodicity,
    reset_user_settings,
    set_setting,
    set_user_dict_lang,
    set_user_interface_lang,
    set_user_setting,
)
from ..ui.keyboards import (
    _add_keys,
    _algo_settings_menu_keys,
    _drill_settings_menu_keys,
    _fsrs_settings_menu_keys,
    _language_menu_keys,
    _learning_settings_menu_keys,
    _main_menu_markup,
    _ru_de_menu_keys,
    _settings_menu_keys,
    _tests_menu_keys,
)

# Extra imports for the verbformen-heavy Settings root router (settings_menu_choice).
from ..app_config import BACK_TEXT, DUMP_MENU, LANGUAGE_MENU
from ..handlers.dump import _dump_menu_keys
from ..labels import (
    VF_APPLY_TEXT,
    VF_DRY_RUN_ALL_TEXT,
    VF_DRY_RUN_NEW_TEXT,
    VF_RETRY_TEXT,
    VF_SET_JITTER_TEXT,
    VF_SET_PROXY_TEXT,
    VF_SET_SLEEP_TEXT,
    VF_SHOW_DIFF_TEXT,
    VF_STOP_TEXT,
    VF_TEST_PROXY_TEXT,
    VF_VERIFY_TEXT,
)
from ..storage import (
    approve_vf_checks,
    get_word_by_id,
    list_approved_vf_checks,
    list_latest_vf_mismatches,
    list_latest_vf_retryable,
    list_vf_candidates,
    mark_vf_check_applied,
)
from ..text_utils import split_text_chunks as _split_text_chunks
from ..verb_forms_parsing.handlers import (
    VF_RUNNING_TASKS,
    VF_STOP_FLAGS,
    _send_vf_menu,
    _vf_menu_markup,
    _vf_run_dry_task,
)
from ..verb_forms_parsing.parser import (
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


def _settings_edit_back_markup(user_id):
    """Keyboard for value-input prompts (bug #19): a single Back button so the
    user can leave the input mode without having to type a value. The edit/confirm
    handlers treat 'back' (and, for the reset confirm, any non-'yes') as cancel."""
    return ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


def _settings_submenu_text(lang, desc_key, rows):
    """Build a settings submenu message (bug #0): the mode description, then each
    parameter's current value with a one-line explanation, all in the user's lang.
    `rows` is a list of (label_key, value, explanation_key)."""
    lines = [t(lang, desc_key), ""]
    for label_key, value, expl_key in rows:
        lines.append(f"• {t(lang, label_key)} = {value}")
        lines.append(f"  {t(lang, expl_key)}")
    return "\n".join(lines)


async def _send_algo_settings_menu(update: Update, user_id):
    """Algo settings root (bug #0): FSRS/weak/learning params live in their own
    submenus now; the root only carries the topic filter + resets."""
    lang = _user_interface_lang(user_id)
    topics = list_topics(get_db_path())
    topics_line = ", ".join(topics) if topics else "-"
    msg = "\n".join(
        [
            f"{t(lang, 'algo_settings')}:",
            f"{t(lang, 'topic_filter')} = {_topic_filter_value(user_id)}",
            f"topics = {topics_line}",
        ]
    )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return ALGO_SETTINGS_MENU


async def _send_fsrs_settings_menu(update: Update, user_id):
    """FSRS settings submenu (bug #0): the two in-session retry params."""
    lang = _user_interface_lang(user_id)
    rows = [
        ("ses_rtr_gap", _session_retry_gap(get_db_path(), user_id), "ses_rtr_gap_desc"),
        ("ses_rtr_it", _session_retry_iterations(get_db_path(), user_id), "ses_rtr_it_desc"),
    ]
    await update.message.reply_text(
        _settings_submenu_text(lang, "fsrs_sett_desc", rows),
        reply_markup=ReplyKeyboardMarkup(_fsrs_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return FSRS_SETTINGS_MENU


async def _send_drill_settings_menu(update: Update, user_id):
    """Weak settings submenu (bug #0, UI label: weak_sett): the weak-drill params,
    plus an owner-only footer with the weak-episode thresholds (constants)."""
    lang = _user_interface_lang(user_id)
    rows = [
        ("weak_drill_gap", _weak_drill_gap(get_db_path(), user_id), "weak_drill_gap_desc"),
        ("weak_drill_repeats", _weak_drill_repeats(get_db_path(), user_id), "weak_drill_repeats_desc"),
    ]
    msg = _settings_submenu_text(lang, "weak_sett_desc", rows)
    if is_owner(user_id):
        msg += "\n\n" + "\n".join(
            [
                f"weak.enter = err>={WEAK_ACTIVE_MIN_ERRORS} or wrong_streak>={WEAK_ACTIVE_MIN_STREAK_WRONG} (held until clear)",
                f"weak.to_review = {WEAK_ACTIVE_CLEAR_STREAK} correct in row",
                f"weak.review_days = {WEAK_REVIEW_DAYS_STAGE1}/{WEAK_REVIEW_DAYS_STAGE2}/{WEAK_REVIEW_DAYS_STAGE3}",
            ]
        )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(_drill_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return DRILL_SETTINGS_MENU


async def _send_learning_settings_menu(update: Update, user_id):
    """Learning settings submenu (bug #0): editable params with explanations, plus
    an owner-only footer with the fixed warm-up schedule / on-wrong behavior."""
    lang = _user_interface_lang(user_id)
    rows = [
        ("learning_daily_qty", _learning_daily_qty(get_db_path(), user_id), "learning_daily_qty_desc"),
        ("learning_intervals", _learning_intervals_text(get_db_path(), user_id), "learning_intervals_desc"),
        ("learning_ready_quorum", _learning_ready_quorum(get_db_path(), user_id), "learning_ready_quorum_desc"),
        ("learning_reminder_interval", _learning_reminder_interval(get_db_path(), user_id), "learning_reminder_interval_desc"),
        ("learning_reminder_cooldown", _learning_reminder_cooldown(get_db_path(), user_id), "learning_reminder_cooldown_desc"),
    ]
    msg = _settings_submenu_text(lang, "learning_sett_desc", rows)
    if is_owner(user_id):
        msg += "\n\n" + "\n".join(
            [
                f"learning_warmup = {','.join(str(v) for v in LEARNING_WARMUP_INTERVALS)}",
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
    # Project root = three levels up from bot/handlers/settings.py (tests/ + scripts/ live there).
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    # Shown before the user has picked a language: use their current interface
    # lang if any (existing ru/uk user re-picking from Settings), else the default
    # (a brand-new onboarding user has none yet -> English).
    lang = _user_interface_lang(update.effective_user.id)
    await update.message.reply_text(
        t(lang, "choose_ui_lang"),
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


async def _send_language_menu(update: Update, user_id):
    """Show the language submenu (P6.3): interface + dictionary language."""
    lang = _user_interface_lang(user_id)
    await update.message.reply_text(
        t(lang, "language_sett"),
        reply_markup=ReplyKeyboardMarkup(_language_menu_keys(user_id), resize_keyboard=True),
    )
    return LANGUAGE_MENU


async def language_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Language submenu router: interface/dict language; back returns to Settings."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU
    # #4: mark that the picker was opened from Settings (not onboarding), so after
    # the choice we return to the language submenu instead of dragging the user
    # through the dictionary-language onboarding step / main menu.
    if action == "interface_lang":
        context.user_data["lang_pick_from_settings"] = True
        return await _ask_interface_language(update)
    if action == "dict_lang":
        context.user_data["lang_pick_from_settings"] = True
        return await _ask_dictionary_language(update, user_id)
    return await _send_language_menu(update, user_id)


async def select_ui_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interface language choice. For a pending guest it triggers the owner request (P3.1)."""
    user_id = update.effective_user.id

    interface_lang = parse_interface_choice(update.message.text.strip())
    if not interface_lang:
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "choose_ui_lang"),
            reply_markup=ReplyKeyboardMarkup([interface_choices()], resize_keyboard=True),
        )
        return SELECT_UI_LANG

    set_user_interface_lang(get_db_path(), user_id, interface_lang)
    # #4: changing the interface language from Settings only switches it and goes
    # back to the language submenu — it must NOT continue into dict-lang onboarding.
    if context.user_data.pop("lang_pick_from_settings", False):
        return await _send_language_menu(update, user_id)
    # Owner and approved guest continue onboarding (dictionary language).
    if access_allowed(user_id):
        return await _ask_dictionary_language(update, user_id)
    # A pending guest just chose a language -> send the owner request and ask them to wait.
    await notify_owner_access_request(context, user_id)
    await update.message.reply_text(t(_user_interface_lang(user_id), "access_pending"))
    return ConversationHandler.END


async def select_dict_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dictionary language choice (owner or approved guest) -> main menu."""
    user_id = update.effective_user.id
    if not access_allowed(user_id):
        return ConversationHandler.END

    lang = _user_interface_lang(user_id)
    dict_lang = parse_dict_choice(update.message.text.strip(), lang)
    if not dict_lang:
        return await _ask_dictionary_language(update, user_id)

    set_user_dict_lang(get_db_path(), user_id, dict_lang)
    # #4: a dict-language change made from Settings returns to the language submenu;
    # only the onboarding path falls through to the main menu.
    if context.user_data.pop("lang_pick_from_settings", False):
        return await _send_language_menu(update, user_id)
    await update.message.reply_text(
        t(lang, "main_menu"),
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


async def algo_settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Algo settings menu router (available to guests; unit_tests is owner-only, P3.2)."""
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

    if action == "fsrs_sett":
        return await _send_fsrs_settings_menu(update, user_id)
    if action == "weak_sett":
        return await _send_drill_settings_menu(update, user_id)
    if action == "learning_sett":
        return await _send_learning_settings_menu(update, user_id)

    if text == t(lang, "topic_filter"):
        from .topic_filter import send_topic_filter_screen  # local import: anti-cycle
        return await send_topic_filter_screen(update, context, user_id, "algo")

    if text == t(lang, "unit_tests"):
        if not is_owner(user_id):  # owner-only (P3.2): in-bot test run
            return ALGO_SETTINGS_MENU
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
            t(lang, "settings_reset_periodicity_confirm"),
            reply_markup=_settings_edit_back_markup(user_id),
        )
        return ALGO_SETTINGS_CONFIRM

    if text == t(lang, "reset_defaults"):
        # clear this user's personal study settings -> getters fall back to defaults (P2.4d)
        reset_user_settings(
            get_db_path(),
            user_id,
            keys=[
                SESSION_RETRY_GAP_KEY,
                SESSION_RETRY_IT_KEY,
                WEAK_DRILL_GAP_KEY,
                WEAK_DRILL_REPEATS_KEY,
                LEARNING_DAILY_QTY_KEY,
                LEARNING_INTERVALS_KEY,
                LEARNING_REMINDER_INTERVAL_KEY,
                LEARNING_REMINDER_COOLDOWN_KEY,
                LEARNING_READY_QUORUM_KEY,
            ],
        )
        await update.message.reply_text(t(lang, "settings_algo_reset_done"))
        return await _send_algo_settings_menu(update, user_id)

    await update.message.reply_text(
        t(lang, "settings_use_algo_buttons"),
        reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return ALGO_SETTINGS_MENU


async def fsrs_settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSRS settings submenu router (bug #0): edit the two in-session retry params."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)

    if action == "back":
        return await _send_algo_settings_menu(update, user_id)

    if text == t(lang, "ses_rtr_gap"):
        context.user_data["algo_setting_edit"] = SESSION_RETRY_GAP_KEY
        context.user_data["algo_edit_return"] = "fsrs"
        await update.message.reply_text(
            t(lang, "clarify_enter_value").format(field=t(lang, "ses_rtr_gap")),
            reply_markup=_settings_edit_back_markup(user_id),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "ses_rtr_it"):
        context.user_data["algo_setting_edit"] = SESSION_RETRY_IT_KEY
        context.user_data["algo_edit_return"] = "fsrs"
        await update.message.reply_text(
            t(lang, "clarify_enter_value").format(field=t(lang, "ses_rtr_it")),
            reply_markup=_settings_edit_back_markup(user_id),
        )
        return ALGO_SETTINGS_EDIT

    return await _send_fsrs_settings_menu(update, user_id)


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
            t(lang, "clarify_enter_value").format(field=t(lang, "weak_drill_gap")),
            reply_markup=_settings_edit_back_markup(user_id),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "weak_drill_repeats"):
        context.user_data["algo_setting_edit"] = WEAK_DRILL_REPEATS_KEY
        context.user_data["algo_edit_return"] = "drill"
        await update.message.reply_text(
            t(lang, "clarify_enter_value").format(field=t(lang, "weak_drill_repeats")),
            reply_markup=_settings_edit_back_markup(user_id),
        )
        return ALGO_SETTINGS_EDIT

    await update.message.reply_text(
        t(lang, "settings_use_drill_buttons"),
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
        await update.message.reply_text(t(lang, "clarify_enter_value").format(field=t(lang, "learning_daily_qty")), reply_markup=_settings_edit_back_markup(user_id))
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_intervals"):
        context.user_data["algo_setting_edit"] = LEARNING_INTERVALS_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text(
            t(lang, "clarify_enter_value").format(field=t(lang, "learning_intervals")),
            reply_markup=_settings_edit_back_markup(user_id),
        )
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_reminder_interval"):
        context.user_data["algo_setting_edit"] = LEARNING_REMINDER_INTERVAL_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text(t(lang, "clarify_enter_value").format(field=t(lang, "learning_reminder_interval")), reply_markup=_settings_edit_back_markup(user_id))
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_reminder_cooldown"):
        context.user_data["algo_setting_edit"] = LEARNING_REMINDER_COOLDOWN_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text(t(lang, "clarify_enter_value").format(field=t(lang, "learning_reminder_cooldown")), reply_markup=_settings_edit_back_markup(user_id))
        return ALGO_SETTINGS_EDIT
    if text == t(lang, "learning_ready_quorum"):
        context.user_data["algo_setting_edit"] = LEARNING_READY_QUORUM_KEY
        context.user_data["algo_edit_return"] = "learning"
        await update.message.reply_text(t(lang, "clarify_enter_value").format(field=t(lang, "learning_ready_quorum")), reply_markup=_settings_edit_back_markup(user_id))
        return ALGO_SETTINGS_EDIT
    await update.message.reply_text(
        t(lang, "settings_use_learning_buttons"),
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
        if return_to == "fsrs":
            return await _send_fsrs_settings_menu(update, user_id)
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
        if return_to == "fsrs":
            return await _send_fsrs_settings_menu(update, user_id)
        if return_to == "drill":
            return await _send_drill_settings_menu(update, user_id)
        if return_to == "learning":
            return await _send_learning_settings_menu(update, user_id)
        return await _send_algo_settings_menu(update, user_id)

    if key == LEARNING_INTERVALS_KEY:
        values = _parse_learning_intervals(text)
        if not values:
            await update.message.reply_text(t(_user_interface_lang(user_id), "settings_enter_csv_seconds"))
            return ALGO_SETTINGS_EDIT
        # all study settings (algo/drill/learning) are now per-user (P2.4d)
        set_user_setting(get_db_path(), user_id, key, ",".join(str(v) for v in values))
    else:
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text(t(_user_interface_lang(user_id), "settings_enter_number"))
            return ALGO_SETTINGS_EDIT
        if key == LEARNING_READY_QUORUM_KEY:
            if value < 1 or value > 100:
                await update.message.reply_text(t(_user_interface_lang(user_id), "settings_range_1_100"))
                return ALGO_SETTINGS_EDIT
        elif value < 1:
            await update.message.reply_text(t(_user_interface_lang(user_id), "settings_min_1"))
            return ALGO_SETTINGS_EDIT
        # all study settings (algo/drill/learning) are now per-user (P2.4d)
        set_user_setting(get_db_path(), user_id, key, str(value))
    context.user_data.pop("algo_setting_edit", None)
    context.user_data.pop("algo_edit_return", None)
    if return_to == "fsrs":
        return await _send_fsrs_settings_menu(update, user_id)
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
        reset_periodicity(get_db_path(), user_id)
        await update.message.reply_text(t(_user_interface_lang(user_id), "settings_periodicity_reset_done"))
        return await _send_algo_settings_menu(update, user_id)

    await update.message.reply_text(t(_user_interface_lang(user_id), "settings_canceled"))
    return await _send_algo_settings_menu(update, user_id)


async def topic_filter_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Topic filter text input (quiz filter) + owner add-flow topic selection."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back":
        return_to = context.user_data.pop("topic_return", "")
        if return_to == "add_queue":
            await update.message.reply_text(
                t(lang, "settings_topic_selection_canceled"),
                reply_markup=ReplyKeyboardMarkup(_add_keys(user_id), resize_keyboard=True),
            )
            context.user_data.pop("pending_add_queue", None)
            context.user_data.pop("pending_add_stats", None)
            context.user_data.pop("pending_new_topic", None)
            return ADD_INPUT
        if return_to == "algo":
            return await _send_algo_settings_menu(update, user_id)
        if return_to == "tests_menu":
            await update.message.reply_text(
                t(lang, "tests") + ":",
                reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
            )
            return TESTS_MENU
        if return_to == "ru_de_menu":
            await update.message.reply_text(
                f"{_quiz_dir_labels(user_id)[0]}:",
                reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
            )
            return QUIZ_RU_DE_MENU
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    # Owner add-flow owns its own parsing (name + description, new-topic prompt) — spec #8.
    if context.user_data.get("topic_return") == "add_queue":
        from ..handlers.add_words import handle_add_topic_input  # local import: anti-cycle
        return await handle_add_topic_input(update, context, text)

    value = text.strip()
    if not value:
        await update.message.reply_text(t(lang, "topic_filter_empty"))
        return TOPIC_FILTER_EDIT

    set_user_setting(get_db_path(), user_id, TOPIC_FILTER_KEY, value)
    return_to = context.user_data.pop("topic_return", "")
    if return_to == "ru_de_menu":
        await update.message.reply_text(
            t(lang, "topic_filter_set").format(value=value),
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    if return_to == "algo":
        await update.message.reply_text(
            t(lang, "topic_filter_set").format(value=value),
            reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return ALGO_SETTINGS_MENU
    if return_to == "tests_menu":
        await update.message.reply_text(
            t(lang, "topic_filter_set").format(value=value),
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU

    await update.message.reply_text(
        t(lang, "topic_filter_set").format(value=value),
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Settings menu router: languages/algo (guest ok), parse_VF/dump are owner-only (P3.2)."""
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

    if text == t(_user_interface_lang(user_id), "language_sett"):
        return await _send_language_menu(update, user_id)

    if action == "algo_settings":
        return await _send_algo_settings_menu(update, user_id)

    if text == t(_user_interface_lang(user_id), "feedback_btn"):
        from .feedback import _start_feedback
        return await _start_feedback(update, user_id)

    if is_owner(user_id) and text == t(_user_interface_lang(user_id), "review_menu"):
        from .review import _send_review_menu
        return await _send_review_menu(update, user_id)

    if not is_owner(user_id) and text == t(_user_interface_lang(user_id), "support_project"):
        from .donate import start_donate
        return await start_donate(update, user_id)

    # parse_VF and dump are owner-only (P3.2). Defense in depth: the buttons are hidden
    # from guests in _settings_menu_keys, plus we close the actions against raw text.
    if action in {"dump", "review_flags"} and not is_owner(user_id):
        await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
        return SETTINGS_MENU

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

    await update.message.reply_text(
        t(_user_interface_lang(user_id), "settings_use_buttons"),
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU
