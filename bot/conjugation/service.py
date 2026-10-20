"""Atomic conjugation commands; Telegram and live frame locks stay outside SQL."""

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from ..storage import _connect
from ..study.srs import default_srs
from . import repository as repo
from .domain import PAIRS, StaleAction, check_form, reduce_pass


class ConjugationService:
    def __init__(self, db_path, connect=None):
        self.db_path = db_path
        self.connect = connect or _connect

    def _command(self, user_id, pair, transport_id, flow_id, kind, action_key, command):
        from ..study.answers import authorize_user, recover_pending_undo
        from ..study.deliveries import begin_delivery, commit_delivery, lookup_delivery

        with self.connect(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            authorize_user(conn, user_id)
            old = lookup_delivery(conn, user_id, transport_id)
            if old:
                result = dict(old.get('result') or json.loads(old.get('result_json') or '{}'))
                result['replayed'] = True
                return result
            recovered = recover_pending_undo(conn, user_id)
            if pair not in PAIRS:
                raise StaleAction('invalid pair')
            begin_delivery(conn, user_id, transport_id, flow_id, kind, action_key=action_key)
            # A second transport of an already accepted form must not be rebound
            # to the newly displayed pronoun, even if its old revision is stale.
            original = conn.execute(
                'SELECT id,pass_id FROM conjugation_answers WHERE user_id=? AND action_key=?',
                (user_id, action_key),
            ).fetchone() if kind == 'conjugation_answer' else None
            if recovered:
                result = {'stale': True, 'recovered_undo': True}
            elif original:
                result = {'replayed': True, 'answer_id': original['id'], 'pass_id': original['pass_id']}
            else:
                try:
                    result = command(conn)
                except StaleAction:
                    result = {'stale': True}
            result.update({'flow_id': flow_id, 'pair': pair})
            commit_delivery(conn, user_id, transport_id, result, action_key=action_key)
            return result

    def preparation(self, user_id, pair, transport_id, flow_id, *, viewed_id=None):
        def command(conn):
            repo.sync_blocks(conn, user_id, pair)
            if viewed_id and any(b['word_id'] == viewed_id and b['phase'] == 'preparation'
                                 for b in repo.blocks(conn, user_id, pair)):
                repo.record_view(conn, user_id, viewed_id)
            return {'preparation': repo.preparation(conn, user_id, pair),
                    'scheduled': [b for b in repo.blocks(conn, user_id, pair) if b['phase'] == 'test']}
        return self._command(user_id, pair, transport_id, flow_id, 'conjugation_next', None, command)

    def transition(self, user_id, pair, word_id, tense, revision, cycle, operation,
                   transport_id, flow_id, *, today=None, now=None):
        today, stamp = self._clock(today, now)
        def command(conn):
            current = repo.block(conn, user_id, pair, word_id, tense)
            if not current or current['revision'] != revision or current['cycle'] != cycle:
                raise StaleAction('stale preparation')
            key = repo.block_key(current)
            if operation == 'ready' and current['phase'] == 'preparation':
                initial = default_srs(today=today)
                conn.execute(
                    """UPDATE conjugation_blocks SET phase='test',interval=?,repetitions=?,ease=?,
                       due=?,prepared_at=?,revision=revision+1
                       WHERE user_id=? AND pair=? AND word_id=? AND tense=?""",
                    (initial['interval'], initial['repetitions'], initial['ease'],
                     (today+timedelta(days=1)).isoformat(), stamp, *key),
                )
            elif operation == 'reset' and current['phase'] == 'test':
                # Returning to preparation closes this whole live test session,
                # including a last error in A while current question belongs to B.
                conn.execute(
                    'UPDATE conjugation_answers SET correction_session_id=NULL WHERE user_id=? AND correction_session_id=?',
                    (user_id, flow_id),
                )
                conn.execute(
                    """UPDATE conjugation_answers SET correction_session_id=NULL WHERE pass_id IN
                       (SELECT id FROM conjugation_passes WHERE user_id=? AND pair=? AND word_id=? AND tense=?)""", key,
                )
                conn.execute(
                    """UPDATE conjugation_passes SET status='reset',revision=revision+1
                       WHERE user_id=? AND pair=? AND word_id=? AND tense=? AND status='active'""", key,
                )
                initial = default_srs(today=today)
                conn.execute(
                    """UPDATE conjugation_blocks SET phase='preparation',cycle=cycle+1,interval=?,
                       repetitions=?,ease=?,due=NULL,prepared_at=NULL,revision=revision+1
                       WHERE user_id=? AND pair=? AND word_id=? AND tense=?""",
                    (initial['interval'], initial['repetitions'], initial['ease'], *key),
                )
            else:
                raise StaleAction('invalid transition')
            return {'block': repo.block(conn, *key), 'preparation': repo.preparation(conn, user_id, pair),
                    'scheduled': [b for b in repo.blocks(conn, user_id, pair) if b['phase'] == 'test']}
        return self._command(user_id, pair, transport_id, flow_id, 'conjugation_'+operation, None, command)

    def start_test(self, user_id, pair, transport_id, flow_id, *, today=None, now=None):
        today, stamp = self._clock(today, now)
        def command(conn):
            repo.sync_blocks(conn, user_id, pair)
            # A new test session never revives correction rights from an earlier
            # process/session, but saved passes and answers remain resumable.
            conn.execute('UPDATE conjugation_answers SET correction_session_id=NULL WHERE user_id=?', (user_id,))
            return self._test_view(conn, user_id, pair, today, stamp)
        return self._command(user_id, pair, transport_id, flow_id, 'conjugation_enter', None, command)

    def submit_form(self, user_id, pair, pass_id, revision, pronoun, text, action_key,
                    transport_id, session_id, *, today=None, now=None):
        today, stamp = self._clock(today, now)
        def command(conn):
            try:
                current = repo.assert_pass(conn, user_id, pair, pass_id, revision)
            except StaleAction:
                # Explicit deletion/reclassification may invalidate the current
                # word. Keep the history and advance only to an eligible rest.
                old = repo.pass_row(conn, user_id, pass_id)
                if old and old['pair'] == pair:
                    entry = repo.word(conn, old['word_id'])
                    if not repo.eligible_word(entry):
                        return {**self._test_view(conn, user_id, pair, today, stamp), 'skipped': True}
                raise
            prompt = repo.question(conn, current)
            if not prompt or prompt['pronoun'] != pronoun:
                raise StaleAction('stale pronoun')
            expected = json.loads(current['expected_forms_json'])[pronoun]
            correct = check_form(text, pronoun, expected)
            cursor = conn.execute(
                """INSERT INTO conjugation_answers(user_id,pass_id,pronoun_group,ordinal,answer_text,
                   is_correct,original_text,original_correct,answered_at,answer_day,correction_session_id,action_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, pass_id, pronoun, prompt['ordinal'], str(text), int(correct), str(text),
                 int(correct), stamp, today.isoformat(), session_id, action_key),
            )
            self._reduce(conn, current, today, stamp)
            view = self._test_view(conn, user_id, pair, today, stamp)
            view.update({'ok': correct, 'expected': expected, 'answered_pass_id': pass_id,
                         'last_block_has_error': any(not a['is_correct'] for a in repo.answers(conn, pass_id)),
                         'last_answer': {'id': cursor.lastrowid, 'pass_id': pass_id, 'pair': pair,
                                         'action_key': action_key, 'session_id': session_id,
                                         'answer_day': today.isoformat(), 'ok': correct}})
            return view
        return self._command(user_id, pair, transport_id, session_id, 'conjugation_answer', action_key, command)

    def correct_last(self, user_id, pair, answer_id, session_id, transport_id,
                     *, current_question=None, today=None, now=None):
        today, stamp = self._clock(today, now)
        def command(conn):
            last = repo.asdict(conn.execute(
                """SELECT * FROM conjugation_answers WHERE user_id=? AND correction_session_id=?
                   ORDER BY id DESC LIMIT 1""", (user_id, session_id),
            ).fetchone())
            if not last or last['id'] != answer_id or last['is_correct'] or last['corrected'] or last['answer_day'] != today.isoformat():
                return {'fixed': False}
            current = repo.pass_row(conn, user_id, last['pass_id'])
            if not current or current['pair'] != pair or current['status'] == 'reset':
                raise StaleAction('old correction')
            b = repo.block(conn, user_id, pair, current['word_id'], current['tense'], valid=False)
            if not b or not repo.eligible_word(repo.word(conn, current['word_id'])) or b['cycle'] != current['cycle'] or b['phase'] != 'test':
                raise StaleAction('ineligible correction')
            expected = json.loads(current['expected_forms_json'])[last['pronoun_group']]
            conn.execute('UPDATE conjugation_answers SET is_correct=1,corrected=1,answer_text=? WHERE id=?', (expected, answer_id))
            self._reduce(conn, current, today, stamp)
            # Preserve the separately selected next question. Never choose again
            # because the last form of the previous block was corrected.
            question = current_question
            if question and question['pass_id'] == current['id']:
                question = repo.question(conn, repo.pass_row(conn, user_id, current['id']))
            elif question and not repo.eligible_word(repo.word(conn, question['word_id'])):
                question = self._test_view(conn, user_id, pair, today, stamp)['question']
            scheduled = [b for b in repo.blocks(conn, user_id, pair) if b['phase'] == 'test']
            return {'fixed': True, 'question': question, 'corrected_pass_id': current['id'],
                    'answer_id': answer_id, 'parent_action_key': last['action_key'],
                    'scheduled': scheduled, 'next_due': min((b['due'] for b in scheduled), default=None),
                    'last_block_has_error': any(not a['is_correct'] for a in repo.answers(conn, current['id']))}
        return self._command(user_id, pair, transport_id, session_id, 'conjugation_correction',
                             'conjugation-correction:'+str(answer_id), command)

    def stop(self, user_id, pair, session_id, transport_id, touched=(), *, today=None):
        def command(conn):
            conn.execute(
                'UPDATE conjugation_answers SET correction_session_id=NULL WHERE user_id=? AND correction_session_id=?',
                (user_id, session_id),
            )
            return {'summary': repo.summary(conn, user_id, touched), 'closed': True}
        return self._command(user_id, pair, transport_id, session_id, 'conjugation_stop', None, command)

    def inspect(self, user_id, pair, question=None, touched=()):
        """Read-only render state; deliberately no picker, counters or grade."""
        with self.connect(self.db_path) as conn:
            output = {'summary': repo.summary(conn, user_id, touched)}
            if question:
                try:
                    current = repo.assert_pass(conn, user_id, pair, question['pass_id'], question['revision'])
                except StaleAction:
                    return output
                output['question'] = repo.question(conn, current)
            return output

    def control(self, user_id, pair, transport_id, flow_id, kind, resolve):
        """Persist a UI-only transition after delivery lookup, without a grade."""
        return self._command(user_id, pair, transport_id, flow_id, kind, None, lambda conn: resolve())

    @staticmethod
    def _clock(today, now):
        # Same calendar source as the existing SRS, not a newly chosen timezone.
        return today or date.today(), (now or datetime.now(timezone.utc)).isoformat()

    @staticmethod
    def _reduce(conn, current, today, stamp):
        answers = repo.answers(conn, current['id'])
        state, result, result_day = reduce_pass(
            json.loads(current['baseline_srs_json']), answers,
            current['applied_result'], current['result_day'], today,
        )
        conn.execute(
            """UPDATE conjugation_blocks SET interval=?,repetitions=?,ease=?,due=?,revision=revision+1
               WHERE user_id=? AND pair=? AND word_id=? AND tense=? AND cycle=?""",
            (state['interval'], state['repetitions'], state['ease'], state['due'],
             current['user_id'], current['pair'], current['word_id'], current['tense'], current['cycle']),
        )
        completed = len(answers) == 6
        conn.execute(
            """UPDATE conjugation_passes SET applied_result=?,result_day=?,revision=revision+1,
               status=?,completed_at=? WHERE id=?""",
            (result, result_day, 'completed' if completed else 'active', stamp if completed else None, current['id']),
        )

    @staticmethod
    def _test_view(conn, user_id, pair, today, stamp):
        current = repo.select_pass(conn, user_id, pair, today, stamp)
        scheduled = [b for b in repo.blocks(conn, user_id, pair) if b['phase'] == 'test']
        return {'question': repo.question(conn, current), 'scheduled': scheduled,
                'next_due': min((b['due'] for b in scheduled), default=None)}
