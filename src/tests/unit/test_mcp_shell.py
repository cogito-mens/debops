# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the 'mcp shell' interface.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from unittest import mock

from debops.mcp import repl
from debops.mcp.repl import Repl
from debops.mcp import shell


def make_session(elevated=False):
    client = mock.MagicMock()
    client.legacy = False
    client.bearer = None
    out, err = io.StringIO(), io.StringIO()
    session = Repl(client, project_name='project', view='system',
                   out=out, err=err, project_type='legacy',
                   view_root_rel='ansible')
    session.elevated = elevated
    return session, client, out, err


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeWatcher(object):
    """A scripted watch connection: yields the queued lines, then
    holds the stream open until closed."""

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.closed = False
        self._gate = threading.Event()

    def request_streaming(self, method, params):
        for line in self.lines:
            yield {'method': 'notifications/activity',
                   'params': {'line': line}}
        self._gate.wait(5)

    def close(self):
        self.closed = True
        self._gate.set()


def run_shell_once(session, lines=()):
    """Run the shell with a mocked prompt_toolkit session: the lines
    go through the accept handler (the worker picks them up), then
    EOF ends it."""
    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        raise unittest.SkipTest('prompt_toolkit is not installed')
    with mock.patch('prompt_toolkit.PromptSession') as pt_class:
        prompt_session = pt_class.return_value

        def prompt():
            buffer = prompt_session.default_buffer
            for line in lines:
                buffer.text = line
                buffer.accept_handler(buffer)
            raise EOFError()

        prompt_session.prompt.side_effect = prompt
        code = shell.run_shell(session)
        # The commands run on the worker thread; wait them out
        wait_for(lambda: not session._busy)
    return code, pt_class


class ShellTestCase(unittest.TestCase):

    def test_the_toolbar_pads_across_the_width(self):
        session, _client, _out, _err = make_session()
        session.cwd = 'ansible/inventory/group_vars/all'
        app = mock.MagicMock()
        app.output.get_size.return_value.columns = 40
        with mock.patch('prompt_toolkit.application.get_app',
                        return_value=app):
            text = shell._toolbar(session)
        self.assertEqual(len(text), 40)
        self.assertTrue(text.startswith('project:system/i/g/all/'))
        self.assertTrue(text.endswith(' '))

    def test_the_toolbar_carries_the_trust_state(self):
        session, client, _out, _err = make_session()
        client.bearer = 'the-token'
        app = mock.MagicMock()
        app.output.get_size.return_value.columns = 30
        with mock.patch('prompt_toolkit.application.get_app',
                        return_value=app):
            text = shell._toolbar(session)
        self.assertTrue(text.endswith('trusted'))

    def test_the_shell_uses_the_toolbar_and_the_short_prompt(self):
        session, _client, _out, _err = make_session()
        code, pt_class = run_shell_once(session)
        self.assertEqual(code, 0)
        _args, kwargs = pt_class.call_args
        self.assertIn('bottom_toolbar', kwargs)
        self.assertIn('style', kwargs)
        # The prompt marker is dynamic (the elevation shows on it)
        message = kwargs['message']
        self.assertEqual(message(), 'debops> ')

    def test_the_shell_prompt_marks_the_elevation(self):
        session, _client, _out, _err = make_session(elevated=True)
        _code, pt_class = run_shell_once(session)
        message = pt_class.call_args[1]['message']
        self.assertEqual(message(), 'debops!> ')

    def test_the_shell_restores_the_session_channels(self):
        session, _client, out, err = make_session()
        code, _pt_session = run_shell_once(session)
        self.assertEqual(code, 0)
        # The banner printed before the re-bind; the channels are
        # the session's own again afterwards
        self.assertIn('debops mcp repl', err.getvalue())
        self.assertIs(session._out, out)
        self.assertIs(session._err, err)

    def test_the_shell_runs_commands(self):
        session, client, _out, _err = make_session()
        client.request.return_value = {'result': {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}]}}
        code, _pt_session = run_shell_once(session, lines=['status'])
        self.assertEqual(code, 0)
        client.request.assert_called()

    def test_the_shell_falls_back_without_prompt_toolkit(self):
        session, _client, _out, _err = make_session()
        with mock.patch.dict(sys.modules, {'prompt_toolkit': None}):
            with mock.patch.object(shell, 'run_interactive_plain',
                                   return_value=0) as plain:
                code = shell.run_shell(session)
        self.assertEqual(code, 0)
        self.assertTrue(plain.called)

    def test_a_pipe_stays_a_pipe(self):
        # 'mcp shell < commands' never builds the chrome
        client = mock.MagicMock()
        with mock.patch.object(repl, 'run_pipe', return_value=0) \
                as pipe:
            code = repl.run(client, project_name='project',
                            interactive=False, frontend='shell')
        self.assertEqual(code, 0)
        self.assertTrue(pipe.called)

    def test_the_history_follows_the_view_switch(self):
        try:
            from prompt_toolkit.history import FileHistory
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        tmpdir = tempfile.mkdtemp(prefix='debops-mcp-history-')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        session, _client, _out, _err = make_session()
        session.project_path = os.path.join(tmpdir, 'project')
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': tmpdir}):
            _code, pt_session = run_shell_once(session)
            _args, kwargs = pt_session.call_args
            self.assertTrue(kwargs['history'].filename.endswith(
                    '-project-system'))
            # The hook re-points the buffer at the current view's
            # file
            session.view = 'admin'
            session._history_changed()
            history = pt_session.return_value.default_buffer.history
            self.assertIsInstance(history, FileHistory)
            self.assertTrue(history.filename.endswith(
                    '-project-admin'))

    def test_suspend_enabled_unless_confined(self):
        for confined, expected in ((False, True), (True, False)):
            session, _client, _out, _err = make_session()
            session.confined = confined
            _code, pt_session = run_shell_once(session)
            _args, kwargs = pt_session.call_args
            self.assertIs(kwargs['enable_suspend'], expected)

    def test_a_terminal_gets_the_shell(self):
        client = mock.MagicMock()
        with mock.patch('debops.mcp.shell.run_shell',
                        return_value=0) as shell_run:
            code = repl.run(client, project_name='project',
                            interactive=True, frontend='shell')
        self.assertEqual(code, 0)
        self.assertTrue(shell_run.called)


class BackgroundWatchTestCase(unittest.TestCase):
    """The shell's background watch: a reader thread on its own
    connection, the lines printed above the prompt."""

    def make_watching_session(self, watcher):
        session, client, out, err = make_session()
        client.bearer = 'the-token'
        client.clone.return_value = watcher
        session.background_watch = True
        return session, client, out, err

    def test_the_watch_runs_in_the_background(self):
        watcher = FakeWatcher(lines=['run #1 finished'])
        session, client, out, err = self.make_watching_session(
                watcher)
        session.execute('watch')
        # The command returned at once; the lines print as they come
        self.assertTrue(wait_for(lambda: 'run #1 finished'
                                 in out.getvalue()))
        self.assertIn('in the background', err.getvalue())
        self.assertIsNotNone(session._watch_thread)
        # The clone carries the trust
        client.clone.assert_called_once_with()
        # Toggling stops it
        session.execute('watch')
        self.assertTrue(watcher.closed)
        self.assertIsNone(session._watch_thread)
        self.assertIn('watch ended', err.getvalue())

    def test_the_watch_stream_dying_notes_it(self):
        class DyingWatcher(object):
            def __init__(self):
                self.closed = False

            def request_streaming(self, method, params):
                raise ConnectionError('gone')
                yield

            def close(self):
                self.closed = True

        watcher = DyingWatcher()
        session, _client, _out, err = self.make_watching_session(
                watcher)
        session.execute('watch')
        self.assertTrue(wait_for(lambda: 'watch ended: gone'
                                 in err.getvalue()))
        self.assertIsNone(session._watch_thread)
        self.assertTrue(watcher.closed)

    def test_the_watch_needs_the_trust(self):
        session, client, _out, _err = make_session()
        client.bearer = None
        session.background_watch = True
        # A pipe cannot ask for the passphrase
        with self.assertRaises(repl.ReplError) as context:
            session.execute('watch')
        self.assertIn('trust passphrase', str(context.exception))
        self.assertIsNone(session._watch_thread)

    def test_the_shell_stops_the_watch_on_exit(self):
        session, _client, _out, _err = make_session()
        watcher = FakeWatcher()
        session._watch_client = watcher
        session._watch_thread = mock.MagicMock()
        code, _pt_session = run_shell_once(session)
        self.assertEqual(code, 0)
        self.assertTrue(watcher.closed)
        self.assertIsNone(session._watch_thread)

    def test_the_foreground_watch_stays_in_the_plain_repl(self):
        # The engine keeps the Ctrl-C flow when no front end opted
        # into the background watch
        session, client, _out, err = make_session()
        client.bearer = 'the-token'
        client.request_streaming.side_effect = KeyboardInterrupt()
        session.execute('watch')
        self.assertIn('watch ended', err.getvalue())
        self.assertIsNone(session._watch_thread)


if __name__ == '__main__':
    unittest.main()
