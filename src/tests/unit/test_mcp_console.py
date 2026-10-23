# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the 'mcp console' interface.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import sys
import threading
import time
import unittest

from unittest import mock

from debops.mcp import repl
from debops.mcp.console import ConsoleRepl


def make_session():
    client = mock.MagicMock()
    client.legacy = False
    client.bearer = None
    client.request.return_value = {'result': {
            'resultType': 'complete', 'isError': False,
            'content': [{'type': 'text', 'text': 'Project: x'}]}}
    session = ConsoleRepl(client, project_name='project', view='system',
                          project_type='legacy', view_root_rel='ansible')
    return session, client


def attach_buffers(session):
    """The prompt_toolkit buffers the interface build attaches."""
    try:
        from prompt_toolkit.buffer import Buffer
    except ImportError:
        raise unittest.SkipTest('prompt_toolkit is not installed')
    from debops.mcp.console import _BufferWriter
    session._scroll_buffer = Buffer()
    session._activity_buffer = Buffer()
    writer = _BufferWriter(session, session._scroll_buffer)
    session._out = writer
    session._err = writer


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ConsoleSessionTestCase(unittest.TestCase):

    def test_the_scrollback_collects_the_session(self):
        session, _client = make_session()
        attach_buffers(session)
        session.submit('status')
        self.assertTrue(wait_for(lambda: not session._busy))
        text = session._scroll_buffer.text
        self.assertIn('debops> status', text)
        self.assertIn('Project: x', text)

    def test_the_gate_is_answered_on_the_input_line(self):
        session, _client = make_session()
        attach_buffers(session)
        answers = []
        thread = threading.Thread(
                target=lambda: answers.append(
                        session._gate_ask('apply the changes? [y/N] ')),
                daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        # The question is visible; the input line answers it
        self.assertIn('apply the changes?',
                      session._scroll_buffer.text)
        session.submit('y')
        thread.join(timeout=5)
        self.assertEqual(answers, [True])
        self.assertIsNone(session._gate)

    def test_the_gate_decline(self):
        session, _client = make_session()
        attach_buffers(session)
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
        attach_buffers(session)
        answers = []
        thread = threading.Thread(
                target=lambda: answers.append(
                        session._ask_passphrase()), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        session.submit('correct horse battery staple')
        thread.join(timeout=5)
        self.assertEqual(answers, ['correct horse battery staple'])
        self.assertNotIn('correct horse', session._scroll_buffer.text)

    def test_the_passphrase_gate_cancelled(self):
        session, _client = make_session()
        attach_buffers(session)
        errors = []
        thread = threading.Thread(
                target=lambda: errors.append(
                        self._passphrase_error(session)), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: session._gate is not None))
        session._gate_decline()
        thread.join(timeout=5)
        self.assertEqual(errors, ['cancelled'])

    def _passphrase_error(self, session):
        try:
            session._ask_passphrase()
        except repl.ReplError as errmsg:
            return str(errmsg)

    def test_submit_serializes_the_commands(self):
        session, _client = make_session()
        attach_buffers(session)
        session._busy = True
        session.submit('status')
        self.assertIn('a command is running',
                      session._scroll_buffer.text)

    def test_the_watch_lands_in_the_activity_pane(self):
        session, _client = make_session()
        attach_buffers(session)
        session._watch_line('run #1 finished')
        session._watch_line('run #2 running')
        self.assertEqual(session._activity_buffer.text,
                         'run #1 finished\nrun #2 running\n')
        # The scrollback is not the feed
        self.assertNotIn('run #1', session._scroll_buffer.text)

    def test_the_watch_line_without_the_interface_prints(self):
        session, _client = make_session()
        # No buffers attached: the engine's default (a print) —
        # nothing crashes
        session._out = mock.MagicMock()
        session._watch_line('a line')
        session._out.write.assert_called()

    def test_the_panel_cycle(self):
        session, client = make_session()
        attach_buffers(session)
        client.request.return_value = {'result': {
                'resultType': 'complete', 'isError': False,
                'content': [],
                'structuredContent': [{'name': 'inventory',
                                       'type': 'dir'},
                                      {'name': 'note.txt',
                                       'type': 'file'}]}}
        self.assertEqual(session.pane, 'off')
        session.cycle_pane()
        self.assertEqual(session.pane, 'tree')
        self.assertEqual(session._tree_lines,
                         ['inventory/', 'note.txt'])
        session.cycle_pane()
        self.assertEqual(session.pane, 'activity')
        session.cycle_pane()
        self.assertEqual(session.pane, 'off')

    def test_the_panel_verb(self):
        session, _client = make_session()
        attach_buffers(session)
        session.execute('panel activity')
        self.assertEqual(session.pane, 'activity')
        with self.assertRaises(repl.ReplError) as context:
            session.execute('panel sideways')
        self.assertIn('usage: panel', str(context.exception))

    def test_the_tree_pane_refreshes_after_a_command(self):
        session, client = make_session()
        attach_buffers(session)
        client.request.side_effect = [
                {'result': {'resultType': 'complete', 'isError': False,
                            'content': []}},
                {'result': {'resultType': 'complete', 'isError': False,
                            'content': [],
                            'structuredContent': [
                                    {'name': 'new', 'type': 'dir'}]}},
        ]
        session.pane = 'tree'
        session.submit('status')
        self.assertTrue(wait_for(lambda: not session._busy))
        self.assertEqual(session._tree_lines, ['new/'])

    def test_exit_leaves_the_application(self):
        session, _client = make_session()
        attach_buffers(session)
        app = mock.MagicMock()
        session._app = app
        session.submit('exit')
        self.assertTrue(wait_for(lambda: app.exit.called))
        self.assertFalse(session._busy)


class ConsoleBuildTestCase(unittest.TestCase):

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
        self.assertIsNotNone(session._scroll_buffer)
        self.assertIsNotNone(session._activity_buffer)

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
        self.assertIn('debops mcp repl', session._scroll_buffer.text)
        self.assertIn('debops> exit', session._scroll_buffer.text)

    def test_the_console_falls_back_without_prompt_toolkit(self):
        from debops.mcp import console
        session, _client = make_session()
        with mock.patch.dict(sys.modules, {'prompt_toolkit': None}):
            with mock.patch.object(console, 'run_interactive_plain',
                                   return_value=0) as plain:
                code = console.run_console(session)
        self.assertEqual(code, 0)
        self.assertTrue(plain.called)

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
