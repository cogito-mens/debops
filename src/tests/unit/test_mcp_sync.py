# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the git synchronization tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The project itself is a stub; the ProjectDir methods are
covered by the CLI integration tests.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, PROTOCOL_VERSION
from debops.mcp.tools import register_project_tools
import os
import shutil
import tempfile
import unittest


PULL_CHANGED = {'action': 'pull', 'before': 'a' * 40, 'after': 'b' * 40,
                'changed': True, 'files_changed': 3,
                'commits': ['Add a thing', 'Fix another thing']}

PULL_CLEAN = {'action': 'pull', 'before': 'a' * 40, 'after': 'a' * 40,
              'changed': False, 'commits': [], 'files_changed': 0}

PUSH_DONE = {'action': 'push', 'pushed': True, 'commits': ['Add a thing']}

PUSH_CLEAN = {'action': 'push', 'pushed': False, 'commits': []}


def _auto_commit_noop(self, message=None):
    pass


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}


class SyncTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-sync-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.reports = {'pull': PULL_CHANGED, 'push': PUSH_DONE}
        self.fail_with = None

        project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {'raw': {}})(),
        })()
        case = self

        def sync(direction):
            def call():
                if case.fail_with is not None:
                    raise case.fail_with
                return case.reports[direction]
            return call

        project.pull = sync('pull')
        project.push = sync('push')
        self.project = project

        self.policy = McpPolicy(self.project)
        self.server = McpServer(project=self.project, policy=self.policy)
        register_project_tools(
                self.server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})

    def call_tool(self, name, arguments=None, request_id=1):
        return self.server.handle({
            'jsonrpc': '2.0', '_meta': REQUEST_META,
            'id': request_id, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments or {}},
        })

    def result_text(self, response):
        return response['result']['content'][0]['text']

    def test_pull_reports_changes(self):
        response = self.call_tool('project_pull')
        text = self.result_text(response)
        self.assertFalse(response['result']['isError'])
        self.assertIn('files changed: 3', text)
        self.assertIn('Add a thing', text)
        self.assertIn('Fix another thing', text)

    def test_pull_up_to_date(self):
        self.reports['pull'] = PULL_CLEAN
        text = self.result_text(self.call_tool('project_pull'))
        self.assertIn('Already up to date', text)

    def test_push_reports_commits(self):
        text = self.result_text(self.call_tool('project_push'))
        self.assertIn('pushed 1 commit(s)', text)
        self.assertIn('Add a thing', text)

    def test_push_nothing_to_push(self):
        self.reports['push'] = PUSH_CLEAN
        text = self.result_text(self.call_tool('project_push'))
        self.assertIn('Nothing to push', text)

    def test_second_call_is_throttled(self):
        self.call_tool('project_pull')
        response = self.call_tool('project_pull')
        self.assertTrue(response['result']['isError'])
        self.assertIn('retry in', self.result_text(response))

    def test_throttle_is_per_direction(self):
        self.call_tool('project_pull')
        # Push is still allowed while pull is throttled
        response = self.call_tool('project_push')
        self.assertFalse(response['result']['isError'])

    def test_errors_pass_through(self):
        self.fail_with = ValueError('the project has no origin')
        response = self.call_tool('project_pull')
        self.assertTrue(response['result']['isError'])
        self.assertIn('no origin', self.result_text(response))

    def test_project_without_git_is_an_error_result(self):
        self.fail_with = ValueError('the project directory is not a git '
                                    'repository')
        for name in ('project_pull', 'project_push'):
            response = self.call_tool(name)
            self.assertTrue(response['result']['isError'], name)
            self.assertIn('not a git repository',
                          self.result_text(response))

    def test_push_can_be_denied_alone(self):
        self.project.config.raw['project'] = {'mcp': {'deny':
                                                      ['project_push']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(
                server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})
        self.assertIn('project_pull', server.tools)
        self.assertNotIn('project_push', server.tools)


if __name__ == '__main__':
    unittest.main()
