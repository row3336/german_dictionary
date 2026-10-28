"""Oral cards: current question with its own spoiler answer; algorithm unchanged.

Synthetic in-memory SQLite and fake sends only (fixture from test_new_logic_oral).
"""

import asyncio
import copy
import itertools
import unittest
from unittest.mock import patch

from tests import test_new_logic_oral as fixture
from bot.app_config import ORAL_QUIZ
from bot.common import quiz_resume
from bot.common.verb_construction import display_infinitive, reflexive_hint
from bot.i18n import t
from bot.oral import domain, handlers as H, view
from bot.oral.service import OralService, accepted_frame
from bot.study import answers
from bot.study.srs import update_fsrs
from bot.ui import formatting as F
import tests.test_reflexive_quizzes as reflexive

LANGS = ('ru', 'uk', 'en')
COLUMN = {'ru': 'russian', 'uk': 'ukrainian', 'en': 'english'}
MODES = [m for lang in LANGS for m in (f'de_{lang}', f'phrase_de_{lang}', f'phrase_{lang}_de')]


def u16(text, offset, length):
    data = text.encode('utf-16-le')
    return data[offset * 2:(offset + length) * 2].decode('utf-16-le')


def spans(text, entities, kind):
    return [u16(text, e[1], e[2]) for e in entities if e[0] == kind]


def as_tuples(entities):
    return [(e.type, e.offset, e.length) for e in entities or []]


class CardHarness(unittest.TestCase):
    def harness(self):
        h = fixture.OralTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        return h

    def put(self, h, wid, kind='nomen', **fields):
        row = {'type': kind, 'german': wid.upper(), 'article': 'der', 'plural': '',
               'russian': 'ru-' + wid, 'ukrainian': 'uk-' + wid, 'english': 'en-' + wid,
               'example': 'Beispiel ' + wid + '.', **fields}
        h.conn.execute('INSERT INTO words(id,' + ','.join(row) + ') VALUES(?,' +
                       ','.join('?' for _ in row) + ')', (wid, *row.values()))
        h.conn.execute("INSERT INTO learning_items(user_id,word_id,status,preview_done) VALUES(7,?,'completed',1)", (wid,))
        h.conn.commit()

    def sent(self, update):
        text, kwargs = update.message.sent[-1]
        return text, as_tuples(kwargs.get('entities')), kwargs


class FirstCardTests(CardHarness):
    def test_all_nine_modes_first_message_is_current_card_with_own_spoiler_answer(self):
        self.assertEqual(len(MODES), 9)
        self.assertEqual(set(MODES), set(domain.MODES))
        for mode in MODES:
            with self.subTest(mode=mode):
                h = self.harness()
                lang = domain.dictionary_language(mode)
                h.options['dict_lang'] = lang
                kind = 'phrase' if mode.startswith('phrase') else 'nomen'
                self.put(h, 'aa', kind, german='Erste Frage' if kind == 'phrase' else 'Apfel')
                self.put(h, 'bb', kind, german='Zweite Reserve' if kind == 'phrase' else 'Birne',
                      **{COLUMN[lang]: 'HIDDEN-B-TRANSLATION'})
                start = h.update()
                h.start(mode, update=start)
                frame = h.frame()
                self.assertEqual((frame['phase'], frame['pending']['word_id'], frame['hidden']['word_id']),
                                 ('grading', 'aa', 'bb'))
                self.assertEqual(len(start.message.sent), 1)
                text, entities, kwargs = self.sent(start)
                self.assertIsNone(kwargs['parse_mode'])
                self.assertNotIn('Markdown', str(kwargs))
                german = 'Erste Frage' if kind == 'phrase' else 'Apfel'
                own = 'ru-aa' if lang == 'ru' else f'{lang}-aa'
                question, answer = (german, own) if mode.startswith(('de_', 'phrase_de_')) else (own, german)
                head, tail = text.split(view.QUESTION_MARK)
                self.assertIn(question, head)
                self.assertNotIn(answer, head)
                hidden = spans(text, entities, 'spoiler')
                self.assertEqual(len(hidden), 1)
                self.assertTrue(hidden[0].startswith(view.ANSWER_PREFIX + answer))
                self.assertTrue(text.endswith(hidden[0]))
                self.assertIn(answer, spans(text, entities, 'bold'))
                for leak in ('Zweite Reserve', 'Birne', 'HIDDEN-B-TRANSLATION', 'BB'):
                    self.assertNotIn(leak, text)
                keyboard = str(kwargs['reply_markup'])
                for key in ('oral_wrong', 'oral_correct', 'clarify_fields', 'stop'):
                    self.assertIn(t('en', key), keyboard)
                self.assertNotIn(t('en', 'oral_next'), keyboard)
                published = h.ctx.user_data['quiz_published_view']
                self.assertEqual((published['current_question_id'], published['visible_answer_id']), ('aa', 'aa'))

    def test_grade_sends_exactly_one_new_message_with_next_card_only(self):
        for correct in (True, False):
            with self.subTest(correct=correct):
                h = self.harness()
                self.put(h, 'aa'); self.put(h, 'bb'); self.put(h, 'cc')
                first = h.update(); h.start(update=first)
                first_text = self.sent(first)[0]
                grade = h.update('oral_correct' if correct else 'oral_wrong')
                h.call(update=grade)
                self.assertEqual(len(grade.message.sent), 1)
                text, entities, _ = self.sent(grade)
                self.assertIn('Bb', text.split(view.QUESTION_MARK)[0])
                self.assertNotIn('ru-aa', text)
                self.assertTrue(spans(text, entities, 'spoiler')[0].startswith('>>> ru-bb'))
                self.assertNotIn('Cc', text)
                self.assertEqual(self.sent(first)[0], first_text)
                self.assertEqual(len(first.message.sent), 1)
                self.assertEqual([(r['word_id'], r['is_correct']) for r in h.rows('quiz_events')],
                                 [('aa', int(correct))])
                published = h.ctx.user_data['quiz_published_view']
                self.assertEqual((published['current_question_id'], published['visible_answer_id']), ('bb', 'bb'))

    def test_old_next_text_is_ignored_and_service_has_no_next_control(self):
        h = self.harness(); self.put(h, 'aa'); self.put(h, 'bb'); h.start()
        before = copy.deepcopy(h.frame())
        old = h.update('oral_next'); h.call(update=old)
        self.assertEqual(old.message.sent, [])
        self.assertEqual(domain.copy_frame(h.frame()), domain.copy_frame(before))
        kinds = {r['action_kind'] for r in h.rows('study_deliveries')}
        self.assertNotIn('oral_next', kinds)
        with self.assertRaisesRegex(ValueError, 'unsupported oral control'):
            h.service.transition(7, h.frame(), 'next', 999, h.options)

    def test_empty_single_last_and_stop_summaries(self):
        h = self.harness()
        empty = h.update(); h.start(update=empty)
        text, entities, _ = self.sent(empty)
        self.assertIn('Accuracy: —', text)
        self.assertEqual(entities, [])
        h = self.harness(); self.put(h, 'aa'); h.start()
        self.assertIsNone(h.frame()['hidden'])
        last = h.update('oral_correct'); h.call(update=last)
        self.assertEqual(h.frame()['phase'], 'completed')
        self.assertIn('Accuracy: 100%', self.sent(last)[0])
        h = self.harness(); self.put(h, 'aa'); self.put(h, 'bb'); h.start()
        stop = h.update('stop'); h.call(update=stop)
        self.assertIn('Accuracy: —', self.sent(stop)[0])
        self.assertEqual(h.rows('quiz_events'), [])


class CardFormatTests(unittest.TestCase):
    def test_short_answer_padded_to_three_wide_blank_lines_and_long_answers_grow_uncut(self):
        entry = {'id': 'x', 'type': 'phrase', 'german': 'Ja', 'russian': 'да'}
        text, entities = view.card(entry, 'phrase_de_ru')
        block = spans(text, entities, 'spoiler')[0].split('\n')
        self.assertEqual(len(block), view.SPOILER_LINES)
        self.assertEqual(block[0], '>>> да' + view.FILLER * (view.SPOILER_WIDTH - len('>>> да')))
        self.assertEqual(block[1:], [view.FILLER * view.SPOILER_WIDTH] * 2)
        self.assertTrue(text.endswith(view.FILLER))
        long_line = 'очень ' * 60
        multi = 'первое значение\nвторое значение\nтретье значение\nчетвёртое значение'
        for translation, lines in ((long_line.strip(), 3), (multi, 4)):
            entry['russian'] = translation
            text, entities = view.card(entry, 'phrase_de_ru')
            block = spans(text, entities, 'spoiler')[0]
            self.assertEqual(len(block.split('\n')), lines)
            self.assertIn(translation, block.replace(view.FILLER, ''))
            self.assertEqual(spans(text, entities, 'bold'), translation.split('\n'))

    def test_layout_question_example_mark_then_answer(self):
        entry = {'id': 'v', 'type': 'nomen', 'article': 'der', 'german': 'Verkäufer',
                 'plural': 'Verkäufer', 'russian': 'продавец',
                 'example': 'Der Verkäufer zeigt mir verschiedene Handys.\nZweites.'}
        text, entities = view.card(entry, 'de_ru')
        lines = text.split('\n')
        self.assertEqual(lines[:7], ['🔵 der Verkäufer', '  pl. die Verkäufer', '',
                                     'Der Verkäufer zeigt mir verschiedene Handys.', '', '>> ❓', ''])
        self.assertTrue(lines[7].startswith('>>> продавец'))
        self.assertNotIn('Zweites.', text)
        self.assertEqual(spans(text, entities, 'italic'), ['pl. die Verkäufer'])
        self.assertEqual(spans(text, entities, 'bold'), ['продавец'])

    def test_unicode_markup_characters_are_literal_with_utf16_entities(self):
        german = 'Ça `va` <b>&amp;</b> *x* _y_ [z](u) 😀'
        entry = {'id': 'u', 'type': 'phrase', 'german': german, 'russian': '😀 привет \\ «x» <i>',
                 'english': 'e', 'example': 'Beispiel *mit* `code` <i>&</i> 🇩🇪', 'image': 'Bild_*'}
        for mode, answer in (('phrase_de_ru', entry['russian']), ('phrase_ru_de', german)):
            with self.subTest(mode=mode):
                text, entities = view.card(entry, mode)
                self.assertIn(german, text)
                self.assertNotIn('\\_', text)
                self.assertNotIn('\\*', text)
                self.assertEqual(spans(text, entities, 'bold'), [answer])
                self.assertTrue(spans(text, entities, 'spoiler')[0].startswith('>>> ' + answer))
                if mode == 'phrase_de_ru':
                    self.assertIn('Beispiel *mit* `code` <i>&</i> 🇩🇪', text)
                    self.assertIn('Bild_*', text)
                total = len(text.encode('utf-16-le')) // 2
                for kind, offset, length in entities:
                    self.assertGreater(length, 0)
                    self.assertLessEqual(offset + length, total)
        with_prefix, entities = view.card(entry, 'phrase_de_ru', prefix='✅ Gespeichert 😀\nzweite Zeile')
        self.assertTrue(with_prefix.startswith('✅ Gespeichert 😀\nzweite Zeile\n\n'))
        self.assertEqual(spans(with_prefix, entities, 'bold'), [entry['russian']])

    def test_forms_hints_case_and_personal_translation(self):
        # Valid reflexive verb shape from the reflexive quiz fixture (module import:
        # its TestCase is not collected here).
        verb = reflexive.entry('v', hilfsverb='hat', my_translation='Мой Перевод',
                               example='Ich FREUE mich.')
        text, entities = view.card(verb, 'de_ru')
        head = text.split(view.QUESTION_MARK)[0]
        self.assertIn(display_infinitive(verb).lower() + reflexive_hint(verb), head)
        self.assertEqual(spans(text, entities, 'italic'), list(F._linked_verb_lines(verb)))
        self.assertIn('Ich FREUE mich.', head)
        self.assertEqual(spans(text, entities, 'bold'), ['Мой Перевод'])
        text, entities = view.card(verb, 'phrase_ru_de')
        self.assertEqual(spans(text, entities, 'bold'), [display_infinitive(verb) + reflexive_hint(verb)])
        self.assertEqual(spans(text, entities, 'italic'), list(F._linked_verb_lines(verb)))
        self.assertIn('Мой Перевод', text.split(view.QUESTION_MARK)[0])
        prep = {'id': 'p', 'type': 'praeposition', 'german': 'MIT', 'case_type': 'dativ',
                'preposition_type': 'modal', 'russian': 'с'}
        self.assertIn('mit (' + ', '.join(F._preposition_hint_parts(prep)) + ')', view.question(prep, 'de_ru'))
        adj = {'id': 'g', 'type': 'adjektiv', 'german': 'gut', 'komparativ': 'besser',
               'superlativ': 'am besten', 'russian': 'хороший'}
        self.assertEqual(spans(*view.card(adj, 'de_ru'), 'italic'), ['besser', 'am besten'])
        noun = {'id': 'n', 'type': 'nomen', 'article': 'die', 'german': 'katze', 'plural': 'katzen', 'russian': 'кошка'}
        self.assertTrue(view.question(noun, 'de_ru').startswith('🔴 die Katze\n  pl. die Katzen'))
        phrase = {'id': 'f', 'type': 'phrase', 'german': 'Das Licht GEHT nicht.', 'russian': 'Свет НЕ горит'}
        self.assertEqual(spans(*view.card(phrase, 'phrase_ru_de'), 'bold'), ['Das Licht GEHT nicht.'])
        self.assertIn('>>    Свет НЕ горит (phrase)', view.question(phrase, 'phrase_ru_de'))

    def test_limit_drops_example_then_filler_width_then_errors_without_cutting(self):
        entry = {'id': 'l', 'type': 'phrase', 'german': 'Kurz', 'russian': 'к', 'example': 'E' * 4090}
        text, entities = view.card(entry, 'phrase_de_ru')
        self.assertNotIn('E' * 100, text)
        self.assertLessEqual(len(text), view.MESSAGE_LIMIT)
        narrowed = raised = 0
        for size in range(4040, 4100):
            entry.update(russian='я' * size, example='')
            try:
                text, entities = view.card(entry, 'phrase_de_ru')
            except view.OralCardTooLong:
                raised += 1
                continue
            self.assertLessEqual(len(text), view.MESSAGE_LIMIT)
            self.assertEqual(spans(text, entities, 'bold'), ['я' * size])
            narrowed += text.endswith('\n' + view.FILLER)
        self.assertGreater(narrowed, 0)
        self.assertGreater(raised, 0)


class HandlerBoundaryTests(CardHarness):
    def test_too_long_answer_is_an_explicit_error_not_a_silent_send(self):
        h = self.harness(); self.put(h, 'aa', 'phrase', russian='я' * 5000)
        h.options['dict_lang'] = 'ru'
        update = h.update()
        with self.assertRaises(view.OralCardTooLong):
            h.start('phrase_de_ru', update=update)
        self.assertEqual(update.message.sent, [])
        self.assertTrue(h.frame()['pending_render'])

    def test_send_failure_replay_resends_the_same_card_without_new_selection(self):
        h = self.harness(); self.put(h, 'aa'); self.put(h, 'bb')
        update = h.update(); update.message.fail_once = True
        with self.assertRaises(RuntimeError):
            h.start(update=update)
        expected = copy.deepcopy(h.frame()['view'])
        with patch.object(fixture.R, 'pick_original', side_effect=AssertionError('second selection')):
            h.start(update=update)
        self.assertEqual(update.message.sent[-1][0], expected[0])
        self.assertEqual(as_tuples(update.message.sent[-1][1]['entities']), expected[1]['entities'])

    def test_clarify_return_and_delete_current_a_never_show_or_target_b(self):
        h = self.harness(); self.put(h, 'aa'); self.put(h, 'bb'); h.start()
        point = quiz_resume.capture_return_point(h.ctx, ORAL_QUIZ)
        self.assertEqual((point.visible_answer_id, point.current_question_id), ('aa', 'aa'))
        back = h.update()
        asyncio.run(quiz_resume.render_existing_frame(back, h.ctx, 'Saved prefix'))
        text, entities, _ = self.sent(back)
        self.assertTrue(text.startswith('Saved prefix\n\n'))
        self.assertEqual(spans(text, entities, 'bold'), ['ru-aa'])
        self.assertNotIn('Bb', text)
        quiz_resume.capture_return_point(h.ctx, ORAL_QUIZ)
        h.conn.execute("DELETE FROM words WHERE id='aa'"); h.conn.commit()
        gone = h.update()
        asyncio.run(quiz_resume.resume_after_delete(gone, h.ctx, 'aa', '✅ Deleted.'))
        text, entities, _ = self.sent(gone)
        self.assertEqual(h.frame()['pending']['word_id'], 'bb')
        self.assertEqual(spans(text, entities, 'bold'), ['ru-bb'])
        self.assertEqual(h.rows('quiz_events'), [])
        # With no reserved B, deleting the only card ends the session.
        h = self.harness(); self.put(h, 'aa'); h.start()
        quiz_resume.capture_return_point(h.ctx, ORAL_QUIZ)
        h.conn.execute("DELETE FROM words WHERE id='aa'"); h.conn.commit()
        gone = h.update()
        asyncio.run(quiz_resume.resume_after_delete(gone, h.ctx, 'aa'))
        self.assertEqual(h.frame()['phase'], 'completed')
        self.assertIn('Accuracy: —', self.sent(gone)[0])

    def test_personal_translation_is_the_spoiler_answer(self):
        h = self.harness(); self.put(h, 'aa'); self.put(h, 'bb')
        h.conn.execute("INSERT INTO user_word_media(user_id,word_id,russian) VALUES(7,'aa','Личный перевод')")
        h.conn.commit()
        with patch('bot.oral.repository.is_owner', return_value=False):
            update = h.update(); h.start(update=update)
        text, entities, _ = self.sent(update)
        self.assertEqual(spans(text, entities, 'bold'), ['Личный перевод'])


class OldNewEquivalenceTests(CardHarness):
    """Oracle: a6c129b behaviour = start without reveal, then an explicit 'next'."""

    def old_start(self, h, mode, transport):
        def resolve(conn):
            pick, get = OralService._selectors(conn, 7, mode, h.options, fixture.DAY)
            return {'oral_frame': domain.start(mode, pick, get, h.options['gap'])}
        return answers.record_control_transition('synthetic', 7, transport, 'oral-entry', 'oral_start', resolve)

    def old_next(self, h, frame, transport):
        source = domain.copy_frame(frame)
        def resolve(conn):
            pick, get = OralService._selectors(conn, 7, source['mode'], h.options, fixture.DAY)
            return {'oral_frame': domain.reveal(source, pick, get, h.options['gap'])}
        return answers.record_control_transition('synthetic', 7, transport, source['flow_id'], 'oral_next', resolve)

    @staticmethod
    def normalized(frame):
        frame = domain.copy_frame(frame)
        frame.pop('flow_id'); frame.pop('revision')
        for name in ('pending', 'hidden', 'last_visible'):
            if frame.get(name):
                frame[name].pop('action_key', None)
        return frame

    def run_path(self, old, gap, limit, outcomes, ids):
        h = self.harness()
        h.options.update(gap=gap, limit=limit)
        for wid in ids:
            self.put(h, wid)
            h.baseline(wid)
        serial = itertools.count(100)
        if old:
            frame = accepted_frame(self.old_start(h, 'de_ru', next(serial)))
            frame = accepted_frame(self.old_next(h, frame, next(serial)))
        else:
            frame = accepted_frame(h.service.start(7, 'de_ru', next(serial), h.options))
        trace = [self.normalized(frame)]
        results = itertools.cycle(outcomes)
        while frame['phase'] == 'grading':
            action = 'correct' if next(results) else 'wrong'
            frame = accepted_frame(h.service.transition(7, frame, action, next(serial), h.options))
            trace.append(self.normalized(frame))
        effects = {
            'progress': [(r['word_id'], r['mode'], r['interval'], r['repetitions'], r['ease'], r['due'])
                         for r in h.conn.execute('SELECT * FROM progress ORDER BY word_id,mode')],
            'events': [(r['word_id'], r['mode'], r['is_correct']) for r in h.rows('quiz_events')],
            'transactions': len(h.rows('transaction_log')),
            'actions': len(h.rows('study_actions')),
            'summary': domain.summary(frame),
        }
        return trace, effects

    def test_old_and_new_paths_have_identical_frames_and_effects(self):
        ids = ('aa', 'bb', 'cc', 'dd', 'ee')
        patterns = ((True,), (False,), (False, True), (True, False, False), (False, False, True, True))
        for gap, limit, outcomes in itertools.product((1, 2, 3), (1, 3), patterns):
            with self.subTest(gap=gap, limit=limit, outcomes=outcomes):
                old_trace, old_effects = self.run_path(True, gap, limit, outcomes, ids)
                new_trace, new_effects = self.run_path(False, gap, limit, outcomes, ids)
                self.assertEqual(new_trace, old_trace)
                self.assertEqual(new_effects, old_effects)
                self.assertGreater(len(old_trace), 5)

    def test_srs_divisor_two_and_undo_unchanged(self):
        h = self.harness(); self.put(h, 'aa'); self.put(h, 'bb'); h.baseline('aa'); h.baseline('bb')
        h.start(); wid = h.frame()['pending']['word_id']; h.grade(True)
        self.assertEqual(h.srs(wid), update_fsrs(fixture.BASE, 5, fixture.DAY, 2))
        self.assertEqual(answers.undo_steps('synthetic', 7, 1, 9000)['restored'], 1)
        self.assertEqual(h.srs(wid), fixture.BASE)
        self.assertEqual(h.rows('quiz_events'), [])


if __name__ == '__main__':
    unittest.main()
