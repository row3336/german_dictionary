"""Independent K05 contracts: synthetic SQLite, fake sends, no live integrations.

The author fixture supplies only schema/clock/transport doubles; assertions and
oracles below exercise the actual domain, repository and shared study services.
"""
import asyncio
import copy
import json
import sqlite3
import unittest
from collections import Counter
from unittest.mock import patch

from tests import test_new_logic_oral as fixture
from bot.app_config import ORAL_QUIZ
from bot.common import quiz_resume
from bot.common.study_lock import invalidate_study_frames
from bot.i18n import t
from bot.oral import domain, handlers as H, repository as R, view
from bot.study import answers
from bot.study.srs import update_fsrs


class IndependentOralTests(unittest.TestCase):
    def harness(self):
        h = fixture.OralTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        return h

    def test_original_and_drill_conservation_matrix(self):
        # Four original outcomes x three gaps x two limits x two drill outcomes.
        for pattern in ((True,)*4, (False, True, True, True),
                        (False, False, True, False), (False,)*4):
            for gap in (1, 3, 5):
                for limit in (1, 4):
                    for drill_correct in (False, True):
                        with self.subTest(pattern=pattern, gap=gap, limit=limit,
                                          drill_correct=drill_correct):
                            h = self.harness()
                            try:
                                h.options.update(gap=gap, limit=limit)
                                ids = ('a', 'b', 'c', 'd')
                                h.add(*ids)
                                for wid in ids: h.baseline(wid)
                                h.start(); h.call('oral_next')
                                originals, presentations = {}, {}
                                for _ in range(4 * (limit + 1) + 3):
                                    frame = h.frame()
                                    for name in ('pending', 'hidden'):
                                        slot = frame.get(name)
                                        if slot and slot['retry']:
                                            presentations[slot['action_key']] = (
                                                slot['word_id'], slot['presentation'])
                                    if frame['phase'] == 'completed': break
                                    pending = frame['pending']
                                    if pending['retry']:
                                        correct = drill_correct
                                    else:
                                        wid = pending['word_id']
                                        self.assertNotIn(wid, originals)
                                        correct = pattern[ids.index(wid)]
                                        originals[wid] = correct
                                    h.grade(correct)
                                else: self.fail('oral tail did not terminate within its presentation limit')
                                self.assertEqual(set(originals), set(ids))
                                self.assertEqual(domain.summary(h.frame()), {
                                    'count': 4, 'correct': sum(pattern),
                                    'wrong': 4-sum(pattern),
                                    'accuracy': str(round(sum(pattern)/4*100))+'%'})
                                counts = Counter(wid for wid, _ in presentations.values())
                                self.assertEqual(counts, Counter({wid: limit for wid in ids
                                    if not pattern[ids.index(wid)]}))
                                for wid in ids:
                                    numbered = sorted(n for w, n in presentations.values() if w == wid)
                                    self.assertEqual(numbered, list(range(1, limit+1))
                                        if not originals[wid] else [])
                                    self.assertEqual(h.srs(wid), update_fsrs(fixture.BASE,
                                        5 if originals[wid] else 2, fixture.DAY, 2))
                                self.assertEqual(len(h.rows('quiz_events')), 4)
                                self.assertEqual(len(h.rows('transaction_log')), 4)
                                self.assertEqual(len(h.rows('study_actions')), 4)
                            finally: h.doCleanups()

    def test_dynamic_limit_increase_and_gap_change_preserve_already_shown_slot(self):
        h = self.harness(); h.add('a', 'b', 'c', 'd', 'e')
        h.options.update(gap=1, limit=1)
        h.start(); h.call('oral_next'); h.grade(False); h.grade()
        hidden = copy.deepcopy(h.frame()['hidden'])
        self.assertEqual(hidden['word_id'], 'a')
        self.assertTrue(hidden['retry'])
        h.options.update(gap=9, limit=3)
        h.grade()
        self.assertEqual(h.frame()['pending'], hidden)
        shown = {}
        while h.frame()['phase'] != 'completed':
            for name in ('pending', 'hidden'):
                item = h.frame().get(name)
                if item and item['retry']:
                    shown[item['action_key']] = item['presentation']
            h.grade()
        self.assertEqual(sorted(shown.values()), [1, 2, 3])
        self.assertEqual(len(h.rows('quiz_events')), 5)
        self.assertEqual(domain.summary(h.frame())['wrong'], 1)

    def test_all_modes_and_independent_ui_languages_use_exact_srs_scope(self):
        for lang in ('ru', 'uk', 'en'):
            for ui in ('ru', 'uk', 'en'):
                for mode in ('de_'+lang, 'phrase_de_'+lang, 'phrase_'+lang+'_de'):
                    with self.subTest(mode=mode, ui=ui):
                        h = self.harness()
                        try:
                            h.options['dict_lang'] = lang
                            h.add('a', kind='phrase' if mode.startswith('phrase') else 'nomen')
                            h.baseline('a', mode)
                            h.baseline('a', lang+'_de')
                            h.baseline('a', mode, user=8)
                            with patch.object(H, '_user_interface_lang', return_value=ui):
                                h.start(mode)
                                markup = h.frame()['view'][1]['reply_markup']['keyboard']
                                self.assertIn(t(ui, 'oral_next'), str(markup))
                                self.assertNotIn(t(ui, 'clarify_fields'), str(markup))
                                h.call(update=h.update(t(ui, 'oral_next')))
                                text = h.frame()['view'][0]
                                self.assertIn('Guten Tag' if mode.endswith('_de') else
                                    {'ru': 'собака', 'uk': 'пес', 'en': 'dog'}[lang], text)
                                h.call(update=h.update(t(ui, 'oral_correct')))
                            self.assertEqual(h.srs('a', mode),
                                update_fsrs(fixture.BASE, 5, fixture.DAY, 2))
                            self.assertEqual(h.srs('a', lang+'_de'), fixture.BASE)
                            self.assertEqual(h.srs('a', mode, user=8), fixture.BASE)
                            self.assertEqual([(r['user_id'], r['mode'], r['is_correct'])
                                for r in h.rows('quiz_events')], [(7, mode, 1)])
                        finally: h.doCleanups()

    def test_forbidden_controls_and_old_ignored_delivery_never_grade(self):
        h = self.harness(); h.add('a', 'b'); h.start()
        old = []
        for text in ('idk', 'input_err', 'send_learn', '/undo', 'A', 'oral_correct', 'oral_wrong'):
            update = h.update(text); old.append(update); h.call(update=update)
        self.assertEqual(h.frame()['phase'], 'initial_hidden')
        h.call('oral_next'); before = copy.deepcopy(h.ctx.user_data)
        with patch.object(H, '_options', side_effect=AssertionError('replayed options')):
            for update in old: self.assertIsNone(h.call(update=update))
        for text in ('idk', 'input_err', 'send_learn', 'typed answer', 'oral_next'):
            h.call(text)
        self.assertEqual(h.ctx.user_data, before)
        self.assertEqual(h.rows('quiz_events'), [])
        self.assertEqual(h.rows('transaction_log'), [])

    def test_each_original_sql_write_failure_rolls_back_entire_plan_and_receipt(self):
        for table in ('progress', 'quiz_events', 'transaction_log', 'study_actions'):
            with self.subTest(table=table):
                h = self.harness()
                try:
                    h.add('a', 'b', 'c'); h.start(); h.call('oral_next')
                    before = copy.deepcopy(h.ctx.user_data)
                    ledgers = h.rows('study_deliveries')
                    h.conn.execute('CREATE TRIGGER qa_oral_failure BEFORE INSERT ON '+table+
                        " BEGIN SELECT RAISE(ABORT,'QA atomic oral write'); END")
                    h.conn.commit(); update = h.update('oral_wrong')
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'QA atomic oral write'):
                        h.grade(update=update)
                    self.assertEqual(h.ctx.user_data, before)
                    self.assertEqual(h.rows('study_deliveries'), ledgers)
                    for output in ('progress', 'quiz_events', 'transaction_log', 'study_actions'):
                        self.assertEqual(h.rows(output), [])
                    h.conn.execute('DROP TRIGGER qa_oral_failure'); h.conn.commit()
                    h.grade(update=update)
                    self.assertEqual(len(h.rows('quiz_events')), 1)
                    self.assertEqual(h.frame()['pending']['word_id'], 'b')
                    self.assertEqual(h.frame()['wrong'], 1)
                finally: h.doCleanups()

    def test_format_failure_at_each_boundary_drains_without_new_selection_or_answer(self):
        for phase in ('entry', 'reveal', 'original', 'drill', 'stop'):
            with self.subTest(phase=phase):
                h = self.harness()
                try:
                    h.add('a', 'b')
                    if phase != 'entry': h.start()
                    if phase in ('original', 'drill', 'stop'): h.call('oral_next')
                    if phase == 'drill':
                        h.grade(False); h.grade()
                        self.assertTrue(h.frame()['pending']['retry'])
                    update = h.update('oral_wrong' if phase in ('original', 'drill') else
                        'oral_next' if phase == 'reveal' else 'stop')
                    with patch.object(view, 'message', side_effect=RuntimeError('QA formatting')):
                        with self.assertRaisesRegex(RuntimeError, 'QA formatting'):
                            h.start(update=update) if phase == 'entry' else h.call(update=update)
                    expected = domain.copy_frame(h.frame())
                    counts = {table: len(h.rows(table)) for table in ('quiz_events', 'transaction_log')}
                    self.assertTrue(h.frame()['pending_render'])
                    with patch.object(R, 'pick_original', side_effect=AssertionError('drain picker')):
                        fresh = h.update('oral_correct'); h.call(update=fresh)
                        self.assertFalse(h.frame()['pending_render'])
                        state = copy.deepcopy(h.ctx.user_data)
                        self.assertIsNone(h.call(update=update))
                        self.assertIsNone(h.call(update=fresh))
                        self.assertEqual(h.ctx.user_data, state)
                    self.assertEqual(domain.copy_frame(h.frame()), expected)
                    self.assertEqual({table: len(h.rows(table)) for table in counts}, counts)
                finally: h.doCleanups()

    def test_undo_correct_and_wrong_restore_one_unit_and_keep_other_user(self):
        for correct in (False, True):
            with self.subTest(correct=correct):
                h = self.harness()
                try:
                    h.add('a', 'b'); h.baseline(); h.baseline(user=8)
                    h.start(); h.call('oral_next'); old = h.update('oral_correct' if correct else 'oral_wrong')
                    h.grade(update=old)
                    self.assertEqual(len(h.rows('transaction_log')), 1)
                    result = answers.undo_steps('synthetic', 7, 1, 9000)
                    self.assertEqual(result['restored'], 1)
                    self.assertEqual(h.srs(), fixture.BASE)
                    self.assertEqual(h.srs(user=8), fixture.BASE)
                    self.assertEqual(h.rows('quiz_events'), [])
                    self.assertEqual(h.rows('transaction_log'), [])
                    invalidate_study_frames(h.ctx); h.start()
                    before = copy.deepcopy(h.ctx.user_data)
                    with patch.object(H, '_options', side_effect=AssertionError('old answer options')):
                        self.assertIsNone(h.call(update=old))
                    self.assertEqual(h.ctx.user_data, before)
                    self.assertEqual(h.rows('quiz_events'), [])
                finally: h.doCleanups()

    def test_failed_clarify_refresh_drains_saved_translation_without_picker_or_grade(self):
        h = self.harness(); h.add('a', 'b'); h.start(); h.call('oral_next')
        point = quiz_resume.capture_return_point(h.ctx, ORAL_QUIZ)
        self.assertEqual((point.visible_answer_id, point.current_question_id), ('a', 'b'))
        before = domain.copy_frame(h.frame())
        h.conn.execute("UPDATE words SET russian='FRESH_VISIBLE_A' WHERE id='a'"); h.conn.commit()
        with patch.object(h.service, 'refresh', side_effect=sqlite3.OperationalError('QA clarify read')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'QA clarify read'):
                asyncio.run(quiz_resume.render_existing_frame(h.update(), h.ctx))
        self.assertTrue(h.frame()['pending_render'])
        fresh = h.update('oral_correct')
        with patch.object(R, 'pick_original', side_effect=AssertionError('clarify picker')):
            h.call(update=fresh)
        self.assertIn(r'FRESH\_VISIBLE\_A', fresh.message.sent[-1][0])
        self.assertEqual(domain.copy_frame(h.frame()), before)
        self.assertFalse(h.frame()['pending_render'])
        self.assertEqual(h.rows('quiz_events'), [])
        self.assertEqual(h.rows('transaction_log'), [])

    def test_delete_initial_visible_hidden_and_completed_keeps_valid_remaining_tail(self):
        for phase, target in (('initial', 'a'), ('grading', 'a'),
                              ('grading', 'b'), ('completed', 'b')):
            with self.subTest(phase=phase, target=target):
                h = self.harness()
                try:
                    h.add('a', 'b'); h.start()
                    if phase != 'initial': h.call('oral_next')
                    if phase == 'completed': h.grade(); h.grade()
                    old_count = len(h.rows('quiz_events'))
                    quiz_resume.capture_return_point(h.ctx, ORAL_QUIZ)
                    h.conn.execute('DELETE FROM words WHERE id=?', (target,)); h.conn.commit()
                    asyncio.run(quiz_resume.resume_after_delete(h.update(), h.ctx, target))
                    for name in ('pending', 'hidden', 'last_visible'):
                        slot = h.frame().get(name)
                        if slot: self.assertNotEqual(slot['word_id'], target)
                    self.assertNotIn(target, h.frame()['retry_queue'])
                    self.assertEqual(len(h.rows('quiz_events')), old_count)
                    if phase == 'initial':
                        self.assertEqual(h.frame()['hidden']['word_id'], 'b')
                        self.assertEqual(h.frame()['phase'], 'initial_hidden')
                    elif phase == 'grading':
                        self.assertEqual(h.frame()['pending']['word_id'], 'b' if target == 'a' else 'a')
                        self.assertIsNone(h.frame()['hidden'])
                        h.grade()
                        self.assertEqual(h.frame()['phase'], 'completed')
                    else: self.assertEqual(h.frame()['phase'], 'completed')
                finally: h.doCleanups()

    def test_receipt_frames_are_json_plans_without_live_render_payload(self):
        h = self.harness(); h.add('a', 'b'); h.start(); h.call('oral_next'); h.grade(False)
        for row in h.rows('study_deliveries'):
            result = json.loads(row['result_json'])
            frame = result.get('oral_frame') or (result.get('after') or {}).get('oral_frame')
            if frame:
                for key in ('view', 'pending_render', 'render_transport', 'render_revision'):
                    self.assertNotIn(key, frame)
                self.assertEqual(json.loads(json.dumps(frame)), frame)
        self.assertEqual(len(h.rows('quiz_events')), 1)


def main_oral_script(body):
    """Reuse only author transport/SQLite setup; replace its entire scenario."""
    from pathlib import Path
    import ast
    import textwrap
    author = Path(__file__).with_name('test_new_logic_oral.py').read_text()
    tree = ast.parse(author)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == 'OralMainIntegrationTests')
    raw = next(node.value for node in ast.walk(owner)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and 'async def scenario()' in node.value)
    script = textwrap.dedent(raw)
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith('routes ='))
    end = max(i for i, line in enumerate(lines) if line.strip() == 'await app.shutdown()')
    indent = lines[start][:-len(lines[start].lstrip())]
    independent = textwrap.indent(textwrap.dedent(body).strip(), indent)
    script = '\n'.join(lines[:start] + [independent,
        indent+"assert NoNetworkRequest.attempts == 0",
        indent+"print('independent actual main oral QA PASS; network=0, SQLite=:memory:')"] + lines[end:])
    core = Path(__file__).with_name('test_new_logic_core_conversation.py').read_text()
    raw = next(n.value for n in ast.walk(ast.parse(core)) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and 'class NoNetworkRequest' in n.value)
    return textwrap.dedent(raw).split('class FakeService:')[0] + script


RETURN_FAILURE_BODY = r"""
import sqlite3
for fault in ('sql', 'format', 'send'):
    for same in (False, True):
        reset(); await enter(); await send(t('en', 'oral_next'))
        slots = copy.deepcopy((h.frame()['pending'], h.frame()['hidden']))
        await send(t('en', 'clarify_fields'))
        assert app.user_data[UID]['quiz_edit_entry']['id'] == slots[0]['word_id']
        h.conn.execute("UPDATE words SET russian='QA saved translation' WHERE id='a'"); h.conn.commit()
        back = update(t('en', 'back'))
        target, name, error = ((h.service, 'refresh', sqlite3.OperationalError('QA return SQL')) if fault == 'sql'
            else (view, 'message', RuntimeError('QA return format')) if fault == 'format'
            else (FakeBot, 'send_message', RuntimeError('QA return send')))
        with patch.object(target, name, side_effect=error):
            await app.process_update(back)
        assert len(errors) == 1 and str(errors.pop()) == str(error), errors
        assert conv._conversations[key] == main.ADD_EDIT
        assert h.frame()['pending_render']
        retry = back if same else update(t('en', 'oral_correct'))
        with patch.object(oral_repo, 'pick_original', side_effect=AssertionError('QA return picker')):
            await process(retry)
        assert conv._conversations[key] == main.ORAL_QUIZ, 'QA recovered return must leave ADD_EDIT'
        assert 'QA saved translation' in bot.sent[-1]
        assert not h.frame()['pending_render']
        assert (h.frame()['pending'], h.frame()['hidden']) == slots
        assert rows('quiz_events') == [] and rows('transaction_log') == []
        assert 'quiz_return_point' not in app.user_data[UID]
        await send(t('en', 'oral_correct'))
        assert len(rows('quiz_events')) == 1 and h.frame()['pending']['word_id'] == slots[1]['word_id']
        stable = copy.deepcopy(h.frame()); count = len(bot.sent)
        await process(retry); await process(back)
        assert h.frame() == stable and len(bot.sent) == count
        assert conv._conversations[key] == main.ORAL_QUIZ and 'quiz_return_point' not in h.ctx.user_data
        assert len(rows('quiz_events')) == 1
"""


class IndependentOralMainQA(unittest.TestCase):
    def setUp(self):
        import os
        from pathlib import Path
        from uuid import uuid4
        stand = Path('/Users/wf/py/_agent_work/888_german_dictionary/new_logic_20261020')
        # Legacy tests may remove runner variables. Each real-dispatch fixture
        # supplies its own safe paths and restores the caller after the test.
        self.enterContext(patch.dict(os.environ, {
            'DB_PATH': str(stand / ('qa-oral-dispatch-'+uuid4().hex+'.sqlite')),
            'TMPDIR': str(stand),
        }))

    def run_case(self, body):
        import os
        import subprocess
        import sys
        from pathlib import Path
        self.assertTrue(os.environ.get('DB_PATH', '').startswith('/Users/wf/py/_agent_work/888_german_dictionary/'))
        self.assertTrue(os.environ.get('TMPDIR', '').startswith('/Users/wf/py/_agent_work/888_german_dictionary/'))
        result = subprocess.run([sys.executable, '-B', '-c', main_oral_script(body)],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('network=0', result.stdout)

    def test_actual_main_three_oral_modes_manual_interval_formula_and_undo(self):
        self.run_case(r"""
            from bot.study.srs import update_fsrs
            from tests.test_new_logic_oral import BASE
            h.add('q', kind='phrase')
            routes = [('DE','Words','de_ru','a'), ('DE','Phrases','phrase_de_ru','p'),
                      ('L1','Phrases','phrase_ru_de','p')]
            for direction, kind, mode, wid in routes:
                reset(); h.baseline(wid, mode); h.baseline('b' if kind == 'Words' else 'q', mode)
                h.baseline(wid, 'ru_de'); h.baseline(wid, mode, user=8)
                await enter(direction, kind)
                assert conv._conversations[key] == main.ORAL_QUIZ
                assert h.frame()['mode'] == mode and h.frame()['pending'] is None
                await send(t('en','oral_next'))
                slots = copy.deepcopy((h.frame()['pending'], h.frame()['hidden']))
                assert slots[0]['word_id'] == wid and slots[1]['word_id'] != wid
                await send(t('en','clarify_fields')); await send('add_int')
                assert h.ctx.user_data['quiz_edit_entry']['id'] == wid
                assert h.ctx.user_data['add_int_mode'] == mode
                await send('3'); await send(clarify.SYN_CONFIRM_TEXT)
                expected = dict(BASE)
                for _ in range(3): expected = update_fsrs(expected,5,DAY,2)
                assert h.srs(wid,mode) == expected, (mode,h.srs(wid,mode),expected)
                assert h.srs(wid,'ru_de') == BASE and h.srs(wid,mode,user=8) == BASE
                assert rows('quiz_events') == [] and len(rows('transaction_log')) == 1
                await send(t('en','back'))
                assert conv._conversations[key] == main.ORAL_QUIZ
                assert (h.frame()['pending'],h.frame()['hidden']) == slots
                await send('/undo',True)
                assert conv._conversations[key] == main.MENU and oral.FRAME not in h.ctx.user_data
                assert h.srs(wid,mode) == BASE and h.srs(wid,'ru_de') == BASE
                assert rows('transaction_log') == [] and rows('quiz_events') == []
                for good in (False, True):
                    await send('Tests'); await enter(direction,kind); await send(t('en','oral_next'))
                    grade = await send(t('en','oral_correct' if good else 'oral_wrong'))
                    assert [(r['word_id'],r['mode'],r['is_correct']) for r in rows('quiz_events')] == [(wid,mode,int(good))]
                    assert h.srs(wid,mode) == update_fsrs(BASE,5 if good else 2,DAY,2)
                    assert h.srs(wid,'ru_de') == BASE and h.srs(wid,mode,user=8) == BASE
                    await send('/undo',True)
                    assert conv._conversations[key] == main.MENU and oral.FRAME not in h.ctx.user_data
                    assert h.srs(wid,mode) == BASE and rows('quiz_events') == []
                    await process(grade)
                    assert conv._conversations[key] == main.MENU and rows('quiz_events') == []
        """)

    def test_actual_main_singleton_drills_stop_zero_and_written_word_grade(self):
        self.run_case(r"""
            from bot.study.srs import update_fsrs
            from tests.test_new_logic_oral import BASE
            # Written words retain their route, one event and the accepted 1.65 growth divisor.
            reset(); h.baseline('a','ru_de'); h.baseline('b','ru_de')
            await enter('L1','Words')
            assert conv._conversations[key] == main.QUIZ_RU_DE and oral.FRAME not in h.ctx.user_data
            with patch.object(quiz,'get_synonym_group',return_value=[]), patch.object(quiz,'_apply_answer_reaction',new=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock()):
                await send('der A Hunde')
            assert [(r['word_id'],r['mode']) for r in rows('quiz_events')] == [('a','ru_de')]
            assert h.srs('a','ru_de') == update_fsrs(BASE,5,DAY,1.65)
            # Zero before and after reveal must not assign either ungraded slot a result.
            for revealed in (False, True):
                reset(); await enter()
                if revealed: await send(t('en','oral_next'))
                await send(t('en','stop'))
                assert h.frame()['phase'] == 'completed' and h.frame()['correct'] == h.frame()['wrong'] == 0
                assert rows('quiz_events') == [] and rows('transaction_log') == [] and '—' in bot.sent[-1]
            # One phrase has no hidden tail; four drills must each be graded, but never counted as originals.
            reset(); await enter('DE','Phrases'); await send(t('en','oral_next'))
            assert h.frame()['pending']['word_id'] == 'p' and h.frame()['hidden'] is None
            await send(t('en','oral_wrong'))
            scheduled = copy.deepcopy(h.srs('p','phrase_de_ru'))
            seen = []
            for ordinal in range(1,5):
                item = h.frame()['pending']; seen.append(item['action_key'])
                assert item['retry'] and item['presentation'] == ordinal
                assert h.frame()['hidden'] is None
                await send(t('en','oral_correct' if ordinal%2 else 'oral_wrong'))
                assert h.srs('p','phrase_de_ru') == scheduled
                assert len(rows('quiz_events')) == len(rows('transaction_log')) == 1
            assert len(set(seen)) == 4 and h.frame()['phase'] == 'completed'
            assert h.frame()['correct'] == 0 and h.frame()['wrong'] == 1
        """)

    def test_actual_main_clarify_synonyms_completed_manual_change_and_delete(self):
        self.run_case(r"""
            from bot.handlers import synonyms
            reset(); await enter(); await send(t('en','oral_next'))
            before = copy.deepcopy(h.frame()); await send(t('en','clarify_fields'))
            assert h.ctx.user_data['quiz_edit_entry']['id'] == 'a'
            point = h.ctx.user_data['quiz_return_point']
            assert point.visible_answer_id == 'a' and point.current_question_id == 'b'
            with patch.object(synonyms,'get_synonym_group',return_value=[]):
                await send(clarify.SYNONYMS_TEXT)
                assert conv._conversations[key] == main.SYN_MENU and h.ctx.user_data['quiz_return_point'] == point
                with patch.object(oral_repo,'pick_original',side_effect=AssertionError('QA synonym return picker')):
                    await send(t('en','back'))
            assert conv._conversations[key] == main.ORAL_QUIZ
            assert h.frame()['pending'] == before['pending'] and h.frame()['hidden'] == before['hidden']
            assert rows('quiz_events') == [] and rows('transaction_log') == []
            await send(t('en','oral_correct')); await send(t('en','oral_correct'))
            assert h.frame()['phase'] == 'completed' and h.frame()['correct'] == 2
            events = copy.deepcopy(rows('quiz_events')); count = len(rows('transaction_log'))
            await send(t('en','clarify_fields')); await send('add_int'); await send('3'); await send(clarify.SYN_CONFIRM_TEXT)
            assert len(rows('transaction_log')) == count+1 and rows('quiz_events') == events
            with patch.object(oral_repo,'pick_original',side_effect=AssertionError('QA completed return picker')):
                await send(t('en','back'))
            assert h.frame()['phase'] == 'completed' and h.frame()['pending'] is None and h.frame()['hidden'] is None
            assert h.frame()['correct'] == 2 and rows('quiz_events') == events
            await send(t('en','clarify_fields')); await send('delete'); await send('yes')
            assert conv._conversations[key] == main.ORAL_QUIZ
            assert h.frame()['phase'] == 'completed' and h.frame()['last_visible'] is None
            assert h.frame()['correct'] == 2 and h.frame()['pending'] is None
        """)

    def test_actual_add_edit_pending_return_recovers_sql_format_and_send(self):
        self.run_case(RETURN_FAILURE_BODY)

    def test_actual_main_initial_pending_grade_and_notify_late_delivery_boundaries(self):
        self.run_case(r"""
            for direction,kind,mode in [('DE','Words','de_ru'),('DE','Phrases','phrase_de_ru'),('L1','Phrases','phrase_ru_de')]:
                for fresh in (False,True):
                    reset(); await send(direction); entry = update(kind)
                    with patch.object(FakeBot,'send_message',side_effect=RuntimeError('QA first oral send')):
                        await app.process_update(entry)
                    assert len(errors)==1 and str(errors.pop())=='QA first oral send'
                    assert conv._conversations[key] == main.QUIZ_RU_DE_MENU
                    reserved = copy.deepcopy(h.frame()['hidden']); flow = h.frame()['flow_id']
                    retry = update(t('en','oral_correct')) if fresh else entry
                    with patch.object(oral_repo,'pick_original',side_effect=AssertionError('QA initial retry picker')):
                        await process(retry)
                    assert conv._conversations[key] == main.ORAL_QUIZ
                    assert h.frame()['flow_id'] == flow and h.frame()['hidden'] == reserved and h.frame()['pending'] is None
                    assert rows('quiz_events') == []
                    await send(t('en','oral_next'))
                    grade = update(t('en','oral_correct'))
                    with patch.object(FakeBot,'send_message',side_effect=RuntimeError('QA graded send')):
                        await app.process_update(grade)
                    assert len(errors)==1 and str(errors.pop())=='QA graded send'
                    assert len(rows('quiz_events')) == 1 and h.frame()['pending_render']
                    stable = copy.deepcopy((h.frame()['pending'],h.frame()['hidden'],h.frame()['retry_counts']))
                    await send(t('en','oral_wrong'))
                    assert not h.frame()['pending_render']
                    assert (h.frame()['pending'],h.frame()['hidden'],h.frame()['retry_counts']) == stable
                    assert len(rows('quiz_events')) == 1
                    old_events = copy.deepcopy(rows('quiz_events'))
                    base=update('notify')
                    query=CallbackQuery('QA-notify',User(UID,'Learner',False),'synthetic-chat-instance',message=base.message,data='notify:quiz')
                    query.set_bot(bot); callback=Update(base.update_id,callback_query=query);callback.set_bot(bot)
                    await process(callback)
                    assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU and oral.FRAME not in h.ctx.user_data
                    await process(entry); await process(grade); await process(callback)
                    assert conv._conversations[key] == main.NOTIFICATION_SETTINGS_MENU and rows('quiz_events') == old_events
                    assert len(rows('transaction_log')) == 1
        """)


if __name__ == '__main__':
    unittest.main()
