"""Quiz handlers (Ru>De, De>Ru, pl>sg, article) + prompt senders, answer check,
FSRS scoring, and input_err correction.

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
from ..common.settings import _topic_filter_for_query
from ..common.session import (
    _bump_answer_streak,
    _bump_session_result,
    _force_take_retry_entry,
    _mark_session_word,
    _maybe_take_retry_entry,
    _pick_due_entry,
    _reaction_emoji_for_streak,
    _register_retry_result,
    _remove_from_retry,
    _retry_since_last,
    _session_correct_count,
    _session_count,
    _session_effect_id,
    _session_wrong_count,
    _set_retry_since_last,
)
from ..common.txlog import _capture_transaction_snapshot, _log_transaction
from ..i18n import t
from ..storage import QUIZ_ARTICLE as MODE_ARTICLE
from ..storage import QUIZ_RU_DE as MODE_RU_DE
from ..storage import (
    _DICT_TRANSLATION_COLUMN,
    apply_user_word_media,
    count_words_for_topics,
    get_synonym_group,
    log_quiz_event,
    set_progress,
    update_form_stats,
    update_progress,
)
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import russian_variants as _russian_variants
from ..text_utils import split_plural_forms
from ..ui.formatting import (
    _article_matches,
    _entry_translation,
    _expected_partizip2,
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
    if count_words_for_topics(get_db_path(), _topic_filter_for_query(user_id)) == 0:
        return "topic_no_words_quiz"
    return due_key


# --- answer-state + scoring helpers ---

def _remember_last_answer(context, entry, mode, ok, message_id=None):
    """Handle remember last answer."""
    context.user_data["last_answered_entry"] = entry
    context.user_data["last_answered_mode"] = mode
    context.user_data["last_answered_ok"] = bool(ok)
    context.user_data["last_answered_srs"] = dict(entry.get("srs") or {})
    context.user_data["last_answered_message_id"] = message_id


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


def _interval_divisor_for_mode(user_id, mode):
    """Handle interval divisor for mode."""
    if mode == MODE_RU_DE:
        return 1.65
    if mode == _mode_pl_sg(user_id):
        return 2.0
    if mode == _mode_de_l1(user_id):
        return 2.0
    return 1.0


def _update_srs_for_entry(user_id, entry, mode, ok):
    """Handle update srs for entry."""
    snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), [mode])
    quality = 5 if ok else 2
    update_progress(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        mode=mode,
        quality=quality,
        interval_divisor=_interval_divisor_for_mode(user_id, mode),
    )
    log_quiz_event(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, is_correct=ok)
    _log_transaction(user_id, f"quiz:{mode}", snapshot)


async def _apply_input_err(update, context, mode):
    """Handle apply input error correction."""
    last_entry = context.user_data.get("last_answered_entry")
    last_mode = context.user_data.get("last_answered_mode")
    last_ok = context.user_data.get("last_answered_ok")
    last_srs = context.user_data.get("last_answered_srs")
    last_message_id = context.user_data.get("last_answered_message_id")
    if not last_entry or last_mode != mode or last_ok is None:
        return False
    if last_ok:
        return False
    if not last_srs:
        return False
    snapshot = _capture_transaction_snapshot(update.effective_user.id, last_entry.get("id"), [mode])
    set_progress(get_db_path(), update.effective_user.id, last_entry.get("id"), mode, last_srs)
    update_progress(
        get_db_path(),
        update.effective_user.id,
        last_entry.get("id"),
        mode,
        quality=5,
        interval_divisor=_interval_divisor_for_mode(update.effective_user.id, mode),
    )
    log_quiz_event(get_db_path(), user_id=update.effective_user.id, word_id=last_entry.get("id"), mode=mode, is_correct=True)
    _log_transaction(update.effective_user.id, f"input_err:{mode}", snapshot)
    _remove_from_retry(context, mode, last_entry.get("id"))
    _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    context.user_data["last_answered_ok"] = True
    # spec #9: lift the 👎 reaction from the corrected answer; no new one is set.
    await _set_reaction(context, update.effective_user.id, last_message_id, [])
    return True


# --- answer checking (Ru>De strict) ---

def _split_answer_parts(text):
    """Handle split answer parts."""
    raw = str(text or "").strip()
    if not raw:
        return []
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [p.strip() for p in raw.split() if p.strip()]


def _normalize_plural_value(value):
    """Handle normalize plural value."""
    token = _normalize_answer(value)
    if token.startswith("die "):
        token = token[4:].strip()
    return token


def _plural_form_set(stored):
    """Normalized set of accepted plural forms (a noun may have several)."""
    forms = {_normalize_plural_value(p) for p in split_plural_forms(stored)}
    forms = {f for f in forms if f}
    return forms or {_normalize_plural_value(stored)}


def _plural_matches(answer, stored):
    """True if the normalized answer equals any of the stored plural forms."""
    return _normalize_plural_value(answer) in _plural_form_set(stored)


def _synonym_entries(entry):
    """Handle synonym entries."""
    wid = entry.get("id")
    if not wid:
        return [entry]
    group = get_synonym_group(get_db_path(), wid)
    if not group:
        return [entry]
    return group


def _check_ru_de_strict(entry, answer_text):
    """Handle check ru de strict."""
    entry_type = (entry.get("type") or "").strip().lower()
    parts = _split_answer_parts(answer_text)
    synonyms = _synonym_entries(entry)

    if entry_type == "nomen":
        if len(parts) < 2:
            return False, {"article": False, "plural": False}
        article_in = _normalize_answer(parts[0])
        german_in = _normalize_answer(parts[1])
        plural_in = _normalize_plural_value(" ".join(parts[2:])) if len(parts) >= 3 else german_in
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "nomen":
                continue
            article_ok = _article_matches(article_in, cand.get("article", ""))
            german_ok = german_in == _normalize_answer(cand.get("german", ""))
            plural_ok = plural_in in _plural_form_set(cand.get("plural", ""))
            if article_ok and german_ok and plural_ok:
                return True, {"article": True, "plural": True}
        return False, {
            "article": _article_matches(article_in, entry.get("article", "")),
            "plural": plural_in in _plural_form_set(entry.get("plural", "")),
        }

    if entry_type == "verb":
        if len(parts) < 2:
            return False, {"partizip2": False, "prateritum": False}
        infinitive_in = _normalize_answer(parts[0])
        prateritum_in = ""
        prateritum_provided = False
        if len(parts) == 2:
            p2_in = _normalize_answer(parts[1])
        else:
            prateritum_in = _normalize_answer(parts[1])
            prateritum_provided = True
            p2_in = _normalize_answer(" ".join(parts[2:]))
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "verb":
                continue
            inf_ok = infinitive_in == _normalize_answer(cand.get("infinitive", ""))
            p2_ok = p2_in == _expected_partizip2(cand)
            pr_ok = prateritum_provided and prateritum_in == _normalize_answer(cand.get("prateritum", ""))
            if inf_ok and p2_ok:
                return True, {"partizip2": True, "prateritum": pr_ok}
        return False, {
            "partizip2": p2_in == _expected_partizip2(entry),
            "prateritum": prateritum_provided and prateritum_in == _normalize_answer(entry.get("prateritum", "")),
        }

    answer = _normalize_answer(answer_text)
    expected_values = []
    for cand in synonyms:
        if (cand.get("type") or "").strip().lower() == "verb":
            expected_values.append(_normalize_answer(cand.get("infinitive", "")))
        else:
            expected_values.append(_normalize_answer(cand.get("german", "")))
    return answer in expected_values, {}


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
    _bump_session_result(context, mode, ok)
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
        dict_lang = _user_dict_lang(update.effective_user.id)
        answer_variants = set(_russian_variants(text))
        variants = set(_russian_variants(_entry_translation(entry, dict_lang)))
        # spec #7: with a personal translation override, _entry_translation returns
        # the personal one — but the base (shared) translation stays semantically
        # correct, so accept BOTH (owner's decision, fork 1).
        if str(entry.get("my_translation", "") or "").strip():
            base_field = _DICT_TRANSLATION_COLUMN.get(dict_lang, "russian")
            variants |= set(_russian_variants(entry.get(base_field, "")))
        ok = bool(answer_variants & variants)
    mode = _mode_de_l1(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok, update.message.message_id)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    _bump_session_result(context, mode, ok)
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
        got = _normalize_answer(text)
        singular = _normalize_answer(entry.get("german", ""))
        plural = _normalize_answer(entry.get("plural", ""))
        article_ok = False
        german_ok = False
        if len(got.split()) >= 2:
            got_article = got.split()[0]
            got_word = " ".join(got.split()[1:])
            article_ok = _article_matches(got_article, entry.get("article", ""))
            german_ok = got_word == singular
            ok = article_ok and german_ok
        else:
            # If singular and plural are identical, allow answer with article only.
            ok = singular == plural and _article_matches(got, entry.get("article", ""))
    mode = _mode_pl_sg(update.effective_user.id)
    if not is_retry:
        _remember_last_answer(context, entry, mode, ok, update.message.message_id)
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        _mark_session_word(context, mode, entry.get("id"))
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    _bump_session_result(context, mode, ok)
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
    _bump_session_result(context, mode, ok)
    await _apply_answer_reaction(update, context, ok)

    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id), lang=_user_interface_lang(update.effective_user.id))
    return await _send_article_prompt(update, context, feedback)
