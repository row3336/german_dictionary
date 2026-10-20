"""Read-only dispatch preflight and public PTB callback replay boundaries."""

import ast
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from bot.storage import init_db
from bot.study import deliveries, repository, transport_guard as guard
from bot.common.study_lock import invalidate_study_frames

STAND = Path('/Users/wf/py/_agent_work/888_german_dictionary/new_logic_learning_tests')


class TransportGuardTest(unittest.TestCase):
    def setUp(self):
        STAND.mkdir(parents=True, exist_ok=True)
        self.db = str(Path(tempfile.mkdtemp(prefix='transport-', dir=STAND)) / 'synthetic.sqlite')
        init_db(self.db)
        with repository.transaction(self.db) as conn:
            conn.executemany("INSERT INTO users(user_id,status,interface_lang,dict_lang) VALUES (?,?, 'en','ru')",
                             [(1, 'approved'), (2, 'pending'), (7, 'approved')])

    def test_lookup_is_read_only_closes_connection_and_never_reserves_fresh_input(self):
        real_connect, trace, opened = sqlite3.connect, [], []
        def connect(*args, **kwargs):
            self.assertTrue(kwargs['uri'])
            self.assertTrue(args[0].endswith('?mode=ro'))
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(trace.append)
            opened.append(conn)
            return conn
        with patch.object(guard.sqlite3, 'connect', side_effect=connect):
            self.assertEqual(guard.read_delivery(self.db, 1, 100), (True, None))
        self.assertTrue(trace)
        self.assertTrue(all(sql.lstrip().upper().startswith('SELECT') for sql in trace), trace)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')
        with repository.transaction(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM study_deliveries').fetchone()[0], 0)
        with patch('bot.app_config.is_owner', return_value=False):
            self.assertEqual(guard.read_delivery(self.db, 2, 100), (False, None))

    def test_processing_undo_resumes_but_committed_or_extinguished_actions_stop(self):
        data = {guard.LAST_TRANSPORT: 99}
        receipt = {'state': 'processing', 'action_kind': 'undo', 'flow_id': 'undo'}
        self.assertTrue(guard.allow_dispatch(data, 10, receipt))
        receipt['state'] = 'committed'
        self.assertFalse(guard.allow_dispatch(data, 10, receipt))
        receipt.update(action_kind='answer', action_state='undone')
        data = {guard.LAST_TRANSPORT: 10, 'learning_frame': {
            'flow_id': 'undo', 'pending_render': True, 'accepted_delivery': 10}}
        self.assertFalse(guard.allow_dispatch(data, 10, receipt))

    def test_all_live_pending_views_pass_and_a_new_input_extinguishes_each_retry(self):
        receipt = {'state': 'committed', 'action_kind': 'answer', 'flow_id': 'flow'}
        cases = [
            {'learning_frame': {'flow_id': 'flow', 'pending_render': True, 'accepted_delivery': 10}},
            {'quiz_written_frame': {'revision': 3, 'pending_render': True, 'render_transport': 10, 'render_revision': 3}},
            {'quiz_written_frame': {'revision': 3, 'next_view': {'transport': 10, 'revision': 3, 'adopted': False}}},
            {'oral_frame': {'flow_id': 'flow', 'revision': 3, 'pending_render': True, 'render_transport': 10, 'render_revision': 3},
             'quiz_published_view': {'caller_kind': 'oral', 'flow_id': 'flow'}},
            {'conjugation_frame': {'flow_id': 'reset-new-flow', 'view_revision': 3, 'pending_render': True,
                                   'pending_render_transport': 10, 'pending_render_revision': 3},
             'conjugation_callback_receipts': {10: True}},
            {'conjugation_frame': {'kind': 'test', 'flow_id': 'flow', 'pending_render': True}},
            *[{key: {'transport': 10}} for key in (
                'quiz_start_pending', 'quiz_notification_pending', 'quiz_learning_pending_navigation')],
        ]
        for data in cases:
            with self.subTest(frame=next(iter(data))):
                self.assertTrue(guard.allow_dispatch(data, 10, receipt))
                frame = json.dumps(data, sort_keys=True)
                self.assertTrue(guard.allow_dispatch(data, 11, None))
                self.assertFalse(guard.allow_dispatch(data, 10, receipt))
                data[guard.LAST_TRANSPORT] = 10
                self.assertEqual(json.dumps(data, sort_keys=True), frame)

    def test_written_adopted_plan_does_not_reopen_an_already_delivered_view(self):
        receipt = {"state": "committed", "action_kind": "answer", "flow_id": "flow"}
        frame = {"revision": 3, "render_transport": 10, "render_revision": 3,
                 "pending_render": False,
                 "next_view": {"transport": 10, "revision": 3, "selected": True, "adopted": True}}
        data = {"quiz_written_frame": frame}
        self.assertFalse(guard.allow_dispatch(data, 10, receipt))
        frame["render_revision"] = 2  # Formatting failed after adoption, before delivery.
        self.assertTrue(guard.allow_dispatch(data, 10, receipt))
        frame["render_revision"], frame["pending_render"] = 3, True
        self.assertTrue(guard.allow_dispatch(data, 10, receipt))
        frame["pending_render"] = False
        self.assertFalse(guard.allow_dispatch(data, 10, receipt))

    def test_connection_scoped_inspection_reads_the_same_memory_ledger(self):
        from bot.study.schema_v18 import ensure_schema
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY,status TEXT)")
            conn.execute("CREATE TABLE words(id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE learning_items(user_id INTEGER, word_id TEXT, status TEXT, preview_done INTEGER)")
            ensure_schema(conn)
            conn.execute("INSERT INTO users VALUES(1,'approved')")
            deliveries.begin_delivery(conn, 1, 50, "memory", "memory")
            deliveries.commit_delivery(conn, 1, 50, {"flow_id": "memory"})
            changes = conn.total_changes
            authorized, delivery = guard.inspect_delivery(conn, 1, 50)
            self.assertTrue(authorized)
            self.assertEqual(delivery["flow_id"], "memory")
            self.assertEqual(conn.total_changes, changes)
        finally:
            conn.close()

    def test_navigation_retry_never_overrides_new_live_flow_and_watermark_survives_invalidation(self):
        receipt = {'state': 'committed', 'action_kind': 'start', 'flow_id': 'start'}
        for key in ('learning_frame', 'quiz_written_frame', 'conjugation_frame', 'oral_frame'):
            data = {'quiz_start_pending': {'transport': 10}, key: {'flow_id': 'new'}}
            self.assertFalse(guard.allow_dispatch(data, 10, receipt))
        data = {guard.LAST_TRANSPORT: 11, 'quiz_start_pending': {'transport': 10}}
        context = type('Context', (), {'user_data': data})()
        invalidate_study_frames(context)
        self.assertEqual(data, {guard.LAST_TRANSPORT: 11})
        data['quiz_start_pending'] = {'transport': 10}
        self.assertFalse(guard.allow_dispatch(data, 10, receipt))

    def test_real_public_handler_guard_stops_late_callbacks_and_preserves_pending_and_undo(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'tests/test_new_logic_core_conversation.py').read_text())
        raw = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and 'class NoNetworkRequest' in node.value)
        prefix = textwrap.dedent(raw).split('class FakeService:')[0]
        script = prefix + textwrap.dedent(r'''
            from telegram.ext import TypeHandler
            from bot.common.study_lock import study_lock
            from bot.study import answers, deliveries, repository, transport_guard
            import sqlite3

            def update(bot, ident, data, uid=UID):
                message = Message(ident, NOW, Chat(uid, 'private'), from_user=User(uid, 'Learner', False), text='card')
                message.set_bot(bot)
                query = CallbackQuery(str(ident), User(uid, 'Learner', False), 'synthetic', message=message, data=data)
                query.set_bot(bot)
                value = Update(ident, callback_query=query); value.set_bot(bot)
                return value

            async def scenario():
                bot = FakeBot(); app = Application.builder().bot(bot).job_queue(None).build()
                builder = RecordingBuilder(app); errors = []; calls = []
                with ExitStack() as stack:
                    stack.enter_context(patch.dict('os.environ', {'BOT_TOKEN': '123456:synthetic', 'OWNER_ID': '0'}))
                    for name in ('load_dotenv','_configure_logging','require_supported_ptb','_prepare_storage',
                                 '_configure_learning_reminders','_configure_fsrs_reports','_configure_access_reminders','_configure_owner_digest'):
                        stack.enter_context(patch.object(main, name))
                    stack.enter_context(patch.object(main, 'Application', SimpleNamespace(builder=lambda: builder)))
                    stack.enter_context(patch.object(main, 'AIORateLimiter', return_value=object()))
                    stack.enter_context(patch.object(main, 'run_polling_safely'))
                    main.main()
                    registered = app.handlers[-1][0]
                    assert isinstance(registered, TypeHandler) and registered.callback is main.study_update_guard
                    for handler in list(app.handlers[0]):
                        app.remove_handler(handler, group=0)
                    app.error_handlers.clear()
                    async def on_error(update, context): errors.append(context.error)
                    app.add_error_handler(on_error)
                    async def callback(update, context):
                        calls.append(update.update_id)
                        if update.callback_query.data == 'guard:onboarding':
                            return
                        async with study_lock(update.effective_user.id):
                            if update.callback_query.data == 'guard:undo':
                                answers.undo_steps(main.get_db_path(), UID, 1, update.update_id)
                                return
                            receipt = answers.record_control_transition(main.get_db_path(), UID,
                                update.update_id, 'direct', 'direct_callback', lambda conn: {'flow_id': 'direct'})
                            if not receipt.get('replayed'):
                                context.user_data['quiz_written_frame'] = {'flow_id': 'direct', 'revision': 1,
                                    'pending_render': True, 'render_transport': update.update_id, 'render_revision': 1}
                                raise RuntimeError('synthetic pending callback')
                            context.user_data['quiz_written_frame']['pending_render'] = False
                    async def unrelated(update, context): calls.append(update.update_id)
                    app.add_handler(CallbackQueryHandler(callback, pattern='^guard:'))
                    app.add_handler(TypeHandler(Update, unrelated))
                    await app.initialize()
                    first = update(bot, 100, 'guard:view')
                    await app.process_update(first)
                    assert len(errors) == 1 and str(errors.pop()) == 'synthetic pending callback'
                    await app.process_update(first)
                    assert calls == [100, 100] and not errors, (calls, errors)
                    await app.process_update(first)
                    assert calls == [100, 100]
                    pending = update(bot, 101, 'guard:view')
                    await app.process_update(pending)
                    assert len(errors) == 1 and str(errors.pop()) == 'synthetic pending callback'
                    before = dict(app.user_data[UID]['quiz_written_frame'])
                    await app.process_update(update(bot, 102, 'unrelated'))
                    await app.process_update(pending)
                    assert calls == [100, 100, 101, 102] and not errors, (calls, errors)
                    assert app.user_data[UID]['quiz_written_frame'] == before
                    with repository.transaction(main.get_db_path()) as conn:
                        deliveries.begin_delivery(conn, UID, 50, 'undo', 'undo', result={'tx_ids': [], 'cursor': 0, 'outcomes': []})
                        deliveries.begin_delivery(conn, 2, 51, 'onboarding', 'onboarding')
                        deliveries.commit_delivery(conn, 2, 51, {})
                    await app.process_update(update(bot, 50, 'guard:undo'))
                    assert calls[-1] == 50 and not errors
                    await app.process_update(update(bot, 50, 'guard:undo'))
                    assert calls.count(50) == 1
                    await app.process_update(update(bot, 51, 'guard:onboarding', uid=2))
                    assert calls[-1] == 51 and not errors
                    assert transport_guard.LAST_TRANSPORT not in app.user_data[2]
                    assert NoNetworkRequest.attempts == 0
                    await app.shutdown()
                    print('public TypeHandler replay/pending/undo/onboarding PASS; network=0')
            asyncio.run(scenario())
        ''')
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=root,
            env={**os.environ, 'DB_PATH': self.db}, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('network=0', result.stdout)


if __name__ == '__main__':
    unittest.main()
