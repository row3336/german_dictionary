import logging
import os
from logging.handlers import TimedRotatingFileHandler
from datetime import (
    datetime,
    timezone,
)

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from .app_config import (
    ACCESS_MESSAGE,
    BROADCAST_CONFIRM,
    BROADCAST_INPUT,
    DONATE_AMOUNT,
    DONATE_CUSTOM,
    FEEDBACK_INPUT,
    LANGUAGE_MENU,
    OWNER_REVIEW_MENU,
    REVIEW_FEEDBACK,
    REVIEW_REPORTS,
    ACCESS_REJECTED,
    ADD_CLARIFY,
    ADD_CONFIRM,
    ADD_EDIT,
    ADD_INPUT,
    ADD_PRECHECK,
    ADD_PRECHECK_EDIT,
    ALGO_SETTINGS_CONFIRM,
    ALGO_SETTINGS_EDIT,
    ALGO_SETTINGS_MENU,
    AUTO_DUMP_MENU,
    AUTO_DUMP_SETTING_KEY,
    DB_SEARCH,
    DB_SEARCH_VIEW,
    DRILL_SETTINGS_MENU,
    DUMP_MENU,
    FSRS_REPORT_HOUR_KEY,
    FSRS_SETTINGS_MENU,
    FSRS_REPORT_UTC_HOUR_DEFAULT,
    FSRS_WEEKLY_REPORT_WEEK_KEY,
    LEARNING_DAILY_QTY_DEFAULT,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_DEFAULT,
    LEARNING_INTERVALS_KEY,
    LEARNING_MENU,
    LEARNING_QUIZ,
    LEARNING_READY_QUORUM_DEFAULT,
    LEARNING_READY_QUORUM_KEY,
    LEARNING_REMINDER_COOLDOWN_DEFAULT,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_REMINDER_INTERVAL_DEFAULT,
    LEARNING_REMINDER_INTERVAL_KEY,
    LEARNING_SETTINGS_MENU,
    MENU,
    OWNER_DICT_LANG_KEY,
    OWNER_INTERFACE_LANG_KEY,
    QUIZ_ARTICLE,
    QUIZ_ARTICLE_WEAK,
    QUIZ_DE_RU,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PLURAL_WEAK,
    QUIZ_PL_SG,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE,
    QUIZ_RU_DE_MENU,
    QUIZ_VERB_WEAK_MENU,
    SELECT_DICT_LANG,
    SELECT_UI_LANG,
    SETTINGS_MENU,
    START_TEXT,
    SYN_ADD_INPUT,
    SYN_AUTO_CONFIRM,
    SYN_CLEAR_CONFIRM,
    SYN_MENU,
    SYN_REMOVE_INPUT,
    TESTS_MENU,
    TOPIC_FILTER_EDIT,
    TOPIC_FILTER_KEY,
    WEAK_DRILL_GAP_DEFAULT,
    WEAK_DRILL_GAP_KEY,
    WEAK_DRILL_REPEATS_DEFAULT,
    WEAK_DRILL_REPEATS_KEY,
    get_db_path,
    get_dump_dir,
    get_log_path,
)
from .storage import (
    add_word_flag,
    cleanup_transaction_logs,
    delete_transaction_logs,
    get_setting,
    list_transaction_logs,
    restore_user_word_state,
    set_setting,
)
from .common.db_safety import apply_migrations_safely
from .common.lang import _user_interface_lang
from .common.outbound import configure_outbound_logger
from .common.navigation import _back_to_tests
from .common.txlog import UNDO_MSG_ID_KEY, UNDO_TOTAL_KEY
from .ui.keyboards import _main_menu_markup
from .i18n import t
from .handlers.dump import (
    auto_dump_choice,
    dump_menu_choice,
)
from .handlers.db_search import (
    db_search_input,
    db_search_view,
)
from .handlers.synonyms import (
    synonyms_add_input,
    synonyms_auto_confirm,
    synonyms_clear_confirm,
    synonyms_menu,
    synonyms_remove_input,
)
from .handlers.weak import (
    quiz_article_weak,
    quiz_partizip2_weak,
    quiz_plural_weak,
    quiz_prateritum_weak,
    quiz_verb_weak_menu_choice,
)
from .handlers.learning import (
    learning_menu_choice,
    learning_quiz,
)
from .handlers.add_words import (
    add_clarify,
    add_confirm,
    add_document,
    add_input,
    add_precheck,
    add_precheck_edit,
)
from .handlers.settings import (
    algo_settings_confirm,
    algo_settings_edit,
    algo_settings_menu_choice,
    drill_settings_menu_choice,
    fsrs_settings_menu_choice,
    language_menu_choice,
    learning_settings_menu_choice,
    select_dict_lang,
    select_ui_lang,
    settings_menu_choice,
    topic_filter_edit,
)
from .handlers.feedback import feedback_input
from .handlers.review import (
    owner_review_menu_choice,
    review_feedback_choice,
    review_reports_choice,
)
from .handlers.broadcast import (
    BROADCAST_MENU_CALLBACK,
    broadcast_confirm,
    broadcast_input,
    broadcast_menu_callback,
)
from .handlers.donate import (
    donate_amount_choice,
    donate_custom_input,
    precheckout_callback,
    successful_payment_callback,
)
from .handlers.access import (
    access_allowed,
    access_decision_callback,
    access_message_input,
    access_rejected_choice,
)
from .handlers.topic_filter import topic_filter_toggle_callback
from .handlers.menu import (
    _guest_start,
    menu_choice,
    quiz_ru_de_menu_choice,
    start,
    tests_menu_choice,
)
from .handlers.clarify_fields import (
    _get_clarify_target,
    _resume_after_clarify,
    clarify_fields_input,
)
from .handlers.quiz import (
    quiz_article,
    quiz_de_ru,
    quiz_pl_sg,
    quiz_ru_de,
)
from .jobs.access_reminders import _configure_access_reminders
from .jobs.auto_dump import _configure_auto_dump
from .jobs.reminders import _configure_learning_reminders
from .jobs.reports import _configure_fsrs_reports


logger = logging.getLogger(__name__)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log unhandled errors and nudge the user to retry the last action.

    Single best-effort notification only — we deliberately never auto-resend the
    answer/prompt, because a send that reached Telegram but lost its HTTP ack would
    then arrive twice (duplicate-answer risk documented in docs/todo.md).
    """
    logger.error("Unhandled error while processing update", exc_info=context.error)
    if not isinstance(update, Update) or update.effective_chat is None:
        return
    lang = _user_interface_lang(update.effective_user.id) if update.effective_user else "en"
    try:
        await context.bot.send_message(update.effective_chat.id, t(lang, "delivery_failed"))
    except Exception:  # best-effort nudge: never let it escape the error handler
        logger.warning("Failed to deliver the error nudge", exc_info=True)


async def undo_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /undo [N] rollback by transaction log."""
    user_id = update.effective_user.id
    # #9: /undo is per-user (transaction_log/restore/delete are all scoped by
    # user_id), so any APPROVED user may undo THEIR OWN steps — not owner-only.
    if not access_allowed(user_id):
        return await _guest_start(update, context)
    steps = 1
    if context.args:
        try:
            steps = int(context.args[0])
        except (TypeError, ValueError):
            steps = 1
    steps = max(1, min(20, steps))
    logs = list_transaction_logs(get_db_path(), user_id=user_id, limit=steps)
    restored = 0
    processed_ids = []
    for item in logs:
        payload = item.get("payload") or {}
        snapshot = payload.get("snapshot")
        if restore_user_word_state(get_db_path(), snapshot):
            restored += 1
        processed_ids.append(int(item.get("id")))
    if processed_ids:
        delete_transaction_logs(get_db_path(), user_id=user_id, ids=processed_ids)

    # Consecutive /undo presses accumulate into ONE message edited in place (a live
    # step counter) instead of one reply per press; the main-menu keyboard is shown
    # so the keyboard matches the state we land in (MENU). Any other action resets
    # the counter via reset_undo_progress() (called from start/menu_choice).
    total = int(context.user_data.get(UNDO_TOTAL_KEY, 0)) + restored
    if restored:
        context.user_data[UNDO_TOTAL_KEY] = total
        text = f"↩️ Undo done: {total} step(s)."
    else:
        text = f"↩️ Undo done: {total} step(s). Nothing more to undo." if total else "Nothing to undo."

    msg_id = context.user_data.get(UNDO_MSG_ID_KEY)
    if msg_id and restored:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=msg_id, text=text,
            )
            return MENU
        except Exception:
            pass  # message gone/uneditable -> fall through and send a fresh one
    sent = await update.message.reply_text(text, reply_markup=_main_menu_markup(user_id))
    context.user_data[UNDO_MSG_ID_KEY] = sent.message_id
    return MENU


async def _flag_current_word(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    """Handle flag current word."""
    entry, _ = _get_clarify_target(context)
    if not entry:
        return await _back_to_tests(update, context)
    word_id = entry.get("id")
    if not word_id:
        return await _back_to_tests(update, context)
    add_word_flag(get_db_path(), word_id=word_id, user_id=update.effective_user.id, comment="")
    if return_state == QUIZ_RU_DE:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_RU_DE)
    if return_state == QUIZ_DE_RU:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_DE_RU)
    if return_state == QUIZ_PL_SG:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PL_SG)
    if return_state == QUIZ_ARTICLE:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE_WEAK)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PLURAL_WEAK)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PRATERITUM_WEAK)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _resume_after_clarify(update, context, "✅ Flag saved.", state_override=QUIZ_PARTIZIP2_WEAK)
    return await _back_to_tests(update, context)


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
    # Outbound send log (P5) goes to its own rotating file, off the root logger.
    configure_outbound_logger()
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    _cleanup_old_dump_logs()


def _cleanup_old_dump_logs(days=14):
    """Delete log dumps and DB backups older than `days` from the backups dir."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 24 * 60 * 60)
    dump_dir = get_dump_dir()
    if not dump_dir.exists():
        return
    for pattern in ("bot-logs-*.txt", "words-*.db"):
        for path in dump_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


def _prepare_storage():
    """Handle prepare storage."""
    db_path = get_db_path()
    # Safe migration: backup-before-change → apply schema → healthcheck → log timeline.
    report = apply_migrations_safely(db_path, get_dump_dir())
    if report.get("migrated"):
        logger.info(
            "DB schema migrated v%s→v%s (backup=%s)",
            report["from_version"], report["to_version"], report["backup"],
        )
    if not report["healthcheck"]["ok"]:
        logger.error("DB healthcheck FAILED after init: %s", report["healthcheck"])
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
    if not get_setting(db_path, FSRS_REPORT_HOUR_KEY):
        set_setting(db_path, FSRS_REPORT_HOUR_KEY, str(FSRS_REPORT_UTC_HOUR_DEFAULT))
    if not get_setting(db_path, FSRS_WEEKLY_REPORT_WEEK_KEY):
        set_setting(db_path, FSRS_WEEKLY_REPORT_WEEK_KEY, "")
    cleanup_transaction_logs(db_path, days=7)


def main():
    """Handle main."""
    load_dotenv()
    _configure_logging()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing in .env")

    _prepare_storage()

    # Timeouts/pool tuned for a flaky VPS↔Telegram route: PTB defaults (connect/read
    # 5s, pool size 1, pool_timeout 1s) are too tight and cause ConnectTimeout/pool
    # exhaustion → lost/delayed quiz sends (diagnosed in docs/todo.md).
    # AIORateLimiter is the outbound governor (P5): paces ALL sends <=30 msg/s
    # globally and auto-retries 429/RetryAfter, so broadcasts to all users can't
    # burst into a Telegram flood/ban. Needs the [rate-limiter] extra (aiolimiter).
    app = (
        Application.builder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(10)
        .connection_pool_size(8)
        .build()
    )
    app.add_error_handler(_on_error)

    mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
    _configure_auto_dump(app, mode)
    _configure_learning_reminders(app)
    _configure_fsrs_reports(app)
    _configure_access_reminders(app)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("undo", undo_steps),
            MessageHandler(filters.Regex(f"^{START_TEXT}$") & ~filters.COMMAND, start),
        ],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            SELECT_UI_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_ui_lang)],
            SELECT_DICT_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_dict_lang)],
            ACCESS_REJECTED: [MessageHandler(filters.TEXT & ~filters.COMMAND, access_rejected_choice)],
            ACCESS_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, access_message_input)],
            FEEDBACK_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_input)],
            DONATE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, donate_amount_choice)],
            DONATE_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, donate_custom_input)],
            TESTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, tests_menu_choice)],
            DB_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, db_search_input)],
            DB_SEARCH_VIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, db_search_view)],
            LEARNING_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, learning_menu_choice)],
            LEARNING_QUIZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, learning_quiz)],
            QUIZ_RU_DE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de_menu_choice)],
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_menu_choice)],
            LANGUAGE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, language_menu_choice)],
            OWNER_REVIEW_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, owner_review_menu_choice)],
            REVIEW_REPORTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_reports_choice)],
            REVIEW_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, review_feedback_choice)],
            BROADCAST_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_input)],
            BROADCAST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_confirm)],
            ALGO_SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_menu_choice)],
            FSRS_SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, fsrs_settings_menu_choice)],
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
            ADD_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, clarify_fields_input)],
            QUIZ_RU_DE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de)],
            QUIZ_DE_RU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_de_ru)],
            QUIZ_PL_SG: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_pl_sg)],
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
        fallbacks=[CommandHandler("start", start), CommandHandler("undo", undo_steps)],
    )

    app.add_handler(conv)
    # Global handler for the owner's inline decisions on access requests (P3.1).
    app.add_handler(CallbackQueryHandler(access_decision_callback, pattern=r"^access:"))
    # Global handler for the topic-filter inline toggle buttons (spec #8).
    app.add_handler(CallbackQueryHandler(topic_filter_toggle_callback, pattern=r"^topicf:"))
    # Global handler for the "menu" button attached to owner broadcasts (spec #12).
    app.add_handler(
        CallbackQueryHandler(broadcast_menu_callback, pattern=f"^{BROADCAST_MENU_CALLBACK}$")
    )
    # Telegram Stars donation (P6.4): pre-checkout approval + post-payment thanks.
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
