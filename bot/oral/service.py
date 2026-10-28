"""Atomic oral plans, shared original receipts, and control-only drill receipts."""

from copy import deepcopy
from datetime import date

from ..study import answers, deliveries, repository as study_repo
from ..study.srs import apply_answer
from ..study.generations import frame_generations, assert_generations
from ..learning_requests import frame_lifecycles, assert_lifecycles
from ..common.verb_construction import validate_quiz_entry
from . import domain, repository


def accepted_frame(receipt):
    return receipt.get('oral_frame') or (receipt.get('after') or {}).get('oral_frame')


class OralService:
    def __init__(self, db_path):
        self.db_path = db_path

    def preflight(self, user_id, transport):
        """Delivery/recovery precedes inspection of any current slot or mode."""
        with study_repo.transaction(self.db_path) as conn:
            answers.authorize_user(conn, user_id)
            old = deliveries.lookup_delivery(conn, user_id, transport)
            if old:
                result = old['result']
                if old.get('action_key'):
                    result = answers.lookup_action(conn, user_id, old['action_key']) or result
                return {**result, 'replayed': True}
            recovered = answers.recover_pending_undo(conn, user_id)
            if recovered:
                result = {'invalidate_frame': True, 'state': 'recovered_undo'}
                deliveries.begin_delivery(conn, user_id, transport, 'recovery', 'oral_recovery')
                deliveries.commit_delivery(conn, user_id, transport, result)
                return result
        return None

    @staticmethod
    def _selectors(conn, user_id, mode, options, today):
        options = {**options, 'dict_lang': domain.dictionary_language(mode)}
        pick = lambda excluded, frame: repository.pick_original(conn, user_id, mode, options, today, excluded, frame)
        get = lambda wid: repository.word(conn, user_id, wid, options['dict_lang'])
        return pick, get

    @staticmethod
    def _generations(frame):
        return frame_generations(*[(frame.get(name) or {}).get('entry')
                                  for name in ('pending', 'hidden', 'last_visible')])

    @staticmethod
    def _lifecycles(frame):
        return frame_lifecycles(*[(frame.get(name) or {}).get('entry')
                                for name in ('pending', 'hidden', 'last_visible')])

    def start(self, user_id, mode, transport, options, today=None):
        old = self.preflight(user_id, transport)
        if old:
            return old
        today = today or date.today()
        if mode not in domain.MODES:
            raise ValueError('unsupported oral mode')
        def resolve(conn):
            pick, get = self._selectors(conn, user_id, mode, options, today)
            # The first card is shown with its answer: perform the former 'next'
            # reveal in the same plan, keeping the A/B reservation order intact.
            frame = domain.start(mode, pick, get, options['gap'])
            return {'oral_frame': domain.reveal(frame, pick, get, options['gap'])}
        return answers.record_control_transition(
            self.db_path, user_id, transport, 'oral-entry', 'oral_start', resolve)

    def transition(self, user_id, current, action, transport, options, today=None,
                   now=None, deleted_word_id=None):
        old = self.preflight(user_id, transport)
        if old:
            return old
        if not current:
            raise answers.StaleStudyAction('oral flow is closed')
        today = today or (now.date() if now is not None else date.today())
        source = domain.copy_frame(current)
        mode, flow_id = source['mode'], source['flow_id']
        if mode not in domain.MODES:
            raise ValueError('unsupported oral mode')

        def plan(conn):
            assert_generations(conn, user_id, self._generations(source))
            assert_lifecycles(conn, user_id, self._lifecycles(source))
            for name in ('pending', 'hidden', 'last_visible'):
                item = source.get(name)
                if item:
                    validate_quiz_entry(item['entry'])
            pick, get = self._selectors(conn, user_id, mode, options, today)
            if action == 'stop':
                return domain.stop(source)
            if action == 'delete':
                return domain.delete(source, deleted_word_id, pick, get, options['gap'])
            if action not in ('correct', 'wrong'):
                raise ValueError('unsupported oral control')
            return domain.grade(source, action == 'correct', pick, get,
                                options['gap'], options['limit'])

        pending = source['pending']
        if action in ('correct', 'wrong'):
            if source['phase'] != 'grading' or not pending:
                raise answers.StaleStudyAction('oral question is not revealed')
            if not pending['retry']:
                def apply_and_plan(conn, snapshot, kind, moment):
                    before = study_repo.get_progress(conn, user_id, pending['word_id'], mode, today)
                    after = apply_answer(before, kind, today, interval_divisor=2.0)
                    study_repo.set_progress(conn, user_id, pending['word_id'], mode, after, moment)
                    return {'srs': after, 'oral_frame': plan(conn)}
                return answers.apply_original_answer(
                    self.db_path, user_id, pending['word_id'], mode, action,
                    pending['action_key'], transport, source['revision'], now,
                    flow_id=flow_id, live_revision=lambda: current.get('revision'),
                    interval_divisor=2.0, learning_transition=apply_and_plan, srs_day=today,
                    expected_generation=pending.get('study_generation', 0),
                    expected_generations=self._generations(source),
                    expected_lifecycles=self._lifecycles(source))
        return answers.record_control_transition(self.db_path, user_id, transport,
            flow_id, 'oral_'+action, lambda conn: {'oral_frame': plan(conn)})

    def bind_render(self, user_id, transport, frame):
        """Consume a new reply as pending-view recovery, never as its answer."""
        return answers.record_control_transition(self.db_path, user_id, transport,
            frame['flow_id'], 'oral_drain_view', lambda conn: {
                'flow_id': frame['flow_id'], 'revision': frame['revision']},
            expected_generations=self._generations(frame),
            expected_lifecycles=self._lifecycles(frame))

    def bind_control(self, user_id, transport, frame, action):
        return answers.record_control_transition(self.db_path, user_id, transport,
            frame['flow_id'], 'oral_'+action, lambda conn: {
                'flow_id': frame['flow_id'], 'revision': frame['revision']},
            expected_generations=self._generations(frame),
            expected_lifecycles=self._lifecycles(frame))

    def refresh(self, user_id, frame, dict_lang):
        """Render-only refresh; deleted cached rows cannot be resurrected."""
        result = deepcopy(frame)
        with study_repo.transaction(self.db_path) as conn:
            answers.authorize_user(conn, user_id)
            assert_generations(conn, user_id, self._generations(frame))
            assert_lifecycles(conn, user_id, self._lifecycles(frame))
            for name in ('hidden', 'pending', 'last_visible'):
                item = result.get(name)
                if item:
                    entry = repository.word(conn, user_id, item['word_id'], dict_lang,
                                            selected=item['entry'])
                    if entry:
                        item['entry'] = entry
                    else:
                        result[name] = None
        return result
