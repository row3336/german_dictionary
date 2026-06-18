"""Top-level menu routers: /start onboarding, the main menu, the tests menu, and
the Ru>De quiz sub-menu. These are the dispatch hubs that hand off into the
feature handlers (add_words / settings / db_search / quiz / weak / learning).
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ..app_config import (
    ADD_INPUT,
    MENU,
    OWNER_DICT_LANG_KEY,
    OWNER_INTERFACE_LANG_KEY,
    QUIZ_RU_DE_MENU,
    QUIZ_VERB_WEAK_MENU,
    SETTINGS_MENU,
    TESTS_MENU,
    TOPIC_FILTER_EDIT,
    get_db_path,
    get_info_path,
    is_owner as _is_owner_only,
)
from ..common.lang import _mode_de_l1, _mode_l1_de, _mode_pl_sg, _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _clear_last_answer
from ..common.session import _reset_retry_state, _reset_session, _reset_weak_drill_state
from ..common.settings import _topic_filter_banner, _topic_filter_value
from ..handlers.db_search import _send_db_search_prompt
from ..handlers.learning import _send_learning_menu
from ..handlers.quiz import _send_de_ru_prompt, _send_pl_sg_prompt, _send_ru_de_prompt
from ..handlers.settings import _ask_interface_language
from ..handlers.weak import _send_article_weak_prompt, _send_plural_weak_prompt
from ..i18n import normalize_interface_lang, t
from ..labels import (
    QUIZ_DE_RU_TEXT,
    QUIZ_PL_SG_TEXT,
    QUIZ_RU_DE_TEXT,
    STANDARD_FSRS_TEXT,
    TEST_ARTICLE_WEAK_TEXT,
    TEST_PARTIZIP2_WEAK_TEXT,
    TEST_PLURAL_WEAK_TEXT,
    TEST_PRATERITUM_WEAK_TEXT,
    TEST_VERB_WEAK_TEXT,
)
from ..storage import get_setting, get_statistics_snapshot, list_topics
from ..text_utils import split_text_chunks as _split_text_chunks
from ..ui.keyboards import (
    _add_input_markup,
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
    return "Info file not found."


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
            f"today complete fsrs Ru > De = {stats['today_complete_fsrs_ru_de']}",
            f"today complete fsrs De > Ru = {stats['today_complete_fsrs_de_ru']}",
            f"today complete pl > sing = {stats.get('today_complete_pl_sg', 0)}",
            "",
            f"today complete weak article = {stats['today_complete_weak_article']}",
            f"today complete weak plural = {stats['today_complete_weak_plural']}",
            f"today complete weak partizip2 = {stats['today_complete_weak_partizip2']}",
            f"today complete weak preteritum = {stats['today_complete_weak_prateritum']}",
            "",
            f"5+ correct streak = {stats.get('streak_5p', 0)}",
            f"4 correct streak = {stats.get('streak_4', 0)}",
            f"3 correct streak = {stats.get('streak_3', stats.get('streak_3p', 0))}",
            f"2 correct streak = {stats.get('streak_2', 0)}",
            f"1 correct = {stats.get('streak_1', 0)}",
            f"wrong fsrs = {stats.get('wrong_fsrs', 0)}",
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
            f"learning weekly = {stats.get('learning_weekly', 0)}",
            "",
            "word types:",
            *[f"{k} = {word_types[k]}" for k in sorted(word_types.keys())],
        ]
        await update.message.reply_text(
            f"```text\n{chr(10).join(lines)}\n```",
            reply_markup=_main_menu_markup(user_id),
            parse_mode="Markdown",
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
        await update.message.reply_text(
            f"{QUIZ_RU_DE_TEXT}:\n\n{_topic_filter_banner()}",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE_MENU
    if action == "quiz_de_l1":
        _reset_retry_state(context)
        _clear_last_answer(context)
        _reset_session(context, _mode_de_l1(user_id))
        return await _send_de_ru_prompt(
            update,
            context,
            f"{QUIZ_DE_RU_TEXT}:\n\n{_topic_filter_banner()}",
        )
    if action == "quiz_pl_sg":
        _reset_retry_state(context)
        _clear_last_answer(context)
        _reset_session(context, _mode_pl_sg(user_id))
        return await _send_pl_sg_prompt(
            update,
            context,
            f"{QUIZ_PL_SG_TEXT}:\n\n{_topic_filter_banner()}",
        )
    if action == "learning":
        return await _send_learning_menu(update, user_id)
    if action == "topic_filter":
        current = _topic_filter_value()
        topics = list_topics(get_db_path())
        topics_line = ", ".join(topics) if topics else "-"
        await update.message.reply_text(
            f"Current topic filter: {current}\nTopics: {topics_line}\nSend e.g. 1+2+food or 'all'.",
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        context.user_data["topic_return"] = "tests_menu"
        return TOPIC_FILTER_EDIT

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
    if text == STANDARD_FSRS_TEXT:
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
