"""Quiz handlers (Ru>De, De>Ru, pl>sg, article) + prompt senders and input_err
correction.

Answer checking and FSRS scoring themselves live in common/quiz_core.py: the mini
app has to reach the same verdict as the chat, and the only way to guarantee that
is to call the same function (miniapp.md §5.1). What stays here is everything that
speaks Telegram — prompts, keyboards, reactions, feedback text.

Cross-feature calls that still live in main (_send_entry_to_learning,
_flag_current_word, _start_clarify_fields) are imported locally inside the handlers
to avoid an import cycle; they move here-adjacent as learning/flags/edit are
extracted in later steps.
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    FLAG_TEXT,
    I_DONT_KNOW_TEXT,
    QUIZ_ARTICLE,
    QUIZ_DE_RU,
    QUIZ_PL_SG,
    QUIZ_RU_DE,
    TESTS_MENU,
    get_db_path,
    is_owner,
)
from ..common.lang import _mode_de_l1, _mode_l1_de, _mode_pl_sg, _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_tests, _reply_text_with_retry
from ..common.input_err import apply_fsrs_input_err
from ..common.quiz_core import (
    _check_de_l1,
    _check_pl_sg,
    _check_ru_de_strict,
    _update_srs_for_entry,
)
from ..common.word_filter import count_words_for_filter
from ..common.session import (
    _bump_answer_streak,
    _bump_session_result,
    _force_take_retry_entry,
    _mark_session_word,
    _maybe_take_retry_entry,
    _pick_due_entry,
    _reaction_emoji_for_streak,
    _register_retry_result,
    _remember_last_answer,
    _remove_from_retry,
    _retry_since_last,
    _session_correct_count,
    _session_count,
    _session_effect_id,
    _session_wrong_count,
    _set_retry_since_last,
)
from ..i18n import t
from ..storage import QUIZ_ARTICLE as MODE_ARTICLE
from ..storage import apply_user_word_media, update_form_stats
from ..ui.formatting import (
    _article_matches,
    _format_article_question,
    _format_de_ru_question,
    _format_feedback_article,
    _format_feedback_de_ru,
    _format_feedback_pl_sg,
    _format_feedback_ru_de,
    _format_pl_sg_question,
    _format_ru_question,
    _is_pl_sg_eligible,
)
from ..ui.keyboards import _quiz_idle_markup, _quiz_markup, _tests_menu_keys


def _no_words_key(user_id, due_key):
    """Choose the empty-state message (spec #8): 'no words under the selected topics
    at all' when the topic filter matches zero words in the DB, otherwise the given
    mode-specific 'no due' key (words exist but none are due)."""
    if count_words_for_filter(user_id) == 0:
        return "topic_no_words_quiz"
    return due_key


# --- reactions (spec #9) ---
# Decorative reaction on the USER's answer message (never the bot's own text) —
# FSRS quizzes only (Lang>De/De>Lang/pl>sg/article). Best-effort: a failed
# Bot API call must never break the quiz flow, so failures are swallowed.

async def _set_reaction(context, chat_id, message_id, reaction):
    """Handle set reaction."""
    if not message_id:
        return
    try:
        await context.bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=reaction)
    except Exception:
        pass


async def _apply_answer_reaction(update, context, ok):
    """React to the just-submitted answer message: streak-based emoji on a
    correct answer, thumbs-down on a wrong one."""
    streak = _bump_answer_streak(context, ok)
    emoji = _reaction_emoji_for_streak(streak) if ok else "\U0001F44E"  # 👎
    await _set_reaction(context, update.effective_user.id, update.message.message_id, [emoji])


async def _apply_input_err(update, context, mode):
    """Apply the input_err rollback, then clear the reaction it invalidated."""
    result = apply_fsrs_input_err(context, update.effective_user.id, mode)
    if not result.applied:
        return False
    # spec #9: lift the 👎 reaction from the corrected answer; no new one is set.
    await _set_reaction(context, update.effective_user.id, result.message_id, [])
    return True


# --- session control ---

async def _send_session_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, mode, tail_text=None, reply_markup=None):
    """Send the 'session over' summary (word/correct/wrong counts) with a message
    effect keyed to this session's accuracy (spec #9 part B): a perfect session
    gets confetti, 80-99% gets fire, below 80% gets the poop effect, and no
    answers at all this session gets no effect.

    `tail_text` (e.g. the 'no due words' notice) is folded into THIS message so it
    doesn't arrive as a second message that interrupts the effect animation
    (multiuser bug #5); `reply_markup` is attached to the same message."""
    count = _session_count(context, mode)
    correct = _session_correct_count(context, mode)
    wrong = _session_wrong_count(context, mode)
    text = t(_user_interface_lang(update.effective_user.id), "session_words_count").format(
        count=count, correct=correct, wrong=wrong,
    )
    if tail_text:
        text = f"{text}\n\n{tail_text}"
    await _reply_text_with_retry(
        update.message,
        text,
        message_effect_id=_session_effect_id(context, mode),
        reply_markup=reply_markup,
    )


async def _stop_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, mode):
    """Handle stop with summary."""
    await _send_session_summary(update, context, mode)
    return await _back_to_tests(update, context)


async def _repeat_current_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle repeat current ru de."""
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, prefix_text)
    prompt_text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_RU_DE


async def _repeat_current_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle repeat current de ru."""
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, prefix_text)
    prompt_text = _format_de_ru_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_DE_RU


async def _repeat_current_pl_sg(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Re-show the current pl>sg word after input_err (mirror of _repeat_current_ru_de).

    input_err only corrects the previous answer; it must NOT advance to a new word.
    Sending the prompt instead would pop the next word off the FSRS queue, changing
    the already-shown word. Falls back to the prompt only when there's no current entry.
    """
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_pl_sg_prompt(update, context, prefix_text)
    prompt_text = _format_pl_sg_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"
    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_PL_SG


# --- prompt senders ---

async def _send_ru_de_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send ru de prompt."""
    mode = _mode_l1_de(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode, update.effective_user.id)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        entry = _force_take_retry_entry(context, mode)
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _send_session_summary(
            update, context, mode,
            tail_text=t(_user_interface_lang(update.effective_user.id), _no_words_key(update.effective_user.id, "no_due_words")),
            reply_markup=_quiz_idle_markup(update.effective_user.id),
        )
        return QUIZ_RU_DE

    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    prompt_text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_RU_DE


async def _send_de_ru_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send de ru prompt."""
    mode = _mode_de_l1(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode, update.effective_user.id)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        entry = _force_take_retry_entry(context, mode)
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _send_session_summary(
            update, context, mode,
            tail_text=t(_user_interface_lang(update.effective_user.id), _no_words_key(update.effective_user.id, "no_due_words")),
            reply_markup=_quiz_idle_markup(update.effective_user.id),
        )
        return QUIZ_DE_RU

    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    prompt_text = _format_de_ru_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_DE_RU


async def _send_pl_sg_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send pl sg prompt."""
    mode = _mode_pl_sg(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode, update.effective_user.id)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        attempts = 0
        entry = None
        while attempts < 30:
            attempts += 1
            candidate = _pick_due_entry(context, update.effective_user.id, mode, word_type="nomen")
            if not candidate:
                break
            if _is_pl_sg_eligible(candidate):
                entry = candidate
                break
        if not entry and attempts > 0:
            if prefix_text:
                await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
            await _send_session_summary(
                update, context, mode,
                tail_text=t(_user_interface_lang(update.effective_user.id), _no_words_key(update.effective_user.id, "no_due_pl_sg")),
                reply_markup=_quiz_idle_markup(update.effective_user.id),
            )
            return QUIZ_PL_SG
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _send_session_summary(
            update, context, mode,
            tail_text=t(_user_interface_lang(update.effective_user.id), _no_words_key(update.effective_user.id, "no_due_words")),
            reply_markup=_quiz_idle_markup(update.effective_user.id),
        )
        return QUIZ_PL_SG

    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    prompt_text = _format_pl_sg_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_PL_SG


async def _send_article_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send article prompt."""
    mode = MODE_ARTICLE
    entry = _maybe_take_retry_entry(context, mode, update.effective_user.id)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode, word_type="nomen")
    if not entry:
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(
            update.message,
            t(_user_interface_lang(update.effective_user.id), _no_words_key(update.effective_user.id, "no_due_nouns")),
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return TESTS_MENU

    entry = apply_user_word_media(get_db_path(), update.effective_user.id, entry)  # personal media (P4)
    context.user_data["quiz_entry"] = entry
    prompt_text = _format_article_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await _reply_text_with_retry(
        update.message,
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=True),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE


# --- answer handlers ---

async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the Lang>De quiz (FSRS)."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, _mode_l1_de(update.effective_user.id))
    if action == "input_err":
        applied = await _apply_input_err(update, context, _mode_l1_de(update.effective_user.id))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _repeat_current_ru_de(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        if added:
            # Word is now in active learning (excluded from FSRS due) — drop it from the
            # retry queue and move on to the NEXT word, not the one just sent.
            _remove_from_retry(context, _mode_l1_de(update.effective_user.id), (context.user_data.get("quiz_entry") or {}).get("id"))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "added_to_learning") if added else t(lang, "nothing_to_send")
        return await _send_ru_de_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_RU_DE)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_RU_DE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))
    if text == I_DONT_KNOW_TEXT:
        ok = False
        form_checks = {}
    else:
        ok, form_checks = _check_ru_de_strict(entry, text)
    mode = _mode_l1_de(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok, update.message.message_id)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        if form_checks:
            update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), form_checks)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    _bump_session_result(context, mode, ok, is_retry)
    await _apply_answer_reaction(update, context, ok)

    feedback = _format_feedback_ru_de(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_ru_de_prompt(update, context, feedback)


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the De>Lang quiz."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, _mode_de_l1(update.effective_user.id))
    if action == "input_err":
        applied = await _apply_input_err(update, context, _mode_de_l1(update.effective_user.id))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _repeat_current_de_ru(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        if added:
            _remove_from_retry(context, _mode_de_l1(update.effective_user.id), (context.user_data.get("quiz_entry") or {}).get("id"))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "added_to_learning") if added else t(lang, "nothing_to_send")
        return await _send_de_ru_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_DE_RU)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_DE_RU)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _check_de_l1(entry, text, _user_dict_lang(update.effective_user.id))
    mode = _mode_de_l1(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok, update.message.message_id)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    _bump_session_result(context, mode, ok, is_retry)
    await _apply_answer_reaction(update, context, ok)

    feedback = _format_feedback_de_ru(entry, ok, _user_dict_lang(update.effective_user.id), update.effective_user.id, lang=_user_interface_lang(update.effective_user.id))
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_pl_sg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the plural -> singular quiz (pl>sg)."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, _mode_pl_sg(update.effective_user.id))
    if action == "input_err":
        applied = await _apply_input_err(update, context, _mode_pl_sg(update.effective_user.id))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _repeat_current_pl_sg(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        if added:
            _remove_from_retry(context, _mode_pl_sg(update.effective_user.id), (context.user_data.get("quiz_entry") or {}).get("id"))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "added_to_learning") if added else t(lang, "nothing_to_send")
        return await _send_pl_sg_prompt(update, context, prefix)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PL_SG)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_PL_SG)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_pl_sg_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _check_pl_sg(entry, text)
    mode = _mode_pl_sg(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok, update.message.message_id)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    _bump_session_result(context, mode, ok, is_retry)
    await _apply_answer_reaction(update, context, ok)
    feedback = _format_feedback_pl_sg(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_pl_sg_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..main import _flag_current_word

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, MODE_ARTICLE)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE)
    if _action_key_from_text(text, update.effective_user.id) == "clarify_fields":
        return await _start_clarify_fields(update, context, QUIZ_ARTICLE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        expected = entry.get("article", "")
        ok = _article_matches(text, expected)
    mode = MODE_ARTICLE
    if not is_retry:
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    _bump_session_result(context, mode, ok, is_retry)
    await _apply_answer_reaction(update, context, ok)

    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id), lang=_user_interface_lang(update.effective_user.id))
    return await _send_article_prompt(update, context, feedback)
