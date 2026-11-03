"""Quiz handlers (Ru>De, De>Ru, pl>sg) + prompt senders, answer check,
FSRS scoring, and input_err correction.

Cross-feature calls that still live in main (_send_entry_to_learning,
_flag_current_word, _start_clarify_fields) are imported locally inside the handlers
to avoid an import cycle; they move here-adjacent as learning/flags/edit are
extracted in later steps.
"""

import re
from copy import deepcopy
from functools import wraps
from types import SimpleNamespace

from ..common.study_lock import study_lock
from ..written import frame as live_frame, service as written
from ..written.logic import interval_divisor

from telegram import Update
from telegram.ext import ContextTypes

from ..app_config import (
    FLAG_TEXT,
    I_DONT_KNOW_TEXT,
    QUIZ_DE_RU,
    QUIZ_PL_SG,
    QUIZ_RU_DE,
    get_db_path,
)
from ..common.bot_media import resolve_bot_word_media
from ..common.lang import (
    _mode_de_l1,
    _mode_l1_de,
    _mode_pl_sg,
    _user_dict_lang,
    _user_interface_lang,
    selected_direction_mode,
)
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _back_to_tests, _reply_text_with_retry
from ..common.word_filter import count_words_for_filter
from ..common.session import (
    _bump_answer_streak,
    _bump_session_result,
    _flip_session_result_to_correct,
    _force_take_retry_entry,
    _mark_session_word,
    _maybe_take_retry_entry,
    _pick_due_entry,
    _reaction_emoji_for_streak,
    _register_retry_result,
    _remove_pending_retry,
    _remove_from_retry,
    _retry_since_last,
    _session_correct_count,
    _session_count,
    _session_effect_id,
    _session_wrong_count,
    _set_retry_since_last,
)
from ..i18n import t
from ..storage import (
    _DICT_TRANSLATION_COLUMN,
    get_synonym_group,
)
from ..text_utils import has_plural_form
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import russian_variants as _russian_variants
from ..text_utils import split_plural_forms
from ..ui.formatting import (
    _article_matches,
    _entry_translation,
    _expected_partizip2,
    _format_de_ru_question,
    _format_feedback_de_ru,
    _format_feedback_pl_sg,
    _format_feedback_ru_de,
    _format_pl_sg_question,
    _format_ru_question,
    _is_pl_sg_eligible,
)
from ..ui.keyboards import _quiz_idle_markup, _quiz_markup


def _no_words_key(user_id, due_key):
    """Choose the empty-state message (spec #8): 'no words under the selected topics
    at all' when the topic filter matches zero words in the DB, otherwise the given
    mode-specific 'no due' key (words exist but none are due)."""
    if count_words_for_filter(user_id) == 0:
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
# FSRS quizzes only (Lang>De/De>Lang/pl>sg). Best-effort: a failed
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
    return interval_divisor(mode)


def _transport_update_id(update):
    from .learning import _transport_update_id as transport_id
    return transport_id(update)


def _written_handler(state):
    """Guard replay and delivery before interpreting any current-card action."""
    def decorate(handler):
        @wraps(handler)
        async def guarded(update, context):
            async with study_lock(update.effective_user.id):
                action = _action_key_from_text(update.message.text.strip(), update.effective_user.id)
                if action in {"stop", "back"}:
                    return await handler(update, context)
                prior = written.find_delivery(get_db_path(), update.effective_user.id,
                                              _transport_update_id(update))
                frame = context.user_data.get("written_frame")
                if prior:
                    receipt = prior.get("receipt") or {}
                    if not frame:
                        return await _back_to_main_menu(update, context)
                    published = context.user_data.get("written_published_actions", set())
                    if (receipt.get("flow_id") == frame.get("flow_id")
                            and receipt.get("action_key") not in published):
                        # A commit survived but its live publication did not.
                        return await _back_to_main_menu(update, context)
                    return state
                if not frame:
                    return await _back_to_main_menu(update, context)
                if frame.get("publication_pending"):
                    return await _back_to_main_menu(update, context)
                if frame.get("send_state") != "confirmed":
                    written.record_ungraded_delivery(get_db_path(), update.effective_user.id,
                                                     _transport_update_id(update), frame)
                    await live_frame.deliver(update, context, _reply_text_with_retry)
                    return state
                if frame.get("consumed"):
                    return await _back_to_main_menu(update, context)
                try:
                    return await handler(update, context)
                except written.StaleWrittenAction:
                    return await _back_to_main_menu(update, context)
        return guarded
    return decorate


def _retry_snapshot(context):
    return {key: deepcopy(context.user_data.get(key, {})) for key in
            ("retry_queue", "retry_counts", "retry_since_last")}


def _apply_written_result(update, context, entry, mode, ok, is_retry, text):
    """Publish live counters only after the complete database action commits."""
    draft = SimpleNamespace(user_data=deepcopy(context.user_data))
    if not is_retry:
        _mark_session_word(draft, mode, entry.get("id"))
    draft.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(draft, mode, entry.get("id"), ok, is_retry, update.effective_user.id)
    if not is_retry and ok:
        _set_retry_since_last(draft, mode, _retry_since_last(draft, mode) + 1)
    _bump_session_result(draft, mode, ok, is_retry)
    kind = "idk" if text == I_DONT_KNOW_TEXT else ("correct" if ok else "wrong")
    args = (get_db_path(), update.effective_user.id, entry["id"], mode, kind,
            context.user_data["written_frame"], _transport_update_id(update))
    if is_retry:
        result = written.apply_retry(*args, _retry_snapshot(context), _retry_snapshot(draft))
    else:
        result = written.apply_original_answer(*args)
    if result["status"] != "applied":
        raise written.StaleWrittenAction("question was already consumed")
    context.user_data["written_frame"]["publication_pending"] = True
    draft.user_data.setdefault("written_published_actions", set()).add(result["receipt"]["action_key"])
    draft.user_data["written_frame"]["consumed"] = True
    if not is_retry:
        receipt = result["receipt"]
        _remember_last_answer(draft, entry, mode, ok, update.message.message_id)
        draft.user_data["last_answered_srs"] = receipt["before"]
        draft.user_data["written_last_original"] = receipt["action_key"]
        draft.user_data["written_frame"]["last_original_key"] = receipt["action_key"]
    context.user_data.update(draft.user_data)


async def _apply_input_err(update, context, mode):
    frame = context.user_data.get("written_frame")
    result = written.correct_last_answer(
        get_db_path(), update.effective_user.id, mode,
        context.user_data.get("written_last_original"), frame, _transport_update_id(update))
    context.user_data["written_correction_status"] = result["status"]
    if result["status"] != "applied":
        return False
    frame["publication_pending"] = True
    word_id = result["receipt"]["word_id"]
    _flip_session_result_to_correct(context, mode)
    _remove_pending_retry(context, mode, word_id)
    _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)
    context.user_data["last_answered_ok"] = True
    if frame.get("retry") and frame.get("word_id") == word_id:
        frame["consumed"] = True
        context.user_data.pop("quiz_entry", None)
    context.user_data.setdefault("written_published_actions", set()).add(result["receipt"]["action_key"])
    frame.pop("publication_pending", None)
    await _set_reaction(context, update.effective_user.id,
                        context.user_data.get("last_answered_message_id"), [])
    return True


def _correction_feedback(context, lang, applied):
    if context.user_data.pop("written_correction_status", None) == "conflict":
        return t(lang, "input_err_conflict")
    return t(lang, "input_err_fixed" if applied else "input_err_nothing")


# --- answer checking (Ru>De strict) ---

def _split_answer_parts(text):
    """Split a multi-field answer (nomen: article/noun/plural, verb: infinitive/
    prateritum/partizip2) into its fields.

    Commas, periods and whitespace are all treated as interchangeable separators:
    users routinely add stray punctuation — a comma or period after a space or
    right before the plural (e.g. "die Bankverbindung, Bankverbindungen") — and
    the answer must still parse into the same fields and validate correctly.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    return [p for p in re.split(r"[,.\s]+", raw) if p]


def _normalize_multi_field_answer(text):
    """Collapse punctuation and whitespace without guessing field boundaries."""
    return _normalize_answer(re.sub(r"[,.\s]+", " ", str(text or "")))


def _stored_form_variants(value):
    """Return normalized alternatives from a comma/slash/semicolon field."""
    return {
        normalized
        for part in re.split(r"[,/;]", str(value or ""))
        if (normalized := _normalize_answer(part))
    }


def _partizip2_variants(entry):
    """Expand stored Partizip II alternatives while preserving an auxiliary."""
    variants = set()
    for part in _stored_form_variants(entry.get("partizip2", "")):
        candidate = dict(entry)
        candidate["partizip2"] = part
        expected = _expected_partizip2(candidate)
        if expected:
            variants.add(expected)
    return variants


def _verb_answer_candidates(entry):
    """Build complete short/full answers without splitting multi-word forms."""
    infinitives = _stored_form_variants(entry.get("infinitive", ""))
    prateritums = _stored_form_variants(entry.get("prateritum", ""))
    partizip2s = _partizip2_variants(entry)
    short = {
        _normalize_multi_field_answer(f"{infinitive} {partizip2}")
        for infinitive in infinitives
        for partizip2 in partizip2s
    }
    full = {
        _normalize_multi_field_answer(f"{infinitive} {prateritum} {partizip2}")
        for infinitive in infinitives
        for prateritum in prateritums
        for partizip2 in partizip2s
    }
    return short, full


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
            checks = {"article": False}
            if has_plural_form(entry.get("plural", "")):
                checks["plural"] = False
            return False, checks
        article_in = _normalize_answer(parts[0])
        german_in = _normalize_answer(parts[1])
        plural_in = _normalize_plural_value(" ".join(parts[2:])) if len(parts) >= 3 else german_in
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "nomen":
                continue
            article_ok = _article_matches(article_in, cand.get("article", ""))
            german_ok = german_in == _normalize_answer(cand.get("german", ""))
            has_plural = has_plural_form(cand.get("plural", ""))
            plural_ok = (
                plural_in in _plural_form_set(cand.get("plural", ""))
                if has_plural
                else len(parts) == 2
            )
            if article_ok and german_ok and plural_ok:
                checks = {"article": True}
                if has_plural:
                    checks["plural"] = True
                return True, checks
        checks = {"article": _article_matches(article_in, entry.get("article", ""))}
        if has_plural_form(entry.get("plural", "")):
            checks["plural"] = plural_in in _plural_form_set(entry.get("plural", ""))
        return False, checks

    if entry_type == "verb":
        answer = _normalize_multi_field_answer(answer_text)
        if not answer:
            return False, {"partizip2": False, "prateritum": False}
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "verb":
                continue
            short, full = _verb_answer_candidates(cand)
            if answer in full:
                return True, {"partizip2": True, "prateritum": True}
            if answer in short:
                return True, {"partizip2": True, "prateritum": False}

        partizip2s = _partizip2_variants(entry)
        prateritums = _stored_form_variants(entry.get("prateritum", ""))
        p2_ok = any(answer == value or answer.endswith(f" {value}") for value in partizip2s)
        pr_ok = any(
            answer == value
            or answer.startswith(f"{value} ")
            or answer.endswith(f" {value}")
            or f" {value} " in answer
            for value in prateritums
        )
        return False, {
            "partizip2": p2_ok,
            "prateritum": pr_ok,
        }

    # single-field answer (adjective/adverb/…). An adjective may be answered with
    # the base form alone, or base+komparativ, or base+komparativ+superlativ — in
    # that order, the extra degrees optional (bug #26). Commas, periods, newlines
    # and stray whitespace are all interchangeable separators, and the superlativ
    # itself carries a space ("am leisesten"), so instead of re-splitting the
    # answer into fields we compare the fully-collapsed answer against the equally
    # collapsed expected prefixes (base / base+komp / base+komp+super).
    def _collapse(value):
        return _normalize_answer(re.sub(r"[,.]+", " ", str(value or "")))

    answer = _collapse(answer_text)
    expected_values = set()
    for cand in synonyms:
        ctype = (cand.get("type") or "").strip().lower()
        if ctype == "verb":
            expected_values.add(_normalize_answer(cand.get("infinitive", "")))
            continue
        forms = [cand.get("german", "")]
        if ctype == "adjektiv":
            forms += [cand.get("komparativ", ""), cand.get("superlativ", "")]
        forms = [f for f in forms if str(f).strip()]
        for n in range(1, len(forms) + 1):
            expected_values.add(_collapse(" ".join(forms[:n])))
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
    live_frame.close(context)
    await _send_session_summary(update, context, mode)
    return await _back_to_tests(update, context)


async def _send_prompt(update, context, prefix_text, direction, keep=False):
    user_id = update.effective_user.id
    mode = (_mode_pl_sg(user_id) if direction == "pl_sg" else
            selected_direction_mode(context, _mode_l1_de(user_id) if direction == "ru_de" else _mode_de_l1(user_id)))
    state = {"ru_de": QUIZ_RU_DE, "de_ru": QUIZ_DE_RU, "pl_sg": QUIZ_PL_SG}[direction]
    entry = context.user_data.get("quiz_entry") if keep else None
    if not entry:
        entry = _maybe_take_retry_entry(context, mode, user_id)
        if not entry:
            context.user_data["quiz_is_retry"] = False
            if direction == "pl_sg":
                for _ in range(30):
                    candidate = _pick_due_entry(context, user_id, mode, word_type="nomen")
                    if not candidate:
                        break
                    if _is_pl_sg_eligible(candidate):
                        entry = candidate
                        break
            else:
                entry = _pick_due_entry(context, user_id, mode)
        if not entry:
            entry = _force_take_retry_entry(context, mode)
    if entry:
        if not keep:
            entry = resolve_bot_word_media(get_db_path(), user_id, entry)
        context.user_data["quiz_entry"] = entry
        text = (_format_ru_question(entry, _user_dict_lang(user_id)) if direction == "ru_de" else
                _format_de_ru_question(entry) if direction == "de_ru" else _format_pl_sg_question(entry))
        kwargs = {"reply_markup": _quiz_markup(user_id), "parse_mode": "Markdown"}
    else:
        context.user_data.pop("quiz_entry", None)
        context.user_data["quiz_is_retry"] = False
        text = t(_user_interface_lang(user_id), "session_words_count").format(
            count=_session_count(context, mode), correct=_session_correct_count(context, mode),
            wrong=_session_wrong_count(context, mode))
        due_key = "no_due_pl_sg" if direction == "pl_sg" else "no_due_words"
        text += "\n\n" + t(_user_interface_lang(user_id), _no_words_key(user_id, due_key))
        # The Markdown answer feedback (prefix_text) shares this message.
        kwargs = {"reply_markup": _quiz_idle_markup(user_id), "parse_mode": "Markdown",
                  "message_effect_id": _session_effect_id(context, mode)}
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    live_frame.prepare(context, user_id, mode, entry, text, kwargs,
                       retry=bool(context.user_data.get("quiz_is_retry")), keep=keep)
    await live_frame.deliver(update, context, _reply_text_with_retry)
    return state


async def _repeat_current_ru_de(update, context, prefix_text):
    if not context.user_data.get("quiz_entry"):
        return await _send_ru_de_prompt(update, context, prefix_text)
    return await _send_prompt(update, context, prefix_text, "ru_de", keep=True)


async def _repeat_current_de_ru(update, context, prefix_text):
    if not context.user_data.get("quiz_entry"):
        return await _send_de_ru_prompt(update, context, prefix_text)
    return await _send_prompt(update, context, prefix_text, "de_ru", keep=True)


async def _repeat_current_pl_sg(update, context, prefix_text):
    if not context.user_data.get("quiz_entry"):
        return await _send_pl_sg_prompt(update, context, prefix_text)
    return await _send_prompt(update, context, prefix_text, "pl_sg", keep=True)


async def _send_ru_de_prompt(update, context, prefix_text):
    return await _send_prompt(update, context, prefix_text, "ru_de")


async def _send_de_ru_prompt(update, context, prefix_text):
    return await _send_prompt(update, context, prefix_text, "de_ru")


async def _send_pl_sg_prompt(update, context, prefix_text):
    return await _send_prompt(update, context, prefix_text, "pl_sg")


# --- answer handlers ---

@_written_handler(QUIZ_RU_DE)
async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the Lang>De quiz (FSRS)."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    mode = selected_direction_mode(context, _mode_l1_de(update.effective_user.id))
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, mode)
    if action == "input_err":
        applied = await _apply_input_err(update, context, mode)
        lang = _user_interface_lang(update.effective_user.id)
        prefix = _correction_feedback(context, lang, applied)
        return await _repeat_current_ru_de(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        if added:
            # Word is now in active learning (excluded from FSRS due) — drop it from the
            # retry queue and move on to the NEXT word, not the one just sent.
            _remove_from_retry(context, mode, (context.user_data.get("quiz_entry") or {}).get("id"))
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
    else:
        ok, _ = _check_ru_de_strict(entry, text)
    _apply_written_result(update, context, entry, mode, ok, is_retry, text)
    await _apply_answer_reaction(update, context, ok)

    feedback = _format_feedback_ru_de(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_ru_de_prompt(update, context, feedback)


@_written_handler(QUIZ_DE_RU)
async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the De>Lang quiz."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    mode = selected_direction_mode(context, _mode_de_l1(update.effective_user.id))
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _back_to_tests(update, context)
    if action == "stop":
        return await _stop_with_summary(update, context, mode)
    if action == "input_err":
        applied = await _apply_input_err(update, context, mode)
        lang = _user_interface_lang(update.effective_user.id)
        prefix = _correction_feedback(context, lang, applied)
        return await _repeat_current_de_ru(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        if added:
            _remove_from_retry(context, mode, (context.user_data.get("quiz_entry") or {}).get("id"))
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
    _apply_written_result(update, context, entry, mode, ok, is_retry, text)
    await _apply_answer_reaction(update, context, ok)

    feedback = _format_feedback_de_ru(entry, ok, _user_dict_lang(update.effective_user.id), update.effective_user.id, lang=_user_interface_lang(update.effective_user.id))
    return await _send_de_ru_prompt(update, context, feedback)


@_written_handler(QUIZ_PL_SG)
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
        prefix = _correction_feedback(context, lang, applied)
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
    _apply_written_result(update, context, entry, mode, ok, is_retry, text)
    await _apply_answer_reaction(update, context, ok)
    feedback = _format_feedback_pl_sg(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    return await _send_pl_sg_prompt(update, context, feedback)
