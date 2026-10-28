"""Top-level menu routers: /start onboarding, the main menu, the tests menu, and
the Ru>De quiz sub-menu. These are the dispatch hubs that hand off into the
feature handlers (add_words / settings / db_search / quiz / learning).
"""

from ..common import match_policy

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ..app_config import (
    ADD_MENU,
    MENU,
    OWNER_DICT_LANG_KEY,
    OWNER_INTERFACE_LANG_KEY,
    QUIZ_RU_DE_MENU,
    QUIZ_RU_DE,
    QUIZ_DE_RU,
    SETTINGS_MENU,
    TESTS_MENU,
    TOPIC_FILTER_EDIT,
    get_db_path,
    get_info_path,
    is_owner as _is_owner_only,
)
from ..common.add_words_gate import add_words_enabled, is_add_action, reject_disabled_add
from ..common.lang import (
    _mode_de_l1,
    _mode_l1_de,
    _mode_pl_sg,
    _quiz_dir_labels,
    _user_interface_lang,
    phrase_mode,
)
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _clear_last_answer
from ..common.session import (
    _reset_fsrs_cycle_state,
    _reset_retry_state,
    _reset_session,
)
from ..common.settings import _topic_filter_banner, _topic_filter_value
from ..common.txlog import reset_undo_progress
from ..common.study_lock import study_lock, invalidate_study_frames
from ..study.answers import close_study_flow
from ..written.state import transport_id, ensure_frame
from ..handlers.access import access_allowed, show_rejected_screen
from ..handlers.db_search import _send_db_search_prompt
from ..handlers.learning import _send_learning_menu
from ..handlers.owner_stats import _format_progress_text, _stat_keyboard
from ..oral.domain import MODES as ORAL_MODES
from ..oral.handlers import start_oral_quiz_locked
from ..handlers.quiz import _send_de_ru_prompt, _send_pl_sg_prompt, _send_ru_de_prompt, serialized_written_entry
from ..handlers.settings import _ask_dictionary_language, _ask_interface_language
from ..i18n import normalize_interface_lang, t
from ..labels import (
    QUIZ_PL_SG_TEXT,
)
from ..storage import (
    get_setting,
    get_user_languages,
    get_user_status,
    register_or_update_user,
)
from ..text_utils import split_text_chunks as _split_text_chunks
from ..ui.keyboards import (
    _add_menu_markup,
    _de_l1_menu_keys,
    _main_menu_markup,
    _ru_de_menu_keys,
    _settings_menu_keys,
    _tests_menu_keys,
)


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
    return t(lang, "info_not_found")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start entry: owner gets their onboarding, a guest goes through the approval flow (P3.1)."""
    user = update.effective_user
    user_id = user.id
    async with study_lock(user_id):
        owner = _is_owner_only(user_id)
        pending_key, pending = "quiz_start_pending", None
        if owner or get_user_status(get_db_path(), user_id) == "approved":
            transport = transport_id(update)
            receipt = close_study_flow(get_db_path(), user_id, transport,
                                       flow_id="start", action_kind="start")
            if receipt.get("replayed"):
                pending = context.user_data.get(pending_key)
                live_study = any(context.user_data.get(key) for key in (
                    "learning_frame", "quiz_written_frame", "conjugation_frame", "oral_frame",
                    "study_frame", "quiz_entry", "quiz_published_view"))
                if not pending or pending["transport"] != transport or live_study:
                    return None
            else:
                invalidate_study_frames(context)
                # Only this accepted close may finish its interrupted display.
                # The quiz_ prefix extinguishes the right on a new study flow.
                pending = {"transport": transport}
                context.user_data[pending_key] = pending
                reset_undo_progress(context)
        else:
            reset_undo_progress(context)
        if not owner:
            state = await _guest_start(update, context)
        else:
            ui_lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "")
            dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "")
            if not ui_lang or not dict_lang:
                state = await _ask_interface_language(update)
            else:
                await update.message.reply_text(t(_user_interface_lang(user_id), "main_menu"), reply_markup=_main_menu_markup(user_id))
                state = MENU
        if pending is not None and context.user_data.get(pending_key) is pending:
            context.user_data.pop(pending_key, None)
        return state


async def _guest_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route a guest by their approval status.

    Flow: Start -> pick interface language -> owner request and "please wait" ->
    (approved) pick dictionary language -> menu / (rejected) "message the developer" screen.
    """
    user = update.effective_user
    user_id = user.id
    db = get_db_path()
    status = get_user_status(db, user_id)
    if status is None:
        # New guest: register as pending and immediately ask for the interface language
        # (the owner request is sent once a language is chosen, so we message the guest in it).
        register_or_update_user(db, user_id, user.username or "", user.first_name or "")
        return await _ask_interface_language(update)
    if status == "approved":
        ui, dl = get_user_languages(db, user_id)
        if not ui:
            return await _ask_interface_language(update)
        if not dl:
            return await _ask_dictionary_language(update, user_id)
        await update.message.reply_text(t(ui, "main_menu"), reply_markup=_main_menu_markup(user_id))
        return MENU
    if status == "rejected":
        return await show_rejected_screen(update, user_id)
    # pending: language already chosen -> just remind them to wait (don't spam the owner);
    # if the language isn't chosen yet (interrupted mid-pick) -> ask again.
    ui, _ = get_user_languages(db, user_id)
    if not ui:
        return await _ask_interface_language(update)
    await update.message.reply_text(t(ui, "access_pending"))
    return ConversationHandler.END


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main-menu router. Access: owner or approved guest; add_word is owner-only."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    reset_undo_progress(context)  # navigating the menu ends any live undo counter

    if action == "start":
        return await start(update, context)

    # Menu access: owner or approved guest. Per-item owner gating
    # (add/AI/parse_VF/dump/...) is handled in P3.2.
    if not access_allowed(user_id):
        return await _guest_start(update, context)

    if not add_words_enabled() and is_add_action(text):
        return await reject_disabled_add(update, context)

    if action == "add_word":
        # Adding words is owner-only (P3.2): a guest never writes to the shared dictionary.
        if not _is_owner_only(user_id):
            await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
            return MENU
        await update.message.reply_text(
            "Add words — choose recognition method:",
            reply_markup=_add_menu_markup(user_id),
        )
        return ADD_MENU

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
            await update.message.reply_text(t(_user_interface_lang(user_id), "info_empty"), reply_markup=_main_menu_markup(user_id))
            return MENU
        for idx, chunk in enumerate(chunks):
            markup = _main_menu_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup)
        return MENU

    if action == "stat":
        text = f"```text\n{_format_progress_text(user_id)}\n```"
        await update.message.reply_text(
            text, reply_markup=_stat_keyboard(user_id), parse_mode="Markdown"
        )
        return MENU

    await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
    return MENU


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
        context.user_data["quiz_direction_base"] = _mode_l1_de(user_id)
        context.user_data.pop("quiz_mode", None)
        l1de, _ = _quiz_dir_labels(user_id)
        await update.message.reply_text(
            f"{l1de}:\n\n{_topic_filter_banner(user_id)}",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE_MENU
    if action == "quiz_de_l1":
        context.user_data["quiz_direction_base"] = _mode_de_l1(user_id)
        context.user_data.pop("quiz_mode", None)
        _, del1 = _quiz_dir_labels(user_id)
        await update.message.reply_text(
            f"{del1}:\n\n{_topic_filter_banner(user_id)}",
            reply_markup=ReplyKeyboardMarkup(_de_l1_menu_keys(user_id), resize_keyboard=True),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE_MENU
    if action == "quiz_pl_sg":
        # Other branches own their own lock; only serialize this written start.
        @serialized_written_entry
        async def start_plural(update, context):
            _reset_retry_state(context)
            _clear_last_answer(context)
            _reset_session(context, _mode_pl_sg(user_id))
            return await _send_pl_sg_prompt(
                update, context,
                f"{QUIZ_PL_SG_TEXT}:\n\n{_topic_filter_banner(user_id)}",
            )
        return await start_plural(update, context)
    if action == "learning":
        return await _send_learning_menu(update, user_id, context)
    if action == "conjugation":
        from .conjugation import start_conjugation  # local import: anti-cycle
        return await start_conjugation(update, context)
    if action == "topic_filter":
        from .word_filter_ui import send_word_filter_screen  # local import: anti-cycle
        return await send_word_filter_screen(update, context, user_id, "tests_menu")

    await update.message.reply_text(
        t(_user_interface_lang(user_id), "settings_use_tests_buttons"),
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU


@serialized_written_entry
async def quiz_ru_de_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choose word or phrase SRS for the selected direction."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        context.user_data.pop("quiz_mode", None)
        context.user_data.pop("quiz_direction_base", None)
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU
    if action == "topic_filter":
        from .word_filter_ui import send_word_filter_screen  # local import: anti-cycle
        return await send_word_filter_screen(update, context, user_id, "ru_de_menu")
    if action in {"quiz_words", "quiz_phrases", "standard_fsrs"}:
        base_mode = context.user_data.get("quiz_direction_base") or _mode_l1_de(user_id)
        mode = phrase_mode(base_mode) if action == "quiz_phrases" else base_mode
        if mode in ORAL_MODES:
            return await start_oral_quiz_locked(update, context, mode)
        context.user_data["quiz_mode"] = mode
        _reset_retry_state(context, mode)
        _reset_fsrs_cycle_state(context, mode)
        _clear_last_answer(context)
        context.user_data.pop("quiz_entry", None)
        context.user_data.pop("quiz_last_answered_entry", None)
        _reset_session(context, mode)
        state = QUIZ_DE_RU if str(base_mode).startswith("de_") else QUIZ_RU_DE
        ensure_frame(context, mode, state)[match_policy.FRAME_KEY] = match_policy.capture(get_db_path(), user_id)
        if str(base_mode).startswith("de_"):
            return await _send_de_ru_prompt(update, context, None)
        return await _send_ru_de_prompt(update, context, None)
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "quiz_use_menu_buttons"),
        reply_markup=ReplyKeyboardMarkup(
            _de_l1_menu_keys(user_id)
            if str(context.user_data.get("quiz_direction_base") or "").startswith("de_")
            else _ru_de_menu_keys(user_id),
            resize_keyboard=True,
        ),
    )
    return QUIZ_RU_DE_MENU
