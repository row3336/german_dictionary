"""Temporary switch for bot imports; independent collectors stay separate."""

from functools import wraps

ADD_WORDS_ENABLED = False


class AddWordsDisabled(RuntimeError):
    """A bot add service was invoked while the feature is disabled."""


def add_words_enabled():
    return ADD_WORDS_ENABLED


def require_add_words_enabled():
    """Check before the first download, request, job or dictionary write."""
    if not add_words_enabled():
        raise AddWordsDisabled("Bot Add words is temporarily disabled")


def is_add_action(text):
    """Recognize stale Add keyboards even after an interface-language change."""
    from ..i18n import t
    return str(text or "").strip() in {
        t(lang, key) for lang in ("en", "ru", "uk")
        for key in ("add_word", "add_ai", "add_vf", "vf_recheck")
    }


def _clear_cached_add_state(context):
    data = getattr(context, "user_data", None)
    if data is None:
        return
    for key in (
        "add_queue", "pending_add_queue", "pending_add_stats", "pending_entry",
        "pending_word", "precheck_items", "precheck_existing", "precheck_filtered",
        "last_ai_prompt", "vf_recheck", "pending_vf_words", "add_topic_then",
        "vf_apply_entries", "add_topic",
    ):
        data.pop(key, None)
    if data.get("topic_return") == "add_queue":
        for key in ("topic_return", "pending_new_topic", "pending_topic_level"):
            data.pop(key, None)
    if data.get("edit_source") == "add":
        for key in ("edit_source", "edit_stage", "edit_field", "prep_cascade"):
            data.pop(key, None)


async def reject_disabled_add(update, context=None):
    """Explain the disabled action and replace stale keyboards with a usable menu."""
    from telegram.error import TelegramError
    from ..app_config import MENU
    from ..common.lang import _user_interface_lang
    from ..i18n import t
    from ..ui.keyboards import _main_menu_markup

    _clear_cached_add_state(context)
    uid = update.effective_user.id
    text = t(_user_interface_lang(uid), "add_words_disabled")
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer(text)
        except TelegramError:
            # An expired callback must not prevent the independent menu attempt.
            pass
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None and query is not None:
        message = query.message
    try:
        await message.reply_text(text, reply_markup=_main_menu_markup(uid))
    except TelegramError:
        # Delivery may already have succeeded. Do not resend or re-enter Add.
        pass
    return MENU


def guard_add_handler(handler):
    """Guard UI entries and their effectful helpers, including direct calls."""
    @wraps(handler)
    async def guarded(update, *args, **kwargs):
        if not add_words_enabled():
            context = kwargs.get("context")
            if context is None and args and hasattr(args[0], "user_data"):
                context = args[0]
            return await reject_disabled_add(update, context)
        return await handler(update, *args, **kwargs)
    return guarded
