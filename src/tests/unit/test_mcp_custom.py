# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the custom script tools from '.debops/mcp/tools/'.

Written with 'unittest' so that the suite runs under pytest without
extra dependencies. The scripts run for real; they are tiny POSIX
shell one-liners.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, RequestCancelled, ToolContext
from debops.mcp.tools import register_project_tools
from debops.mcp.tools.custom import (discover_custom_tools,
                                     make_custom_tool)
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


def _auto_commit_noop(self, message=None):
    pass


def make_project(path, raw_config=None):
    return type('Project', (), {
        'path': path,
        'name': 'project',
        'project_type': 'legacy',
        'view': 'system',
        'auto_commit': _auto_commit_noop,
        'config': type('Config', (), {'_env_vars': {},
                                      'raw': raw_config or {}})(),
    })()


CUSTOM_CONFIG = {'project': {'mcp': {'custom_tools': True}}}


def write_tool(tools_dir, name, body='#!/bin/sh\ncat\n', mode=0o755):
    path = os.path.join(tools_dir, name)
    with open(path, 'w') as fp:
        fp.write(body)
    os.chmod(path, mode)
    return path


class DiscoveryTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-custom-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.tools_dir = os.path.join(self.tmpdir, '.debops', 'mcp',
                                      'tools')
        os.makedirs(self.tools_dir)
        self.project = make_project(self.tmpdir)

    def test_missing_directory_discovers_nothing(self):
        shutil.rmtree(self.tools_dir)
        self.assertEqual(discover_custom_tools(self.project), {})

    def test_discovers_executable_scripts_with_descriptions(self):
        write_tool(self.tools_dir, 'hello-world',
                   body='#!/bin/sh\n# description: Say hello\ncat\n')
        write_tool(self.tools_dir, 'no-description')
        found = discover_custom_tools(self.project)
        self.assertEqual(sorted(found),
                         ['hello-world', 'no-description'])
        path, description = found['hello-world']
        self.assertEqual(description, 'Say hello')
        self.assertEqual(found['no-description'][1], '')

    def test_skips_non_executable_files(self):
        write_tool(self.tools_dir, 'not-executable', mode=0o644)
        self.assertEqual(discover_custom_tools(self.project), {})

    def test_skips_invalid_names(self):
        write_tool(self.tools_dir, 'Bad Name')
        write_tool(self.tools_dir, '.hidden')
        self.assertEqual(discover_custom_tools(self.project), {})

    def test_skips_symlinks(self):
        target = write_tool(self.tools_dir, 'real-tool')
        os.symlink(target, os.path.join(self.tools_dir, 'linked-tool'))
        found = discover_custom_tools(self.project)
        self.assertEqual(sorted(found), ['real-tool'])

    def test_deterministic_order(self):
        for name in ('zeta', 'alpha', 'mid'):
            write_tool(self.tools_dir, name)
        self.assertEqual(list(discover_custom_tools(self.project)),
                         ['alpha', 'mid', 'zeta'])


class PolicyToggleTestCase(unittest.TestCase):

    def test_disabled_by_default(self):
        policy = McpPolicy(make_project('/srv/projects/example'))
        self.assertFalse(policy.custom_tools)

    def test_string_value(self):
        policy = McpPolicy(make_project(
                '/srv/projects/example',
                {'project': {'mcp': {'custom_tools': 'true'}}}))
        self.assertTrue(policy.custom_tools)


class RegistrationTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-custom-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.tools_dir = os.path.join(self.tmpdir, '.debops', 'mcp',
                                      'tools')
        os.makedirs(self.tools_dir)
        write_tool(self.tools_dir, 'hello-world',
                   body='#!/bin/sh\n# description: Say hello\ncat\n')

    def _server(self, raw_config=None):
        project = make_project(self.tmpdir, raw_config)
        policy = McpPolicy(project)
        server = McpServer(project=project, policy=policy)
        register_project_tools(
                server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})
        return server

    def test_not_registered_when_disabled(self):
        server = self._server()
        self.assertNotIn('hello-world', server.tools)

    def test_registered_when_enabled(self):
        server = self._server(CUSTOM_CONFIG)
        tool = server.tools['hello-world']
        described = tool.describe()
        self.assertEqual(described['description'],
                         'Say hello The server asks the user to '
                         'confirm before this runs.')
        self.assertEqual(described['_meta'],
                         {'org.debops/confirm': True})
        self.assertTrue(described['annotations']['destructiveHint'])
        self.assertFalse(described['annotations']['idempotentHint'])
        self.assertTrue(described['annotations']['openWorldHint'])
        self.assertTrue(tool.self_confirms)
        self.assertFalse(tool.task_eligible)

    def test_a_denied_custom_tool_is_not_registered(self):
        config = {'project': {'mcp': {'custom_tools': True,
                                      'deny': ['hello-world']}}}
        server = self._server(config)
        self.assertNotIn('hello-world', server.tools)


class CustomToolRunTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-custom-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.tools_dir = os.path.join(self.tmpdir, '.debops', 'mcp',
                                      'tools')
        os.makedirs(self.tools_dir)
        self.project = make_project(self.tmpdir, CUSTOM_CONFIG)
        self.policy = McpPolicy(self.project)

    def make_tool(self, body='#!/bin/sh\ncat\n', name='echo-args'):
        path = write_tool(self.tools_dir, name, body=body)
        return make_custom_tool(self.project, self.policy, name, path)

    def context(self, **kwargs):
        return ToolContext(1, threading.Event(), **kwargs)

    def confirmed_context(self):
        return self.context(input_responses={
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': True}}})

    def test_confirmed_call_runs_the_script(self):
        handler = self.make_tool()
        result = handler({'one': 1}, self.confirmed_context())
        self.assertFalse(result['isError'])
        # The script is 'cat': the arguments came back on stdout
        self.assertEqual(result['content'][0]['text'],
                         '{"one": 1}\n')

    def test_declined_call_does_not_run(self):
        handler = self.make_tool()
        context = self.context(input_responses={
                'confirmation': {'action': 'decline', 'content': {}}})
        result = handler({}, context)
        self.assertTrue(result['isError'])
        self.assertIn('declined', result['content'][0]['text'])

    def test_elicitation_request_for_a_capable_client(self):
        handler = self.make_tool()
        context = self.context(
                client_capabilities={'elicitation': {'form': {}}})
        result = handler({}, context)
        self.assertEqual(result['resultType'], 'input_required')
        request = result['inputRequests']['confirmation']
        self.assertEqual(request['params']['mode'], 'form')
        self.assertIn('echo-args', request['params']['message'])

    def test_a_client_without_elicitation_proceeds_when_opted_out(self):
        # The proceed behavior is the deliberate opt-out
        self.project.config.raw['project']['mcp']['confirm_required'] = \
            False
        policy = McpPolicy(self.project)
        path = write_tool(self.tools_dir, 'echo-args')
        handler = make_custom_tool(self.project, policy, 'echo-args',
                                   path)
        with self.assertLogs('debops.mcp.tools.common',
                             level='NOTICE') as recorded:
            result = handler({'a': 'b'}, self.context())
        self.assertFalse(result['isError'])
        self.assertTrue(any('without user confirmation' in line
                            for line in recorded.output),
                        recorded.output)

    def test_a_client_without_elicitation_is_refused_by_default(self):
        # confirm_required defaults on: the client never asked, so the
        # call is refused
        handler = self.make_tool()
        result = handler({}, self.context())
        self.assertTrue(result['isError'])
        self.assertIn('confirm_required', result['content'][0]['text'])

    def test_nonzero_exit_is_a_tool_error(self):
        handler = self.make_tool(
                body='#!/bin/sh\necho broke >&2\nexit 3\n')
        result = handler({}, self.confirmed_context())
        self.assertTrue(result['isError'])
        self.assertIn('exit code 3', result['content'][0]['text'])
        self.assertIn('broke', result['content'][0]['text'])

    def test_timeout_is_a_tool_error(self):
        handler = self.make_tool(body='#!/bin/sh\nsleep 30\n')
        with mock.patch('debops.mcp.tools.custom.CUSTOM_TOOL_TIMEOUT',
                        1):
            result = handler({}, self.confirmed_context())
        self.assertTrue(result['isError'])
        self.assertIn('timed out', result['content'][0]['text'])

    def test_cancellation_raises(self):
        handler = self.make_tool(body='#!/bin/sh\nsleep 30\n')
        context = self.confirmed_context()
        context.cancelled.set()
        with self.assertRaises(RequestCancelled):
            handler({}, context)

    def test_the_environment_and_the_working_directory(self):
        handler = self.make_tool(
                body='#!/bin/sh\nprintf %s "$PWD"\n')
        result = handler({}, self.confirmed_context())
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][0]['text'].strip(),
                         os.path.realpath(self.tmpdir))


class CustomToolWriteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-custom-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       check=True)
        self.commits = []

        def _record_commit(instance, message=None):
            self.commits.append(message)

        # The tools directory is an explicit opt-in on top of the
        # feature toggle
        write_config = {'project': {'mcp': {
            'custom_tools': True,
            'write_allow': ['.debops/mcp/tools/**']}}}

        # The tool commits explicitly; the stub records the messages
        self.project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'auto_commit': _auto_commit_noop,
            'commit': _record_commit,
            'config': type('Config', (), {'_env_vars': {},
                                          'raw': write_config})(),
        })()
        self.policy = McpPolicy(self.project)
        self.tool_path = os.path.join(self.tmpdir, '.debops', 'mcp',
                                      'tools', 'hello-world')

    def write(self, path, content='#!/bin/sh\ncat\n'):
        from debops.mcp.tools.files import tool_view_write
        return tool_view_write(self.project, self.policy,
                               {'path': path, 'content': content},
                               server=self.server)

    def result_text(self, result):
        content = result['content']
        self.assertEqual(len(content), 1)
        return content[0]['text']

    server = None

    def test_write_creates_an_executable_tool(self):
        self.server = mock.Mock()
        result = self.write('.debops/mcp/tools/hello-world')
        self.assertFalse(result['isError'])
        self.assertEqual(stat.S_IMODE(os.stat(self.tool_path).st_mode),
                         0o755)
        self.assertEqual(self.commits,
                         ['DebOps MCP: view_write '
                          '.debops/mcp/tools/hello-world'])
        # The new tool surface is picked up in place
        self.server.reload.assert_called_once()

    def test_write_refused_when_custom_tools_are_disabled(self):
        self.project.config.raw['project']['mcp']['custom_tools'] = \
            False
        policy = McpPolicy(self.project)
        from debops.mcp.tools.files import tool_view_write
        result = tool_view_write(
                self.project, policy,
                {'path': '.debops/mcp/tools/hello-world',
                 'content': '#!/bin/sh\n'})
        self.assertTrue(result['isError'])
        self.assertIn('only the MCP content directories',
                      result['content'][0]['text'])

    def test_a_tool_needs_a_shebang(self):
        result = self.write('.debops/mcp/tools/hello-world',
                            content='echo hi\n')
        self.assertTrue(result['isError'])
        self.assertIn('shebang', result['content'][0]['text'])

    def test_invalid_tool_name(self):
        result = self.write('.debops/mcp/tools/Bad Name')
        self.assertTrue(result['isError'])
        self.assertIn('invalid tool name', result['content'][0]['text'])

    def test_no_nested_tools(self):
        result = self.write('.debops/mcp/tools/sub/hello-world')
        self.assertTrue(result['isError'])
        self.assertIn('only the MCP content directories',
                      result['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
