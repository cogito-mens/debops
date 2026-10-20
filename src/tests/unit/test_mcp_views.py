# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the view lifecycle tools (view.list/switch/create)."""

import os
import shutil
import tempfile
import unittest

from unittest import mock

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer
from debops.mcp.tools import register_project_tools
from debops.mcp.tools.views import (NO_ACTIVE_VIEW, tool_view_create,
                                    tool_view_list, tool_view_switch)

# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': '2026-07-28',
    'io.modelcontextprotocol/clientCapabilities': {},
}


def _auto_commit_noop(self, message=None):
    pass


class ViewsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-views-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'views',
                                 'system'))
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'views', 'web'))
        self.commits = []

        def _record_commit(instance, message=None):
            self.commits.append(message)

        def _mkview(instance, view):
            os.makedirs(os.path.join(self.tmpdir, 'ansible', 'views',
                                     view))
            self.project.config.raw['views'][view] = {}

        self.project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'modern',
            'view': 'system',
            'auto_commit': _record_commit,
            'commit': _auto_commit_noop,
            'mkview': _mkview,
            'config': type('Config', (), {
                'raw': {'views': {'system': {}, 'web': {}}},
                '_env_vars': {}})(),
        })()
        self.policy = McpPolicy(self.project)
        self.server = mock.Mock()

    def switch(self, name):
        return tool_view_switch(self.project, self.policy,
                                {'view': name}, self.server)

    def test_list_marks_the_active_view(self):
        result = tool_view_list(self.project, self.policy, {})
        text = result['content'][0]['text']
        self.assertIn('Active: system', text)
        self.assertIn('- `system` — active', text)
        self.assertIn('- `web`', text)

    def test_list_json_conforms(self):
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        result = tool_view_list(self.project, self.policy,
                                {'format': 'json'})
        import json
        data = json.loads(result['content'][0]['text'])
        self.assertEqual(check_schema(
                data, TOOL_OUTPUT_SCHEMAS['view.list']), [])
        self.assertEqual(data['active'], 'system')
        self.assertFalse(data['locked'])

    def test_list_reports_the_lock(self):
        self.policy.view_lock = True
        result = tool_view_list(self.project, self.policy, {})
        self.assertIn('Active: system (locked)',
                      result['content'][0]['text'])

    def test_switch_activates_a_registered_view(self):
        result = self.switch('web')
        self.assertFalse(result.get('isError', False))
        self.server.switch_view.assert_called_once_with('web')
        self.assertIn('system → web', result['content'][0]['text'])
        # The switch is visible to every connected client
        self.assertIn('All connected clients',
                      result['content'][0]['text'])

    def test_switch_requires_the_argument(self):
        result = self.switch('')
        self.assertTrue(result['isError'])
        self.assertIn('"view" argument is required',
                      result['content'][0]['text'])

    def test_switch_refuses_a_locked_view(self):
        self.policy.view_lock = True
        result = self.switch('web')
        self.assertTrue(result['isError'])
        self.assertIn('locked', result['content'][0]['text'])
        self.server.switch_view.assert_not_called()

    def test_switch_refuses_an_unknown_view(self):
        result = self.switch('ghost')
        self.assertTrue(result['isError'])
        self.assertIn('view not found', result['content'][0]['text'])

    def test_switch_to_the_current_view_is_unchanged(self):
        result = self.switch('system')
        self.assertFalse(result.get('isError', False))
        self.assertIn('unchanged', result['content'][0]['text'])
        self.server.switch_view.assert_not_called()

    def test_switch_on_legacy_is_refused(self):
        self.project.project_type = 'legacy'
        result = self.switch('web')
        self.assertTrue(result['isError'])
        self.assertIn('single', result['content'][0]['text'])

    def test_switch_refused_while_encfs_mounted(self):
        # A live plaintext mount on the view's secret directory; the
        # lock-down would rearrange the workspace under the switch
        with mock.patch('debops.mcp.tools.views.os.path.ismount',
                        return_value=True):
            result = self.switch('web')
        self.assertTrue(result['isError'])
        self.assertIn('encfs', result['content'][0]['text'])
        self.assertIn('debops project lock',
                      result['content'][0]['text'])
        self.server.switch_view.assert_not_called()

    def test_switch_refused_while_git_crypt_unlocked(self):
        # The smudge filter configured is git-crypt's unlocked state
        with mock.patch('debops.mcp.tools.views.subprocess.run') \
                as run:
            run.return_value = mock.Mock(
                    returncode=0, stdout='!true\n')
            result = self.switch('web')
        self.assertTrue(result['isError'])
        self.assertIn('git-crypt', result['content'][0]['text'])
        self.server.switch_view.assert_not_called()

    def test_switch_proceeds_with_locked_secrets(self):
        # Not a git repository at all: git answers nothing
        result = self.switch('web')
        self.assertFalse(result.get('isError', False))
        self.server.switch_view.assert_called_once_with('web')

    def test_create_makes_and_commits_the_view(self):
        result = tool_view_create(self.project, self.policy,
                                  {'view': 'staging'})
        self.assertFalse(result.get('isError', False))
        self.assertTrue(os.path.isdir(os.path.join(
                self.tmpdir, 'ansible', 'views', 'staging')))
        self.assertIn('staging', self.project.config.raw['views'])
        self.assertEqual(self.commits, ['DebOps MCP: view.create '
                                        'staging'])
        self.assertIn('view.switch', result['content'][0]['text'])

    def test_create_validates_the_name(self):
        for bad in ('web-servers', 'UPPER', '../x', 'a/b', ''):
            result = tool_view_create(self.project, self.policy,
                                      {'view': bad})
            self.assertTrue(result['isError'], bad)
            self.assertIn('invalid view name' if bad else
                          '"view" argument is required',
                          result['content'][0]['text'], bad)

    def test_create_refuses_dashes_specifically(self):
        # The serve --instance names are '<project>-<view>', split at
        # the last dash; a dash in the view name would break that
        result = tool_view_create(self.project, self.policy,
                                  {'view': 'web-servers'})
        self.assertIn('dashes', result['content'][0]['text'])

    def test_create_refuses_an_existing_view(self):
        result = tool_view_create(self.project, self.policy,
                                  {'view': 'web'})
        self.assertTrue(result['isError'])
        self.assertIn('already exists', result['content'][0]['text'])

    def test_create_on_legacy_is_refused(self):
        self.project.project_type = 'legacy'
        result = tool_view_create(self.project, self.policy,
                                  {'view': 'staging'})
        self.assertTrue(result['isError'])
        self.assertIn('modern', result['content'][0]['text'])


class NoViewPolicyTestCase(unittest.TestCase):
    """The policy without an active view."""

    def _project(self, view, project_type='modern', views=None):
        return type('Project', (), {
            'path': '/srv/projects/example',
            'name': 'example',
            'project_type': project_type,
            'view': view,
            'config': type('Config', (), {
                'raw': {'views': views if views is not None
                        else {'system': {}}},
                '_env_vars': {}})(),
        })()

    def test_registered_view_sets_the_root(self):
        policy = McpPolicy(self._project('system'))
        self.assertTrue(policy.view_root.endswith(
                os.path.join('ansible', 'views', 'system')))

    def test_empty_view_means_no_view(self):
        # An empty default_view is a deliberate configuration: the
        # command line then requires --view or a view directory; the
        # MCP server starts without an active view instead
        policy = McpPolicy(self._project('', views={'system': {}}))
        self.assertIsNone(policy.view_root)

    def test_unregistered_view_means_no_view(self):
        policy = McpPolicy(self._project('ghost'))
        self.assertIsNone(policy.view_root)

    def test_legacy_always_has_a_root(self):
        policy = McpPolicy(self._project('system',
                                         project_type='legacy'))
        self.assertTrue(policy.view_root.endswith('ansible'))

    def test_view_lock_defaults_off(self):
        policy = McpPolicy(self._project('system'))
        self.assertFalse(policy.view_lock)
        self.project_config = {'project': {'mcp': {'view_lock': True}}}
        project = self._project('system')
        project.config.raw.update(self.project_config)
        self.assertTrue(McpPolicy(project).view_lock)


class NoViewSurfaceTestCase(unittest.TestCase):
    """The stubbed surface of a server without an active view."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-noview-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.makedirs(os.path.join(self.tmpdir, '.git'))
        self.project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'modern',
            'view': '',
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {
                'raw': {'views': {'system': {}}},
                '_env_vars': {}})(),
        })()
        self.server = McpServer(project=self.project,
                                policy=McpPolicy(self.project))
        register_project_tools(
                self.server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})

    def call_tool(self, name, arguments=None):
        return self.server.handle({
            'jsonrpc': '2.0',
            'id': 1, 'method': 'tools/call',
            'params': {'_meta': REQUEST_META, 'name': name,
                       'arguments': arguments or {}},
        })

    def test_view_bound_tools_answer_with_the_guidance(self):
        for name in ('file.write', 'inventory.query', 'secret.paths'):
            response = self.call_tool(name, {})
            self.assertTrue(response['result']['isError'], name)
            self.assertIn(NO_ACTIVE_VIEW,
                          response['result']['content'][0]['text'], name)

    def test_view_tools_and_status_still_work(self):
        response = self.call_tool('view.list')
        self.assertFalse(response['result'].get('isError', False))
        self.assertIn('Active: none',
                      response['result']['content'][0]['text'])
        response = self.call_tool('project.status')
        self.assertIn('View: none',
                      response['result']['content'][0]['text'])

    def test_the_surface_keeps_the_same_tool_names(self):
        response = self.server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                'params': {'_meta': REQUEST_META, }})
        names = [tool['name'] for tool in response['result']['tools']]
        # A client which cached tools/list keeps valid names
        self.assertIn('file.write', names)
        self.assertIn('view.switch', names)


if __name__ == '__main__':
    unittest.main()
