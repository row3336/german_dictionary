"""Quiz handlers (Ru>De, De>Ru, pl>sg, article) + prompt senders, answer check,
FSRS scoring, and input_err correction.

Cross-feature calls that still live in main (_send_entry_to_learning,
_flag_current_word, _start_clarify_fields) are imported locally inside the handlers
to avoid an import cycle; they move here-adjacent as learning/flags/edit are
extracted in later steps.
"""

import re
from functools import wraps

from ..common.quiz_resume import register_renderer, _render_legacy_frame
from ..common.study_lock import study_lock, invalidate_study_frames
from ..written import service as written_service, state as written_state
from ..study.answers import StaleStudyAction

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    FLAG_TEXT,
    I_DONT_KNOW_TEXT,
    LEARNING_QUIZ,
    ORAL_QUIZ,
    QUIZ_ARTICLE,
    QUIZ_DE_RU,
    QUIZ_PL_SG,
    QUIZ_RU_DE,
    TESTS_MENU,
    get_db_path,
    is_owner,
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
from ..storage import QUIZ_ARTICLE as MODE_ARTICLE
from ..storage import (
    _DICT_TRANSLATION_COLUMN,
    get_synonym_group,
    get_word_by_id,
)
from ..text_utils import has_plural_form
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
    if str(mode).startswith("phrase_"):
        return 2.0
    base_mode = str(mode).removeprefix("phrase_")
    if base_mode == _mode_l1_de(user_id):
        return 1.65
    if mode == _mode_pl_sg(user_id):
        return 2.0
    if base_mode == _mode_de_l1(user_id):
        return 2.0
    return 1.0


def _update_srs_for_entry(user_id, entry, mode, ok, *, update, context, state, idk=False):
    """One trusted before snapshot, event, transaction and delivery per original."""
    kind = 'idk' if idk else ('correct' if ok else 'wrong')
    return written_service.original(get_db_path(), update, context, mode, state, entry, kind,
                                    _interval_divisor_for_mode(user_id, mode))


def _check_receipt(context, receipt):
    if receipt.get('invalidate_frame') or receipt.get('state') == 'undone':
        invalidate_study_frames(context)
        raise StaleStudyAction('closed study frame')
    return not receipt.get('replayed')


def _accept_written_answer(update, context, entry, mode, ok, is_retry, state, idk=False):
    uid = update.effective_user.id
    if is_retry:
        receipt = written_service.control(get_db_path(), update, context, mode, state, 'written_retry')
    else:
        receipt = _update_srs_for_entry(uid, entry, mode, ok, update=update,
            context=context, state=state, idk=idk)
    if not _check_receipt(context, receipt):
        return False
    frame = context.user_data[written_state.FRAME]
    if not is_retry:
        frame['last_receipt'] = receipt
        _remember_last_answer(context, entry, mode, ok, update.message.message_id)
        _mark_session_word(context, mode, entry.get('id'))
    context.user_data['quiz_last_answered_entry'] = entry
    _register_retry_result(context, mode, entry.get('id'), ok, is_retry, uid)
    if not is_retry and ok:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode)+1)
    _bump_session_result(context, mode, ok, is_retry)
    frame['advance_transport'] = written_state.transport_id(update)
    return True


async def _apply_input_err(update, context, mode):
    """Replace the whole original effect from its persisted before snapshot."""
    receipt = written_service.correction(get_db_path(), update, context, mode,
        _interval_divisor_for_mode(update.effective_user.id, mode))
    if receipt is None or not _check_receipt(context, receipt):
        return False
    frame = context.user_data[written_state.FRAME]
    frame['last_receipt'] = receipt
    frame['revision'] += 1
    _flip_session_result_to_correct(context, mode)
    _remove_pending_retry(context, mode, receipt['word_id'])
    _set_retry_since_last(context, mode, _retry_since_last(context, mode)+1)
    context.user_data['last_answered_ok'] = True
    written_state.publish(context)
    await _set_reaction(context, update.effective_user.id,
        context.user_data.get('last_answered_message_id'), [])
    return True


_REPLAYED_LEARNING_SEND = object()


async def _learning_send_outcome(update, context, state):
    receipt = context.user_data.get('learning_send_receipt') or {}
    if receipt.get('transport_update_id') != written_state.transport_id(update):
        return None
    if receipt.get('replayed'):
        return _REPLAYED_LEARNING_SEND
    if receipt.get('recovered_undo'):
        invalidate_study_frames(context)
        return await _back_to_main_menu(update, context)
    frame = context.user_data.get(written_state.FRAME)
    if frame:
        frame['advance_transport'] = written_state.transport_id(update)
    return None


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
    frame = context.user_data.get(written_state.FRAME) or {}
    receipt = written_service.control(get_db_path(), update, context, mode,
        frame.get('return_state', QUIZ_RU_DE), 'written_stop', destination=TESTS_MENU)
    if not _check_receipt(context, receipt):
        return TESTS_MENU
    context.user_data.pop(written_state.FRAME, None)
    context.user_data.pop('quiz_published_view', None)
    context.user_data.pop('quiz_return_point', None)
    context.user_data.pop('quiz_entry', None)
    context.user_data.pop('quiz_last_answered_entry', None)
    await _send_session_summary(update, context, mode)
    return await _back_to_tests(update, context)


_WRITTEN_KINDS = {
    QUIZ_RU_DE: ('ru_de', None, 'no_due_words'),
    QUIZ_DE_RU: ('de_ru', None, 'no_due_words'),
    QUIZ_PL_SG: ('pl_sg', 'nomen', 'no_due_pl_sg'),
    QUIZ_ARTICLE: ('article', 'nomen', 'no_due_nouns'),
}


def _written_mode(context, uid, state):
    if state == QUIZ_RU_DE:
        return selected_direction_mode(context, _mode_l1_de(uid))
    if state == QUIZ_DE_RU:
        return selected_direction_mode(context, _mode_de_l1(uid))
    return _mode_pl_sg(uid) if state == QUIZ_PL_SG else MODE_ARTICLE


def _written_question(entry, uid, state):
    if state == QUIZ_RU_DE:
        return _format_ru_question(entry, _user_dict_lang(uid))
    if state == QUIZ_DE_RU:
        return _format_de_ru_question(entry)
    return _format_pl_sg_question(entry) if state == QUIZ_PL_SG else _format_article_question(entry)


async def _deliver_written_view(update, context, frame, messages):
    frame['view'] = messages
    frame['pending_render'] = True
    frame['render_transport'] = written_state.transport_id(update)
    frame['render_revision'] = frame['revision']
    for text, kwargs in messages:
        await _reply_text_with_retry(update.message, text, **kwargs)
    frame['pending_render'] = False
    pending = frame.get('next_view') or {}
    if (pending.get('transport') == written_state.transport_id(update)
            and pending.get('revision') == frame['revision']):
        frame.pop('next_view', None)
    return frame['return_state']


async def _render_current_written(update, context, state, prefix_text=None, tail_text=None):
    uid = update.effective_user.id
    mode = _written_mode(context, uid, state)
    existing = context.user_data.get(written_state.FRAME)
    frame = written_state.ensure_frame(context, mode, state)
    if not existing and context.user_data.get('quiz_entry'):
        frame = written_state.establish_view(context, mode, state, context.user_data['quiz_entry'])
    entry = context.user_data.get('quiz_entry') if frame['phase'] == 'question' else None
    messages = []
    if entry:
        text = _written_question(entry, uid, state)
        if prefix_text:
            text = prefix_text+'\n\n'+text
        messages.append((text, {'reply_markup': _quiz_markup(uid, article=state == QUIZ_ARTICLE),
                                'parse_mode': 'Markdown'}))
    elif state == QUIZ_ARTICLE:
        context.user_data.pop('quiz_entry', None)
        frame['return_state'] = TESTS_MENU
        if prefix_text:
            messages.append((prefix_text, {'parse_mode': 'Markdown'}))
        messages.append((tail_text or t(_user_interface_lang(uid), 'no_due_nouns'),
            {'reply_markup': ReplyKeyboardMarkup(_tests_menu_keys(uid), resize_keyboard=True)}))
    else:
        context.user_data.pop('quiz_entry', None)
        text = t(_user_interface_lang(uid), 'session_words_count').format(
            count=_session_count(context, mode), correct=_session_correct_count(context, mode),
            wrong=_session_wrong_count(context, mode))
        if tail_text:
            text += '\n\n'+tail_text
        if prefix_text:
            messages.append((prefix_text, {'parse_mode': 'Markdown'}))
        messages.append((text, {'reply_markup': _quiz_idle_markup(uid),
                               'message_effect_id': _session_effect_id(context, mode)}))
    written_state.publish(context)
    return await _deliver_written_view(update, context, frame, messages)


async def _repeat_current_ru_de(update, context, prefix_text):
    return await _render_current_written(update, context, QUIZ_RU_DE, prefix_text)


async def _repeat_current_de_ru(update, context, prefix_text):
    return await _render_current_written(update, context, QUIZ_DE_RU, prefix_text)


async def _repeat_current_pl_sg(update, context, prefix_text):
    return await _render_current_written(update, context, QUIZ_PL_SG, prefix_text)


async def _send_written_prompt(update, context, prefix_text, state):
    uid = update.effective_user.id
    mode = _written_mode(context, uid, state)
    frame = written_state.ensure_frame(context, mode, state)
    transport = written_state.transport_id(update)
    pending = frame.get('next_view')
    resuming = (pending and pending['transport'] == transport
                and pending['revision'] == frame['revision'])
    if not resuming:
        if frame.get('advance_transport') != transport:
            receipt = written_service.control(get_db_path(), update, context, mode, state, 'written_enter')
            if not _check_receipt(context, receipt):
                if (frame.get('pending_render') and frame.get('render_transport') == transport
                        and frame.get('render_revision') == frame['revision']):
                    return await _deliver_written_view(update, context, frame, frame['view'])
                return frame['return_state']
        # The accepted delivery owns this unfinished transition before any read
        # or queue mutation. Selection and media enrichment are separate steps:
        # a failed media read must retain the already reserved question.
        pending = {'transport': transport, 'revision': frame['revision'],
                   'prefix': prefix_text, 'selected': False, 'adopted': False}
        frame['next_view'] = pending
    if not pending['selected']:
        entry = _maybe_take_retry_entry(context, mode, uid)
        if not entry:
            context.user_data['quiz_is_retry'] = False
            for _ in range(30 if state == QUIZ_PL_SG else 1):
                candidate = _pick_due_entry(context, uid, mode, word_type=_WRITTEN_KINDS[state][1])
                if not candidate:
                    break
                if state != QUIZ_PL_SG or _is_pl_sg_eligible(candidate):
                    entry = candidate
                    break
        if not entry:
            entry = _force_take_retry_entry(context, mode)
        pending['entry'], pending['selected'] = entry, True
    if not pending['adopted']:
        entry = pending['entry']
        if entry:
            entry = resolve_bot_word_media(get_db_path(), uid, entry)
        written_state.establish_view(context, mode, state, entry)
        pending['revision'], pending['adopted'] = frame['revision'], True
        frame.pop('advance_transport', None)
    tail = None if frame['phase'] == 'question' else t(
        _user_interface_lang(uid), _no_words_key(uid, _WRITTEN_KINDS[state][2]))
    return await _render_current_written(update, context, state, pending['prefix'], tail)


async def _send_ru_de_prompt(update, context, prefix_text):
    return await _send_written_prompt(update, context, prefix_text, QUIZ_RU_DE)


async def _send_de_ru_prompt(update, context, prefix_text):
    return await _send_written_prompt(update, context, prefix_text, QUIZ_DE_RU)


async def _send_pl_sg_prompt(update, context, prefix_text):
    return await _send_written_prompt(update, context, prefix_text, QUIZ_PL_SG)


async def _send_article_prompt(update, context, prefix_text):
    return await _send_written_prompt(update, context, prefix_text, QUIZ_ARTICLE)


# --- answer handlers ---

async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the Lang>De quiz (FSRS)."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    mode = selected_direction_mode(context, _mode_l1_de(update.effective_user.id))
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _stop_with_summary(update, context, mode)
    if action == "stop":
        return await _stop_with_summary(update, context, mode)
    if action == "input_err":
        applied = await _apply_input_err(update, context, mode)
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _repeat_current_ru_de(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        outcome = await _learning_send_outcome(update, context, QUIZ_RU_DE)
        if outcome is _REPLAYED_LEARNING_SEND:
            return None
        if outcome is not None:
            return outcome
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
    if not _accept_written_answer(update, context, entry, mode, ok, is_retry, QUIZ_RU_DE,
                                  idk=text == I_DONT_KNOW_TEXT):
        return await _render_current_written(update, context, QUIZ_RU_DE)

    feedback = _format_feedback_ru_de(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    result = await _send_ru_de_prompt(update, context, feedback)
    await _apply_answer_reaction(update, context, ok)
    return result


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the De>Lang quiz."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    text = update.message.text.strip()
    mode = selected_direction_mode(context, _mode_de_l1(update.effective_user.id))
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _stop_with_summary(update, context, mode)
    if action == "stop":
        return await _stop_with_summary(update, context, mode)
    if action == "input_err":
        applied = await _apply_input_err(update, context, mode)
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _repeat_current_de_ru(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        outcome = await _learning_send_outcome(update, context, QUIZ_DE_RU)
        if outcome is _REPLAYED_LEARNING_SEND:
            return None
        if outcome is not None:
            return outcome
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
    if not _accept_written_answer(update, context, entry, mode, ok, is_retry, QUIZ_DE_RU,
                                  idk=False):
        return await _render_current_written(update, context, QUIZ_DE_RU)

    feedback = _format_feedback_de_ru(entry, ok, _user_dict_lang(update.effective_user.id), update.effective_user.id, lang=_user_interface_lang(update.effective_user.id))
    result = await _send_de_ru_prompt(update, context, feedback)
    await _apply_answer_reaction(update, context, ok)
    return result


async def quiz_pl_sg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an answer in the plural -> singular quiz (pl>sg)."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..handlers.learning import _send_entry_to_learning
    from ..main import _flag_current_word

    mode = _mode_pl_sg(update.effective_user.id)
    text = update.message.text.strip()
    action = _action_key_from_text(text, update.effective_user.id)
    if action == "back":
        return await _stop_with_summary(update, context, mode)
    if action == "stop":
        return await _stop_with_summary(update, context, _mode_pl_sg(update.effective_user.id))
    if action == "input_err":
        applied = await _apply_input_err(update, context, _mode_pl_sg(update.effective_user.id))
        lang = _user_interface_lang(update.effective_user.id)
        prefix = t(lang, "input_err_fixed") if applied else t(lang, "input_err_nothing")
        return await _repeat_current_pl_sg(update, context, prefix)
    if action == "send_learn":
        added = _send_entry_to_learning(update, context, entry_key="quiz_entry")
        outcome = await _learning_send_outcome(update, context, QUIZ_PL_SG)
        if outcome is _REPLAYED_LEARNING_SEND:
            return None
        if outcome is not None:
            return outcome
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
    if not _accept_written_answer(update, context, entry, mode, ok, is_retry, QUIZ_PL_SG,
                                  idk=text == I_DONT_KNOW_TEXT):
        return await _render_current_written(update, context, QUIZ_PL_SG)
    feedback = _format_feedback_pl_sg(entry, ok, lang=_user_interface_lang(update.effective_user.id))
    result = await _send_pl_sg_prompt(update, context, feedback)
    await _apply_answer_reaction(update, context, ok)
    return result


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article."""
    from ..handlers.clarify_fields import _start_clarify_fields
    from ..main import _flag_current_word

    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_with_summary(update, context, MODE_ARTICLE)
    if _action_key_from_text(text, update.effective_user.id) == 'input_err':
        applied = await _apply_input_err(update, context, MODE_ARTICLE)
        return await _render_current_written(update, context, QUIZ_ARTICLE,
            t(_user_interface_lang(update.effective_user.id), 'input_err_fixed' if applied else 'input_err_nothing'))
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
    if not _accept_written_answer(update, context, entry, mode, ok, is_retry, QUIZ_ARTICLE,
                                  idk=text == I_DONT_KNOW_TEXT):
        return await _render_current_written(update, context, QUIZ_ARTICLE)

    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id), lang=_user_interface_lang(update.effective_user.id))
    result = await _send_article_prompt(update, context, feedback)
    await _apply_answer_reaction(update, context, ok)
    return result


def _other_flow_state(context):
    kind = (context.user_data.get('quiz_published_view') or {}).get('caller_kind')
    return {'learning': LEARNING_QUIZ, 'oral': ORAL_QUIZ}.get(kind)


async def _drain_unshown_written(update, context):
    """A new delivery finishes an accepted display before interpreting its text."""
    frame = context.user_data.get(written_state.FRAME)
    if not frame:
        return False, None
    pending = frame.get('next_view') or {}
    advancing = pending.get('revision') == frame['revision']
    rendering = frame.get('pending_render') and frame.get('render_revision') == frame['revision']
    if not advancing and not rendering:
        return False, None
    receipt = written_service.control(get_db_path(), update, context, frame['mode'],
                                      frame['return_state'], 'written_drain_view')
    if not receipt.get('replayed') and receipt.get('invalidate_frame'):
        invalidate_study_frames(context)
        return True, await _back_to_main_menu(update, context)
    if receipt.get('replayed'):
        return True, frame['return_state']
    transport = written_state.transport_id(update)
    if advancing:
        pending['transport'] = transport
    if rendering:
        return True, await _deliver_written_view(update, context, frame, frame['view'])
    return True, await _send_written_prompt(update, context, pending['prefix'], frame['return_state'])


def _serialized_written(state):
    """Own the user lock from delivery lookup through frame adoption and send."""
    def decorate(handler):
        @wraps(handler)
        async def wrapped(update, context):
            async with study_lock(update.effective_user.id):
                receipt = written_service.preflight(
                    get_db_path(), update.effective_user.id, written_state.transport_id(update))
                if receipt:
                    other_state = _other_flow_state(context)
                    if receipt.get('replayed') and other_state is not None:
                        return other_state
                    if not receipt.get('replayed') and receipt.get('invalidate_frame'):
                        invalidate_study_frames(context)
                        return await _back_to_main_menu(update, context)
                    frame = context.user_data.get(written_state.FRAME)
                    if frame:
                        if (frame.get('pending_render')
                                and frame.get('render_transport') == written_state.transport_id(update)
                                and frame.get('render_revision') == frame['revision']):
                            return await _deliver_written_view(update, context, frame, frame['view'])
                        pending = frame.get('next_view') or {}
                        if (pending.get('transport') == written_state.transport_id(update)
                                and pending.get('revision') == frame['revision']):
                            return await _send_written_prompt(update, context, pending['prefix'], frame['return_state'])
                        return frame['return_state']
                    return receipt.get('return_state', TESTS_MENU)
                other_state = _other_flow_state(context)
                if state is not None and other_state is not None:
                    return other_state
                if other_state is None:
                    drained, result = await _drain_unshown_written(update, context)
                    if drained:
                        return result
                try:
                    return await handler(update, context)
                except StaleStudyAction:
                    frame = context.user_data.get(written_state.FRAME)
                    if not frame:
                        return await _back_to_main_menu(update, context)
                    # Bind a rejected delivery too: it must not grade a later B.
                    receipt = written_service.control(get_db_path(), update, context,
                        frame['mode'], frame['return_state'], 'written_stale')
                    _check_receipt(context, receipt)
                    if frame['question_id'] and not get_word_by_id(get_db_path(), frame['question_id']):
                        frame['advance_transport'] = written_state.transport_id(update)
                        frame['question_id'], frame['action_key'] = None, None
                        context.user_data.pop('quiz_entry', None)
                        return await _send_written_prompt(update, context, None, frame['return_state'])
                    return await _render_current_written(update, context, frame['return_state'])
        return wrapped
    return decorate


def serialized_written_entry(handler):
    """Menu adapter: lock/preflight before the menu clears an existing frame."""
    return _serialized_written(None)(handler)


def _matching_written_frame(context, point):
    frame = context.user_data.get(written_state.FRAME)
    if (frame and frame['flow_id'] == point.flow_id and frame['mode'] == point.mode
            and frame['revision'] == point.view_revision):
        return frame
    return None


async def _render_existing_written(update, context, point, prefix_text):
    other_state = _other_flow_state(context)
    if other_state is not None:
        return other_state
    frame = _matching_written_frame(context, point)
    if not frame:
        current = context.user_data.get(written_state.FRAME)
        if current:
            return current['return_state']
        # Pre-adapter in-flight sessions can still use the shared render-only
        # legacy view, but a published oral/Learning flow never enters it.
        published = context.user_data.get('quiz_published_view') or {}
        if not context.user_data.get(written_state.FRAME) and published.get('caller_kind') == 'written':
            return await _render_legacy_frame(update, context, point, prefix_text)
        return await _back_to_tests(update, context)
    if frame['phase'] == 'question':
        entry = get_word_by_id(get_db_path(), frame['question_id'])
        if entry:
            context.user_data['quiz_entry'] = resolve_bot_word_media(get_db_path(), update.effective_user.id, entry)
        else:
            frame['phase'], frame['question_id'], frame['action_key'] = 'completed', None, None
            context.user_data.pop('quiz_entry', None)
    return await _render_current_written(update, context, frame['return_state'], prefix_text)


async def _written_after_delete(update, context, point, word_id, prefix_text):
    frame = _matching_written_frame(context, point)
    if not frame:
        return await _render_existing_written(update, context, point, prefix_text)
    if (frame.get('last_receipt') or {}).get('word_id') == word_id:
        frame['last_receipt'] = None
    if frame['phase'] == 'question' and frame['question_id'] == word_id:
        frame['question_id'], frame['action_key'] = None, None
        return await _send_written_prompt(update, context, prefix_text, frame['return_state'])
    return await _render_current_written(update, context, frame['return_state'], prefix_text)


quiz_ru_de = _serialized_written(QUIZ_RU_DE)(quiz_ru_de)
quiz_de_ru = _serialized_written(QUIZ_DE_RU)(quiz_de_ru)
quiz_pl_sg = _serialized_written(QUIZ_PL_SG)(quiz_pl_sg)
quiz_article = _serialized_written(QUIZ_ARTICLE)(quiz_article)
register_renderer('written', _render_existing_written, _written_after_delete)
