# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the 'mcp shell' interface.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import io
import sys
import unittest

from unittest import mock

from debops.mcp import repl
from debops.mcp.repl import Repl
from debops.mcp import shell


def make_session(elevated=False):
    client = mock.MagicMock()
    client.bearer = None
    out, err = io.StringIO(), io.StringIO()
    session = Repl(client, project_name='project', view='system',
                   out=out, err=err, project_type='legacy',
                   view_root_rel='ansible')
    session.elevated = elevated
    return session, client, out, err


def run_shell_once(session, lines=()):
    """Run the shell with a mocked prompt_toolkit session feeding the
    lines, then EOF."""
    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        raise unittest.SkipTest('prompt_toolkit is not installed')
    with mock.patch('prompt_toolkit.PromptSession') as pt_session:
        pt_session.return_value.prompt.side_effect = (
                list(lines) + [EOFError()])
        code = shell.run_shell(session)
    return code, pt_session


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
        code, pt_session = run_shell_once(session)
        self.assertEqual(code, 0)
        _args, kwargs = pt_session.call_args
        self.assertIn('bottom_toolbar', kwargs)
        self.assertIn('style', kwargs)
        pt_session.return_value.prompt.assert_called_with('debops> ')

    def test_the_shell_prompt_marks_the_elevation(self):
        session, _client, _out, _err = make_session(elevated=True)
        _code, pt_session = run_shell_once(session)
        pt_session.return_value.prompt.assert_called_with('debops!> ')

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

    def test_a_terminal_gets_the_shell(self):
        client = mock.MagicMock()
        with mock.patch('debops.mcp.shell.run_shell',
                        return_value=0) as shell_run:
            code = repl.run(client, project_name='project',
                            interactive=True, frontend='shell')
        self.assertEqual(code, 0)
        self.assertTrue(shell_run.called)


if __name__ == '__main__':
    unittest.main()
