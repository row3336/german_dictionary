"""Telegram adapter for the persistent, single-block six-form test."""

import uuid

from ..common import match_policy
from ..common.verb_construction import reflexive_hint

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from ..app_config import CONJUGATION_TEST, CONJUGATION_MENU
from ..common.lang import _mode_l1_de, _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _reply_text_with_retry
from ..common.quiz_resume import publish_view, register_renderer
from ..common.study_lock import study_lock
from ..i18n import t
from .conjugation import FRAME, _service, _transport_id, _new_frame, _block_button, _recovered_undo, _selected_generations


def _live_state(context):
    return CONJUGATION_TEST if (context.user_data.get(FRAME) or {}).get('kind') == 'test' else CONJUGATION_MENU


def _adopt(frame, result):
    if result.get('replayed') or result.get('stale'):
        return False
    previous = frame.get('question')
    current = result.get('question')
    if 'question' in result:
        frame['question'] = current
        if not previous or not current or (previous['pass_id'], previous['pronoun']) != (current['pass_id'], current['pronoun']):
            frame['action_key'] = uuid.uuid4().hex if current else None
    if 'last_answer' in result:
        frame['last_answer'] = result['last_answer']
        pid = result['answered_pass_id']
        if pid not in frame['touched']:
            frame['touched'].append(pid)
    if result.get('fixed'):
        frame['last_answer'] = None
    if 'scheduled' in result:
        frame['scheduled'] = result['scheduled']
    if 'last_block_has_error' in result:
        frame['last_block_has_error'] = result['last_block_has_error']
    frame['next_due'] = result.get('next_due', frame.get('next_due'))
    frame['view_revision'] += 1
    return True


async def _close_stale(update, context):
    context.user_data.pop(FRAME, None)
    await _reply_text_with_retry(update.message, t(_user_interface_lang(update.effective_user.id), 'conjugation_stale'))
    return await _back_to_main_menu(update, context)


def _return_markup(frame, lang):
    """A future scheduled block can also be explicitly sent back to preparation."""
    frame['callbacks'] = {}
    current = frame.get('question')
    scheduled = frame.get('scheduled', [])
    if current:
        scheduled = [b for b in scheduled if b['word_id'] == current['word_id'] and b['tense'] == current['tense']]
    # Page only idle lists; each button still carries a server-owned revision.
    page = frame.get('page', 0) if not current else 0
    start = page * 8
    if start >= len(scheduled):
        page = frame['page'] = 0
        start = 0
    rows = [[_block_button(frame, b, 'reset', lang, include_word=not current)] for b in scheduled[start:start+8]]
    if len(scheduled) > 8:
        token = uuid.uuid4().hex[:16]
        frame['callbacks'][token] = {'operation': 'page', 'page': (page+1) % ((len(scheduled)+7)//8)}
        rows.append([InlineKeyboardButton('›', callback_data='conj:page:'+token)])
    return InlineKeyboardMarkup(rows) if rows else None


async def _render_test_locked(update, context, prefix=None):
    uid = update.effective_user.id
    frame = context.user_data[FRAME]
    lang = _user_interface_lang(uid)
    question = frame.get('question')
    keyboard = ReplyKeyboardMarkup([[t(lang, 'input_err'), t(lang, 'conjugation_stop')]], resize_keyboard=True)
    frame['pending_render'] = True
    # Establish the reply keyboard even though the question has inline reset.
    await _reply_text_with_retry(update.message, prefix or t(lang, 'conjugation_test'), reply_markup=keyboard)
    if question:
        text = t(lang, 'conjugation_form_prompt').format(
            word=question['word'] + reflexive_hint({'type': 'verb', 'reflexive_case': question.get('reflexive_case')}), tense='Präsens' if question['tense'] == 'praesens' else 'Präteritum',
            pronoun=question['pronoun'])
        if question['has_error']:
            text += '\n\n' + t(lang, 'conjugation_block_error')
    else:
        text = t(lang, 'conjugation_test_empty')
        if frame.get('last_block_has_error'):
            text += '\n\n' + t(lang, 'conjugation_block_error')
        if frame.get('next_due'):
            text += '\n' + t(lang, 'conjugation_next_due').format(due=frame['next_due'])
        summary = _service().inspect(uid, frame['pair'], touched=frame['touched'], rules=match_policy.rules(frame))['summary']
        text += '\n' + t(lang, 'conjugation_blocks_summary').format(**summary)
    await _reply_text_with_retry(update.message, text, reply_markup=_return_markup(frame, lang))
    publish_view(context, caller_kind='conjugation', mode=frame['pair'],
                 phase='question' if question else 'waiting', flow_id=frame['flow_id'],
                 current_question_id=question['word_id'] if question else None,
                 visible_answer_id=None, view_revision=frame['view_revision'])
    frame['pending_render'] = False
    return CONJUGATION_TEST


async def _replay_state(update, context, receipt):
    # Retry only an unfinished send for the current frame, never an old receipt's question.
    frame = context.user_data.get(FRAME) or {}
    if (receipt.get('stale') and receipt.get('flow_id') == frame.get('flow_id')
            and receipt.get('action_key') and receipt['action_key'] == frame.get('action_key')):
        return await _close_stale(update, context)
    if frame.get('kind') == 'test' and frame.get('pending_render') and receipt.get('flow_id') == frame.get('flow_id'):
        return await _render_test_locked(update, context)
    return _live_state(context)


async def _start_test_locked(update, context):
    uid = update.effective_user.id
    pair = _mode_l1_de(uid)
    frame = _new_frame(pair, 'test')
    result = _service().start_test(uid, pair, _transport_id(update), frame['flow_id'])
    if result.get('replayed'):
        return await _replay_state(update, context, result)
    if _recovered_undo(context, result) or result.get('stale'):
        context.user_data.pop(FRAME, None)
        return await _back_to_main_menu(update, context)
    frame[match_policy.FRAME_KEY] = result.get(match_policy.FRAME_KEY, {})
    context.user_data[FRAME] = frame
    _adopt(frame, result)
    return await _render_test_locked(update, context, t(_user_interface_lang(uid), 'conjugation_test'))


async def start_conjugation_test(update, context):
    async with study_lock(update.effective_user.id):
        return await _start_test_locked(update, context)


async def conjugation_test_input(update, context):
    async with study_lock(update.effective_user.id):
        uid = update.effective_user.id
        frame = context.user_data.get(FRAME)
        if not frame or frame.get('kind') != 'test' or frame['pair'] != _mode_l1_de(uid):
            context.user_data.pop(FRAME, None)
            return await _back_to_main_menu(update, context)
        lang = _user_interface_lang(uid)
        text = (update.message.text or '').strip()
        action = _action_key_from_text(text, uid)
        if text == t(lang, 'conjugation_stop') or action in ('stop', 'back'):
            result = _service().stop(uid, frame['pair'], frame['flow_id'], _transport_id(update), frame['touched'],
                                     expected_generations=_selected_generations(frame))
            if result.get('replayed'):
                return await _replay_state(update, context, result)
            _recovered_undo(context, result)
            context.user_data.pop(FRAME, None)
            if not result.get('stale'):
                await _reply_text_with_retry(update.message, t(lang, 'conjugation_blocks_summary').format(**result['summary']))
            return await _back_to_main_menu(update, context)
        if action == 'input_err':
            last = frame.get('last_answer') or {}
            result = _service().correct_last(
                uid, frame['pair'], last.get('id'), frame['flow_id'], _transport_id(update),
                current_question=frame.get('question'), rules=match_policy.rules(frame))
            if result.get('replayed'):
                return await _replay_state(update, context, result)
            if _recovered_undo(context, result):
                context.user_data.pop(FRAME, None)
                return await _back_to_main_menu(update, context)
            if result.get('stale'):
                return await _close_stale(update, context)
            _adopt(frame, result)
            return await _render_test_locked(update, context,
                t(lang, 'input_err_fixed' if result.get('fixed') else 'input_err_nothing'))
        question = frame.get('question')
        if not question:
            return await _render_test_locked(update, context)
        result = _service().submit_form(
            uid, frame['pair'], question['pass_id'], question['revision'], question['pronoun'],
            text, frame['action_key'], _transport_id(update), frame['flow_id'], rules=match_policy.rules(frame))
        if result.get('replayed'):
            return await _replay_state(update, context, result)
        if _recovered_undo(context, result):
            context.user_data.pop(FRAME, None)
            return await _back_to_main_menu(update, context)
        if result.get('stale'):
            return await _close_stale(update, context)
        _adopt(frame, result)
        if result.get('skipped'):
            return await _render_test_locked(update, context, t(lang, 'conjugation_stale'))
        prefix = t(lang, 'answer_correct' if result['ok'] else 'answer_wrong')
        if not result['ok']:
            prefix += ' · ' + result['expected']
        return await _render_test_locked(update, context, prefix)


async def _render_existing(update, context, point, prefix_text):
    frame = context.user_data.get(FRAME)
    if not frame or frame.get('kind') != 'test' or frame['flow_id'] != point.flow_id or frame['view_revision'] != point.view_revision:
        return await _back_to_main_menu(update, context)
    view = _service().inspect(update.effective_user.id, frame['pair'], frame.get('question'), frame['touched'], rules=match_policy.rules(frame))
    if frame.get('question') and 'question' not in view:
        return await _close_stale(update, context)
    return await _render_test_locked(update, context, prefix_text)


register_renderer('conjugation', _render_existing)
