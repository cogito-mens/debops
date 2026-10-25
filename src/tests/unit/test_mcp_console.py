# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the 'mcp console' interface.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from unittest import mock

from debops.mcp import repl
from debops.mcp.console import ConsoleRepl
from tests.unit.test_mcp_repl import FakeClient, FakeTailConnection


def make_session(console_config=None):
    client = mock.MagicMock()
    client.legacy = False
    client.bearer = None
    client.request.return_value = {'result': {
            'resultType': 'complete', 'isError': False,
            'content': [{'type': 'text', 'text': 'Project: x'}]}}
    session = ConsoleRepl(client, project_name='project', view='system',
                          project_type='legacy', view_root_rel='ansible',
                          console_config=console_config)
    return session, client


def attach_transcript(session):
    """The transcript writer the interface build attaches."""
    from debops.mcp.console import _TranscriptWriter
    writer = _TranscriptWriter(session)
    session._out = writer
    session._err = writer


def transcript_text(session):
    """The transcript's lines as one text."""
    return '\n'.join(session._console_lines)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def passphrase_error(session):
    """Run the passphrase prompt; return the error it raises."""
    try:
        session._ask_passphrase()
    except repl.ReplError as errmsg:
        return str(errmsg)


class ConsoleSessionTestCase(unittest.TestCase):

    def test_the_transcript_collects_the_session(self):
        session, _client = make_session()
        attach_transcript(session)
        session.submit('status')
        self.assertTrue(wait_for(lambda: not session._busy))
        text = transcript_text(session)
        self.assertIn('> status', text)
        self.assertIn('Project: x', text)

    def test_the_gate_is_answered_on_the_input_line(self):
        session, _client = make_session()
        attach_transcript(session)
        answers = []
        thread = threading.Thread(
                target=lambda: answers.append(
                        session._gate_ask('apply the changes? [y/N] ')),
                daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        # The question is visible; the input line answers it
        self.assertIn('apply the changes?', transcript_text(session))
        session.submit('y')
        thread.join(timeout=5)
        self.assertEqual(answers, [True])
        self.assertIsNone(session._gate)

    def test_the_gate_decline(self):
        session, _client = make_session()
        attach_transcript(session)
        answers = []
        thread = threading.Thread(
                target=lambda: answers.append(session._ask('sure?')),
                daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        self.assertTrue(session._gate_decline())
        thread.join(timeout=5)
        self.assertEqual(answers, [False])

    def test_the_passphrase_gate_does_not_echo_the_secret(self):
        session, _client = make_session()
        attach_transcript(session)
        answers = []
        thread = threading.Thread(
                target=lambda: answers.append(
                        session._ask_passphrase()), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        session.submit('correct horse battery staple')
        thread.join(timeout=5)
        self.assertEqual(answers, ['correct horse battery staple'])
        self.assertNotIn('correct horse', transcript_text(session))

    def test_the_passphrase_gate_cancelled(self):
        session, _client = make_session()
        attach_transcript(session)
        errors = []
        thread = threading.Thread(
                target=lambda: errors.append(
                        passphrase_error(session)), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        session._gate_decline()
        thread.join(timeout=5)
        self.assertEqual(errors, ['cancelled'])

    def test_submit_serializes_the_commands(self):
        session, _client = make_session()
        attach_transcript(session)
        session._busy = True
        session.submit('status')
        self.assertIn('a command is running', transcript_text(session))

    def test_the_watch_lands_in_the_transcript(self):
        session, _client = make_session()
        attach_transcript(session)
        session._watch_line('run #1 finished')
        session._watch_line('run #2 running')
        self.assertIn('run #1 finished\nrun #2 running',
                      transcript_text(session))

    def test_the_watch_line_without_the_interface_prints(self):
        session, _client = make_session()
        # No transcript attached: the engine's default (a print) —
        # nothing crashes
        session._out = mock.MagicMock()
        session._watch_line('a line')
        session._out.write.assert_called()

    def test_the_focus_dance(self):
        # Headless (no application): the state moves, the focus
        # calls no-op
        session, _client = make_session()
        self.assertEqual(session.console_focus, 'prompt')
        self.assertIsNone(session.console_tab)
        session._focus_area()
        self.assertEqual(session.console_focus, 'area')
        self.assertEqual(session.console_tab, 'queue')
        session._cycle_tab(1)
        self.assertEqual(session.console_tab, 'runs')
        session._cycle_tab(-2)
        self.assertEqual(session.console_tab, 'credits')
        session._toggle_tab('docs')
        self.assertEqual(session.console_tab, 'docs')
        # The open tab's key closes the area, back to the prompt
        session._toggle_tab('docs')
        self.assertIsNone(session.console_tab)
        self.assertEqual(session.console_focus, 'prompt')
        session._focus_prompt()

    def test_the_console_config_overrides(self):
        session, _client = make_session(
                console_config={'repl_lines': 8,
                                'prompt_active': False,
                                'keys': 'emacs'})
        self.assertEqual(session.console_repl_lines, 8)
        self.assertFalse(session.console_prompt_active)
        self.assertEqual(session.console_keys, 'emacs')
        session, _client = make_session()
        self.assertEqual(session.console_repl_lines, 5)
        self.assertTrue(session.console_prompt_active)
        self.assertEqual(session.console_keys, 'vim')

    def test_exit_leaves_the_application(self):
        session, _client = make_session()
        attach_transcript(session)
        app = mock.MagicMock()
        session._app = app
        session.submit('exit')
        self.assertTrue(wait_for(lambda: app.exit.called))
        self.assertFalse(session._busy)

    def test_an_internal_error_lands_in_the_transcript(self):
        session, client = make_session()
        attach_transcript(session)
        client.request.side_effect = RuntimeError('boom')
        session.submit('status')
        self.assertTrue(wait_for(lambda: not session._busy))
        text = transcript_text(session)
        self.assertIn('error: internal: boom', text)
        self.assertIn('Traceback', text)
        # The session survived
        client.request.side_effect = None
        session.submit('status')
        self.assertTrue(wait_for(lambda: 'Project: x'
                                 in transcript_text(session)))


class EditorSuspensionTestCase(unittest.TestCase):
    """The console's 'edit' suspends the interface — scheduled on
    the application's event loop, never on the worker thread."""

    def test_the_editor_runs_directly_when_headless(self):
        session, _client = make_session()
        session._app = None
        with mock.patch.object(repl.Repl, '_run_editor',
                               return_value=0) as editor:
            self.assertEqual(session._run_editor('/tmp/x'), 0)
        editor.assert_called_once_with('/tmp/x')

    def test_the_editor_runs_directly_when_the_loop_is_closed(self):
        session, _client = make_session()
        app = mock.MagicMock()
        app.loop.is_closed.return_value = True
        session._app = app
        with mock.patch.object(repl.Repl, '_run_editor',
                               return_value=0) as editor:
            self.assertEqual(session._run_editor('/tmp/x'), 0)
        self.assertTrue(editor.called)

    def test_the_editor_is_scheduled_on_the_app_loop(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        session, _client = make_session()
        loop = mock.MagicMock()
        loop.is_closed.return_value = False
        # The test double runs the scheduled callback at once, as
        # the live loop would on its own thread
        loop.call_soon_threadsafe.side_effect = lambda cb: cb()
        app = mock.MagicMock()
        app.loop = loop
        session._app = app
        with mock.patch('prompt_toolkit.application.run_in_terminal'
                        '.run_in_terminal') as run_in_terminal, \
                mock.patch.object(repl.Repl, '_run_editor',
                                  return_value=0) as editor:
            future = mock.MagicMock()

            def run(func):
                func()
                return future

            run_in_terminal.side_effect = run
            self.assertEqual(session._run_editor('/tmp/x'), 0)
        loop.call_soon_threadsafe.assert_called_once()
        self.assertTrue(editor.called)

    def test_an_editor_failure_is_a_terse_error(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        session, _client = make_session()
        loop = mock.MagicMock()
        loop.is_closed.return_value = False
        loop.call_soon_threadsafe.side_effect = lambda cb: cb()
        app = mock.MagicMock()
        app.loop = loop
        session._app = app
        with mock.patch('prompt_toolkit.application.run_in_terminal'
                        '.run_in_terminal') as run_in_terminal, \
                mock.patch.object(repl.Repl, '_run_editor',
                                  side_effect=OSError('no editor')):
            future = mock.MagicMock()

            def run(func):
                func()
                return future

            run_in_terminal.side_effect = run
            with self.assertRaises(repl.ReplError) as context:
                session._run_editor('/tmp/x')
        self.assertIn('the editor could not run: no editor',
                      str(context.exception))


class ConsoleQueueTabTestCase(unittest.TestCase):
    """The Queue tab: the queue's state rendered, the cursor, the
    job details, the removals."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-conqueue-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ,
                                  {'XDG_DATA_HOME': self.tmpdir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _job(self, job_id, state='queued'):
        return {'id': job_id, 'batch': 'work', 'mode': 'check',
                'targets': ['slow'], 'options': {}, 'view': 'system',
                'createdAt': '2026-10-25T00:00:00Z', 'state': state,
                'tally': {'taskCount': 3, 'hostCount': 1,
                          'estimateSeconds': 11},
                'executionId': 56 if state == 'running' else None,
                'error': None, 'position': 1, 'etaSeconds': 0}

    def _queue_session(self):
        """A console session with the queue scripted: one batch, a
        running job and a queued one."""
        from debops.mcp import console
        session, _client = make_session()
        attach_transcript(session)
        client = FakeClient()
        session.client = client
        # The refresh rides a side connection; the scripted client
        # clones into itself
        client.tail = client
        # The refresh asks queue.list, then batch.info per batch —
        # the scripted answers ride one call in order
        client.queue(
                'tools/call',
                {'resultType': 'complete', 'isError': False,
                 'structuredContent': {
                         'started': True,
                         'batches': [{'name': 'work', 'jobs': 2,
                                      'estimateSeconds': 22,
                                      'pending': False,
                                      'state': 'running'}],
                         'activeJob': None, 'lastFailure': None}},
                {'resultType': 'complete', 'isError': False,
                 'structuredContent': {'batch': {
                         'name': 'work', 'state': 'running',
                         'pending': False,
                         'createdAt': '2026-10-25T00:00:00Z',
                         'jobs': [self._job('job-1', 'running'),
                                  self._job('job-2')],
                         'estimateSeconds': 11}}})
        session._focus_area('queue')
        self.assertTrue(wait_for(lambda: session._queue_batches))
        return session, client

    def _rendered(self, session):
        from debops.mcp import console
        fragments = console._queue_tab_text(session)
        return ''.join(text for _style, text in fragments)

    def test_the_tab_renders_the_queue(self):
        session, _client = self._queue_session()
        text = self._rendered(session)
        self.assertIn('Queue: dispatching', text)
        self.assertIn('▾ work — 2 job(s)', text)
        self.assertIn('job-1: check slow [running]', text)
        self.assertIn('job-2: check slow [queued]', text)

    def test_the_tab_renders_the_active_run(self):
        session, _client = self._queue_session()
        session.active_run = {
                'run': {'mode': 'run', 'playbooks': ['site'],
                        'taskIndex': 2, 'taskCount': 50,
                        'currentTask': 'TASK [nginx : Install]'},
                'started': time.monotonic(), 'task_id': 't1'}
        text = self._rendered(session)
        self.assertIn('Running: running site │ 2/50', text)
        self.assertIn('TASK [nginx : Install]', text)

    def test_the_cursor_moves_and_the_detail_opens(self):
        session, _client = self._queue_session()
        self.assertEqual(session._queue_cursor, 0)
        session._queue_move(1)
        self.assertEqual(session._queue_cursor, 1)
        session._queue_move(5)  # clamped at the last row
        self.assertEqual(session._queue_cursor, 2)
        session._queue_open()
        self.assertIsNotNone(session._queue_detail)
        self.assertEqual(session._queue_detail['id'], 'job-2')
        from debops.mcp import console
        text = console._queue_detail_text(session)
        self.assertIn('job-2: check slow', text)
        self.assertIn('tally: 3 tasks x 1 hosts, ~11 s', text)
        session._queue_detail = None

    def test_a_batch_row_collapses(self):
        session, _client = self._queue_session()
        session._queue_open()   # the batch row: toggle
        text = self._rendered(session)
        self.assertIn('▸ work', text)
        self.assertNotIn('job-1', text)

    def test_the_removal_calls_and_refreshes(self):
        session, client = self._queue_session()
        client.tail = client   # the removal's side connection
        client.queue(
                # job.prune, then the refresh's queue.list
                'tools/call',
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text',
                              'text': 'removed: job-2'}]},
                {'resultType': 'complete', 'isError': False,
                 'structuredContent': {
                         'started': True, 'batches': [],
                         'activeJob': None, 'lastFailure': None}})
        session._queue_cursor = 2   # job-2
        session._queue_remove()
        self.assertTrue(wait_for(lambda: 'removed: job-2'
                                 in transcript_text(session)))
        self.assertIn(('tools/call',
                       {'name': 'job.prune',
                        'arguments': {'id': 'job-2'}}),
                      client.calls)
        self.assertTrue(wait_for(lambda: not session._queue_batches))


class ConsoleJournalTabTestCase(unittest.TestCase):
    """The Journal tab: the journalctl feed of the console's
    processes, switchable between the MCP pair, the user's journal
    and the system's."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-conjour-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ,
                                  {'XDG_DATA_HOME': self.tmpdir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, feed_lines=()):
        session, _client = make_session()
        attach_transcript(session)
        return session

    def _fake_proc(self, lines):
        proc = mock.MagicMock()
        proc.stdout = iter(list(lines))
        proc.wait.return_value = 0
        return proc

    def test_the_feed_lands_in_the_tab(self):
        session = self._session()
        proc = self._fake_proc(['paź 25 03:00 host debops-mcp[9]: '
                                'Tool check() -> task',
                                'paź 25 03:01 host debops-mcp[9]: done'])
        with mock.patch('debops.mcp.console.subprocess.Popen',
                        return_value=proc) as popen:
            session._journal_start()
            self.assertTrue(wait_for(
                    lambda: session._journal_note is not None
                    or len(session._journal_lines) >= 2))
        from debops.mcp import console
        text = console._journal_tab_text(session)
        self.assertIn('journal: mcp', text)
        self.assertIn('Tool check() -> task', text)
        # The MCP source follows both processes by pid
        command = popen.call_args[0][0]
        self.assertIn('--user', command)
        self.assertIn('--follow', command)
        self.assertIn('_PID={}'.format(os.getpid()), command)

    def test_the_daemon_pid_rides_the_socket_sidecar(self):
        session = self._session()
        socket_path = os.path.join(self.tmpdir, 'daemon.sock')
        with open(socket_path[:-5] + '.pid', 'w') as fp:
            fp.write('4321\n')
        session.client._path = socket_path
        proc = self._fake_proc([])
        with mock.patch('debops.mcp.console.subprocess.Popen',
                        return_value=proc) as popen:
            session._journal_start()
            self.assertTrue(wait_for(
                    lambda: session._journal_note is not None))
        command = popen.call_args[0][0]
        self.assertIn('_PID=4321', command)

    def test_the_source_switches(self):
        session = self._session()
        proc = self._fake_proc([])
        with mock.patch('debops.mcp.console.subprocess.Popen',
                        return_value=proc):
            session._journal_start()
            session._journal_switch()
            # The user's journal: no pid filter, still the user one
            command = session._journal_command()
            self.assertIn('--user', command)
            self.assertNotIn('_PID=', command)
            self.assertEqual(session._journal_source, 'user')
            session._journal_switch()
            command = session._journal_command()
            self.assertNotIn('--user', command)
            self.assertEqual(session._journal_source, 'system')
            session._journal_switch()
            self.assertEqual(session._journal_source, 'mcp')

    def test_a_missing_journalctl_is_a_note_not_a_crash(self):
        session = self._session()
        with mock.patch('debops.mcp.console.subprocess.Popen',
                        side_effect=FileNotFoundError('no journalctl')):
            session._journal_start()
        self.assertIn('journalctl', session._journal_note)
        self.assertIsNone(session._journal_proc)

    def test_the_feed_dies_with_the_session(self):
        session = self._session()
        # A live feed blocks its reader mid-stream
        gate = threading.Event()
        self.addCleanup(gate.set)

        def feed():
            yield 'a line'
            gate.wait(10)

        proc = self._fake_proc([])
        proc.stdout = feed()
        with mock.patch('debops.mcp.console.subprocess.Popen',
                        return_value=proc):
            session._journal_start()
        self.assertTrue(wait_for(lambda: session._journal_lines))
        self.assertIsNotNone(session._journal_proc)
        session._journal_stop()
        proc.terminate.assert_called_once_with()


class ConsoleRunsTabTestCase(unittest.TestCase):
    """The Runs tab: the finished executions listed; a run's log
    read whole and colored through the daemon's log.reader."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-conruns-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ,
                                  {'XDG_DATA_HOME': self.tmpdir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entry(self, execution_id, mode='check', playbooks=('slow',),
               exit_code=0):
        return {'id': execution_id, 'mode': mode,
                'playbooks': list(playbooks), 'hosts': ['web1'],
                'startedAt': '2026-10-25T00:00:00Z',
                'finishedAt': '2026-10-25T00:00:11Z',
                'exitCode': exit_code, 'status': 'done',
                'log': '/nonexistent/execution-{}.log'.format(
                        execution_id),
                'logBytes': 100}

    def _runs_session(self):
        """A console session with the Runs tab scripted: two
        finished executions."""
        session, _client = make_session()
        attach_transcript(session)
        client = FakeClient()
        session.client = client
        client.tail = client   # the side connections clone into it
        client.queue(
                'tools/call',  # execution.list
                {'resultType': 'complete', 'isError': False,
                 'structuredContent': [self._entry(4),
                                       self._entry(5, mode='run',
                                                   playbooks=('site',),
                                                   exit_code=2)]})
        session._focus_area('runs')
        self.assertTrue(wait_for(lambda: session._runs_entries))
        return session, client

    def test_the_tab_lists_the_runs(self):
        from debops.mcp import console
        session, _client = self._runs_session()
        fragments = console._runs_tab_text(session)
        text = ''.join(t for _s, t in fragments)
        self.assertIn('4: check slow — exit 0, 11 s', text)
        self.assertIn('5: run site — exit 2, 11 s', text)

    def test_the_log_opens_colorized_and_closes(self):
        from debops.mcp import console
        session, client = self._runs_session()
        client.queue(
                'tools/call',  # log.reader
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text',
                              'text': 'PLAY [x]\n'
                                      'changed: [web1]\n'
                                      'ok: [web1]'}]})
        session._runs_cursor = 1
        session._runs_open()
        self.assertTrue(wait_for(
                lambda: session._runs_detail is not None))
        # The log rode the socket, not the cache file
        self.assertIn(('tools/call',
                       {'name': 'log.reader', 'arguments': {'id': 5}}),
                      client.calls)
        detail = session._runs_detail
        self.assertEqual(detail['id'], 5)
        # The colorizer applied (ANSI codes ride the lines)
        self.assertIn('\x1b[', ''.join(detail['lines']))
        text = console._runs_detail_text(session)
        self.assertIn('execution 5 — 3 lines', text)
        # Three lines fit the area: scrolling is a no-op
        session._runs_scroll_by(1)
        self.assertEqual(session._runs_scroll, 0)
        # A longer log scrolls (the area's height is the
        # environment's own headless; pin it for the test)
        session._runs_detail['lines'] = detail['lines'] * 8
        with mock.patch.object(session, '_area_height',
                               return_value=5):
            session._runs_scroll_by(3)
            self.assertEqual(session._runs_scroll, 3)
        session._runs_close()
        self.assertIsNone(session._runs_detail)
        self.assertEqual(session._runs_scroll, 0)

    def test_the_log_read_failure_lands_as_a_note(self):
        session, client = self._runs_session()
        client.queue(
                'tools/call',  # log.reader errors
                {'resultType': 'complete', 'isError': True,
                 'content': [{'type': 'text',
                              'text': 'no such execution: 99'}]})
        session._runs_cursor = 0
        session._runs_open()
        self.assertTrue(wait_for(lambda: 'no such execution'
                                 in transcript_text(session)))
        self.assertIsNone(session._runs_detail)


class ConsoleBuildTestCase(unittest.TestCase):

    def setUp(self):
        # Building the interface reads the command history; keep the
        # session's files in a temporary data home, never the user's
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-console-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ,
                                  {'XDG_DATA_HOME': self.tmpdir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def build_sized_app(self, session, rows=10, cols=30):
        """Build the console headless, pinned to a fixed terminal
        size."""
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output import DummyOutput
        from debops.mcp import console

        class SmallOutput(DummyOutput):
            def get_size(self):
                return Size(rows=rows, columns=cols)

        app = console._build_app(session)
        app.renderer.output = SmallOutput()
        app.output = SmallOutput()
        return app

    def render_app(self, app, rows=10, cols=30):
        """Render one frame; return the screen's rows as text and
        the window positions. Emulates a live frame: the app is the
        current one and the render counter moves, so the controls
        re-evaluate their content (they cache per frame)."""
        from prompt_toolkit.application.current import set_app
        app.render_counter += 1
        with set_app(app):
            app.renderer.render(app, app.layout)
        screen = app.renderer.last_rendered_screen
        lines = [''.join(
                screen.data_buffer.get(r, {}).get(c).char
                if c in screen.data_buffer.get(r, {}) else ' '
                for c in range(cols)).rstrip()
                for r in range(rows)]
        return lines, screen.visible_windows_to_write_positions

    def _key_binding(self, app, *keys):
        # The key processor's own rule: the first binding whose
        # filter passes, most recently registered first
        matches = [b for b in app.key_bindings.bindings
                   if b.keys == keys]
        for binding in matches:
            filter_ = binding.filter
            try:
                passes = filter_() if callable(filter_) else bool(
                        filter_)
            except Exception:
                passes = False
            if passes:
                return binding.handler
        return matches[0].handler

    def test_the_application_builds_headless(self):
        try:
            from prompt_toolkit.application import Application
            from prompt_toolkit.layout.containers import (
                    FloatContainer)
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import console
        session, _client = make_session()
        app = console._build_app(session)
        self.assertIsInstance(app, Application)
        self.assertIsInstance(app.layout.container, FloatContainer)
        self.assertIsNotNone(session._input_buffer)
        self.assertEqual(len(session._tab_controls), 6)

    def test_the_chrome_layout(self):
        # The title on top; the transcript, the prompt and the
        # status line hugging the bottom; the tab area hidden
        from debops.mcp import console
        session, _client = make_session()
        app = self.build_sized_app(session, rows=12, cols=40)
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertTrue(lines[0].startswith('DebOps'), lines)
        self.assertRegex(lines[0].rstrip()[-5:],
                         r'\d{2}:\d{2}')
        self.assertEqual(lines[10], '>')
        self.assertIn('project:system', lines[11])
        self.assertNotIn('Queue │', '\n'.join(lines))

    def test_the_title_bangs_when_elevated(self):
        from debops.mcp import console
        session, _client = make_session()
        app = self.build_sized_app(session, rows=12, cols=40)
        session.elevated = True
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertTrue(lines[0].startswith('DebOps!'), lines)

    def test_the_input_line_carries_the_chip(self):
        from debops.mcp import console
        session, _client = make_session()
        app = self.build_sized_app(session, rows=12, cols=40)
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertEqual(lines[10], '>')
        session.confined = True
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertEqual(lines[10], '[>]')

    def test_the_tab_area_dances(self):
        from debops.mcp import console
        session, _client = make_session()
        app = self.build_sized_app(session, rows=16, cols=40)
        session._app = app
        event = mock.MagicMock()
        # Escape leaves the prompt for the area, opened on the Queue
        self._key_binding(app, 'escape')(event)
        self.assertEqual(session.console_focus, 'area')
        self.assertEqual(session.console_tab, 'queue')
        lines, _positions = self.render_app(app, rows=16, cols=40)
        self.assertIn('Queue', lines[1])
        # The Queue tab renders its state (nothing fetched headless)
        self.assertIn('Queue: …', lines[2])
        # Tab cycles the tabs, Shift-Tab back
        self._key_binding(app, 'c-i')(event)
        self.assertEqual(session.console_tab, 'runs')
        self._key_binding(app, 's-tab')(event)
        self.assertEqual(session.console_tab, 'queue')
        # The F-keys select; the open tab's key closes the area
        self._key_binding(app, 'f3')(event)
        self.assertEqual(session.console_tab, 'runs')
        self._key_binding(app, 'f3')(event)
        self.assertIsNone(session.console_tab)
        self.assertEqual(session.console_focus, 'prompt')
        # 'i' returns to the prompt; 'C-x o' toggles
        session._focus_area('journal')
        self._key_binding(app, 'i')(event)
        self.assertEqual(session.console_focus, 'prompt')
        self.assertEqual(session.console_tab, 'journal')
        self._key_binding(app, 'c-x', 'o')(event)
        self.assertEqual(session.console_focus, 'area')
        # ... and Escape declines a pending gate first
        session._gate = {'secret': False,
                         'answered': threading.Event(),
                         'value': None}
        session._focus_prompt()
        self._key_binding(app, 'escape')(event)
        # The gate got its 'no'; the focus stayed on the prompt
        self.assertTrue(session._gate['answered'].is_set())
        self.assertIs(session._gate['value'], False)
        self.assertEqual(session.console_focus, 'prompt')

    def test_the_prompt_inactive_default_opens_the_area(self):
        from debops.mcp import console
        session, _client = make_session(
                console_config={'prompt_active': False})
        app = self.build_sized_app(session, rows=16, cols=40)
        # The area opens on the Queue and holds the focus
        self.assertEqual(session.console_tab, 'queue')
        self.assertIs(app.layout.current_control,
                      session._tab_controls['queue'])
        lines, _positions = self.render_app(app, rows=16, cols=40)
        self.assertIn('Queue', lines[1])

    def test_the_transcript_shows_the_tail_slice(self):
        from debops.mcp import console
        session, _client = make_session()
        app = self.build_sized_app(session, rows=12, cols=40)
        session._echo(''.join('line{:02d}\n'.format(i)
                              for i in range(10)))
        lines, _positions = self.render_app(app, rows=12, cols=40)
        # The window is five lines of the tail; the scrollback holds
        # the rest
        self.assertEqual(lines[5], 'line05')
        self.assertEqual(lines[9], 'line09')
        self.assertEqual(len(session._console_lines), 10)

    def test_the_transcript_scroll_holds_the_position(self):
        from debops.mcp import console
        session, _client = make_session()
        app = self.build_sized_app(session, rows=12, cols=40)
        session._echo(''.join('line{:02d}\n'.format(i)
                              for i in range(10)))
        event = mock.MagicMock()
        self._key_binding(app, 'pageup')(event)
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertEqual(lines[5], 'line00')
        self.assertEqual(lines[9], 'line04')
        # New output while paged up holds the viewport
        session._echo('new\n')
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertEqual(lines[5], 'line00')
        # ... and End snaps back to the tail
        self._key_binding(app, 'end')(event)
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertEqual(lines[9], 'new')

    def test_a_run_tails_into_the_transcript(self):
        # The run's live log streams into the transcript, colored
        session, _client = make_session()
        attach_transcript(session)
        client = FakeClient()
        client.tail = FakeTailConnection(['ok: [localhost]'])
        session.client = client
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1', 'status': 'working'})
        client.queue('tasks/get',
                     {'status': 'working',
                      'run': {'mode': 'run', 'playbooks': ['site'],
                              'executionId': 7, 'taskIndex': 1}},
                     {'status': 'completed',
                      'result': {'isError': False,
                                 'structuredContent': {'exitCode': 0},
                                 'content': []}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            session.execute('run site')
        self.assertIn('ok: [localhost]', transcript_text(session))
        self.assertEqual(client.tail.subscriptions, [{'runLog': 7}])

    def test_the_secret_gate_masks_the_input_line(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        session, _client = make_session()
        app = self.build_sized_app(session, rows=12, cols=40)
        errors = []
        thread = threading.Thread(
                target=lambda: errors.append(
                        passphrase_error(session)), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        app.layout.current_buffer.text = 'supersecret'
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertIn('*' * 11, lines[10])
        self.assertNotIn('supersecret', '\n'.join(lines))
        session._gate_decline()
        thread.join(timeout=5)
        self.assertEqual(errors, ['cancelled'])
        # The mask follows the gate, not the buffer content
        lines, _positions = self.render_app(app, rows=12, cols=40)
        self.assertIn('supersecret', lines[10])

    def test_the_console_runs_headless_and_exits(self):
        try:
            from prompt_toolkit.application import (
                    create_app_session)
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output import DummyOutput
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import console
        session, _client = make_session()
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                app = console._build_app(session)
                session._app = app
                session._echo(console._banner(session) + '\n')
                pipe.send_text('exit\r')
                thread = threading.Thread(target=app.run, daemon=True)
                thread.start()
                thread.join(timeout=10)
                if thread.is_alive():
                    app.exit()
                    thread.join(timeout=5)
                    self.fail('the console did not exit')
        self.assertIn('debops mcp repl', transcript_text(session))
        self.assertIn('> exit', transcript_text(session))

    def test_commands_are_recorded_in_the_shared_history(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import console
        session, _client = make_session()
        # A project session records in the project-and-view file
        session.project_path = os.path.join(self.tmpdir, 'project')
        app = console._build_app(session)
        buffer = app.layout.current_buffer
        buffer.text = 'status'
        buffer.validate_and_handle()
        # Recorded, and the line cleared for the next command
        self.assertIn('status', buffer.history.get_strings())
        self.assertEqual(buffer.text, '')
        self.assertTrue(session.history_file.endswith(
                '-project-system'))
        with open(session.history_file) as fp:
            self.assertIn('+status', fp.read())

    def test_the_console_loads_the_shared_history(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from prompt_toolkit.history import FileHistory
        from debops.mcp import console
        session, _client = make_session()
        session.project_path = os.path.join(self.tmpdir, 'project')
        # As written by another front end (the repl, the shell)
        with open(session.history_file, 'w') as fp:
            fp.write('\n# 2026-10-23 10:00:00.000000\n'
                     '+run containers\n')
        app = console._build_app(session)
        history = app.layout.current_buffer.history
        self.assertIsInstance(history, FileHistory)
        self.assertEqual(history.filename, session.history_file)
        self.assertEqual(list(history.load_history_strings()),
                         ['run containers'])

    def test_gate_answers_stay_out_of_the_history(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import console
        session, _client = make_session()
        app = console._build_app(session)
        buffer = app.layout.current_buffer
        for secret in (True, False):
            session._gate = {'secret': secret,
                             'answered': threading.Event(),
                             'value': None}
            buffer.text = 'supersecret'
            buffer.validate_and_handle()
            session._gate = None
        self.assertNotIn('supersecret', buffer.history.get_strings())
        if os.path.exists(session.history_file):
            with open(session.history_file) as fp:
                self.assertNotIn('supersecret', fp.read())

    def test_blank_lines_stay_out_of_the_history(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import console
        session, _client = make_session()
        app = console._build_app(session)
        buffer = app.layout.current_buffer
        buffer.text = '   '
        buffer.validate_and_handle()
        self.assertEqual(buffer.history.get_strings(), [])

    def test_the_history_follows_the_view_switch(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from prompt_toolkit.history import FileHistory
        from debops.mcp import console
        session, client = make_session()
        session.project_path = os.path.join(self.tmpdir, 'project')
        console._build_app(session)
        # The initial file is the startup view's
        self.assertIsInstance(session._input_buffer.history,
                              FileHistory)
        self.assertTrue(session._input_buffer.history.filename
                        .endswith('-project-system'))
        # The answer carries the daemon's current view, as the
        # server's '_meta' always does
        client.request.return_value = {'result': {
                'resultType': 'complete', 'isError': False,
                'content': [], '_meta': {'org.debops/view': 'admin'}}}
        session.execute('view admin')
        self.assertTrue(session._input_buffer.history.filename
                        .endswith('-project-admin'))
        self.assertEqual(session._input_buffer.history.filename,
                         session.history_file)

    def test_the_console_ctrl_z(self):
        try:
            from prompt_toolkit.key_binding import KeyBindings
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import console
        session, _client = make_session()
        attach_transcript(session)
        bindings = KeyBindings()
        console._bind_suspend(bindings, session)
        handler = bindings.bindings[0].handler
        event = mock.MagicMock()
        # A free account: the process backgrounds
        handler(event)
        event.app.suspend_to_background.assert_called_once_with()
        # A confined one: a note, no background
        session.confined = True
        event.app.suspend_to_background.reset_mock()
        handler(event)
        self.assertFalse(event.app.suspend_to_background.called)
        self.assertIn('a confined session does not background',
                      transcript_text(session))

    def test_the_console_falls_back_without_prompt_toolkit(self):
        from debops.mcp import console
        session, _client = make_session()
        with mock.patch.dict(sys.modules, {'prompt_toolkit': None}):
            with mock.patch.object(console, 'run_interactive_plain',
                                   return_value=0) as plain:
                code = console.run_console(session)
        self.assertEqual(code, 0)
        self.assertTrue(plain.called)

    def test_ctrl_home_end_edit_the_input_line(self):
        # Home/End page the scrollback; the line's start and end
        # keep a chord of their own
        from debops.mcp import console
        session, _client = make_session()
        app = console._build_app(session)
        buffer = session._input_buffer
        buffer.text = 'some command'
        buffer.cursor_position = 4
        event = mock.MagicMock()
        event.current_buffer = buffer
        self._key_binding(app, 'c-home')(event)
        self.assertEqual(buffer.document.cursor_position, 0)
        self._key_binding(app, 'c-end')(event)
        self.assertEqual(buffer.document.cursor_position, 12)

    def test_run_dispatches_the_console_session(self):
        client = mock.MagicMock()
        with mock.patch('debops.mcp.console.run_console',
                        return_value=0) as console_run:
            code = repl.run(client, project_name='project',
                            interactive=True, frontend='console')
        self.assertEqual(code, 0)
        self.assertTrue(console_run.called)
        session = console_run.call_args[0][0]
        self.assertIsInstance(session, ConsoleRepl)

    def test_a_pipe_stays_a_pipe(self):
        client = mock.MagicMock()
        with mock.patch.object(repl, 'run_pipe', return_value=0) \
                as pipe:
            code = repl.run(client, project_name='project',
                            interactive=False, frontend='console')
        self.assertEqual(code, 0)
        self.assertTrue(pipe.called)


if __name__ == '__main__':
    unittest.main()
