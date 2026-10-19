# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the collection management tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The installation is mocked; the YAML surgery runs for real.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import ToolContext
from debops.mcp.tools import galaxy
from debops.mcp.tools.galaxy import (_entry_blocks, _remove_requirement,
                                     _upsert_requirement,
                                     tool_collection_remove,
                                     tool_collection_require,
                                     tool_collection_sync)
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


def _auto_commit_noop(self, message=None):
    pass


REQUIREMENTS = '''---
# A comment header

collections:

  - name: 'ansible.posix'
    version: '*'

  # A note about the next entry
  - name: 'community.docker'
    version: '>=3.0'
'''


class SurgeryTestCase(unittest.TestCase):

    def test_upsert_appends(self):
        text, changed = _upsert_requirement(
                REQUIREMENTS, {'name': 'community.crypto',
                               'version': '*'})
        self.assertTrue(changed)
        self.assertIn("- name: 'community.crypto'", text)
        # The comment header and the note survive
        self.assertIn('# A comment header', text)
        self.assertIn('# A note about the next entry', text)

    def test_upsert_updates_in_place(self):
        text, changed = _upsert_requirement(
                REQUIREMENTS, {'name': 'community.docker',
                               'version': '>=4.0'})
        self.assertTrue(changed)
        self.assertIn("version: '>=4.0'", text)
        self.assertNotIn("'>=3.0'", text)
        self.assertIn('# A note about the next entry', text)

    def test_upsert_unchanged(self):
        text, changed = _upsert_requirement(
                REQUIREMENTS, {'name': 'ansible.posix', 'version': '*'})
        self.assertFalse(changed)
        self.assertEqual(text, REQUIREMENTS)

    def test_upsert_into_an_empty_list(self):
        text, changed = _upsert_requirement(
                '---\n\ncollections: []\n',
                {'name': 'ansible.posix'})
        self.assertTrue(changed)
        self.assertIn('collections:', text)
        self.assertIn("- name: 'ansible.posix'", text)
        self.assertNotIn('[]', text)

    def test_upsert_into_a_missing_file(self):
        text, changed = _upsert_requirement('', {'name': 'ansible.posix'})
        self.assertTrue(changed)
        self.assertIn('collections:', text)
        self.assertIn("- name: 'ansible.posix'", text)

    def test_remove(self):
        text, changed = _remove_requirement(REQUIREMENTS,
                                            'community.docker')
        self.assertTrue(changed)
        self.assertNotIn('community.docker', text)
        self.assertIn('ansible.posix', text)

    def test_remove_missing(self):
        text, changed = _remove_requirement(REQUIREMENTS, 'nosuch.coll')
        self.assertFalse(changed)
        self.assertEqual(text, REQUIREMENTS)

    def test_entry_blocks(self):
        lines = REQUIREMENTS.splitlines()
        blocks = _entry_blocks(lines)
        self.assertEqual(len(blocks), 2)
        self.assertIn('ansible.posix', lines[blocks[0][0]])
        self.assertIn('community.docker', lines[blocks[1][0]])


class CollectionToolsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-galaxy-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       check=True)
        self.commits = []

        def _record_commit(instance, message=None):
            self.commits.append(message)

        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'commit': _record_commit,
            'config': type('Config', (), {'raw': {}})(),
        })()
        self.policy = McpPolicy(self.project)
        self.context = ToolContext(1, threading.Event())
        self.requirements = os.path.join(
                self.tmpdir, 'ansible', 'collections',
                'requirements.yml')
        os.makedirs(os.path.dirname(self.requirements))
        with open(self.requirements, 'w') as fp:
            fp.write(REQUIREMENTS)

    def test_require_writes_and_installs(self):
        with mock.patch.object(galaxy, '_galaxy_install',
                               return_value='installed ok\n') as install:
            result = tool_collection_require(
                    self.project, self.policy,
                    {'name': 'community.crypto'}, self.context)
        self.assertFalse(result['isError'], result)
        install.assert_called_once()
        with open(self.requirements) as fp:
            text = fp.read()
        self.assertIn("- name: 'community.crypto'", text)
        self.assertIn('# A comment header', text)
        self.assertEqual(self.commits,
                         ['DebOps MCP: collection_require '
                          'community.crypto'])

    def test_require_with_a_git_source(self):
        with mock.patch.object(galaxy, '_galaxy_install',
                               return_value=''):
            result = tool_collection_require(
                    self.project, self.policy,
                    {'name': 'myns.mycoll',
                     'source': 'https://git.example.org/myns/mycoll.git',
                     'type': 'git', 'version': 'main'}, self.context)
        self.assertFalse(result['isError'], result)
        with open(self.requirements) as fp:
            text = fp.read()
        self.assertIn("source: 'https://git.example.org/myns/mycoll.git'",
                      text)
        self.assertIn("type: 'git'", text)

    def test_require_unchanged_skips_the_install(self):
        with mock.patch.object(galaxy, '_galaxy_install') as install:
            result = tool_collection_require(
                    self.project, self.policy,
                    {'name': 'ansible.posix', 'version': '*'},
                    self.context)
        self.assertIn('unchanged', result['content'][0]['text'])
        install.assert_not_called()

    def test_require_install_failure_is_reported_but_committed(self):
        with mock.patch.object(galaxy, '_galaxy_install',
                               side_effect=ChildProcessError('boom')):
            result = tool_collection_require(
                    self.project, self.policy,
                    {'name': 'community.crypto'}, self.context)
        self.assertTrue(result['isError'])
        self.assertIn('installation failed',
                      result['content'][0]['text'])
        self.assertIn('collection_sync', result['content'][0]['text'])
        self.assertEqual(self.commits,
                         ['DebOps MCP: collection_require '
                          'community.crypto'])

    def test_require_validates_the_name(self):
        result = tool_collection_require(self.project, self.policy,
                                         {'name': 'NoDots'},
                                         self.context)
        self.assertTrue(result['isError'])

    def test_require_non_git_refused(self):
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        result = tool_collection_require(self.project, self.policy,
                                         {'name': 'ansible.posix'},
                                         self.context)
        self.assertTrue(result['isError'])
        self.assertIn('not a git repository',
                      result['content'][0]['text'])

    def test_remove_drops_everything(self):
        # The installed content: the directory and the marker
        base = os.path.join(self.tmpdir, 'ansible', 'collections',
                            'ansible_collections')
        os.makedirs(os.path.join(base, 'community', 'docker'))
        with open(os.path.join(
                base, 'community.docker-3.0.0.info'), 'w') as fp:
            fp.write('')
        with open(os.path.join(os.path.dirname(self.requirements),
                               'requirements.lock.yml'), 'w') as fp:
            fp.write('---\n\ncollections:\n\n'
                     "  - name: 'community.docker'\n"
                     "    version: '3.0.0'\n")
        result = tool_collection_remove(self.project, self.policy,
                                        {'name': 'community.docker'})
        self.assertFalse(result['isError'], result)
        text = result['content'][0]['text']
        self.assertIn('requirements.yml', text)
        self.assertIn('requirements.lock.yml', text)
        self.assertIn('community/docker/', text)
        self.assertIn('community.docker-3.0.0.info', text)
        self.assertFalse(os.path.isdir(
                os.path.join(base, 'community')))
        with open(self.requirements) as fp:
            self.assertNotIn('community.docker', fp.read())
        with open(os.path.join(os.path.dirname(self.requirements),
                               'requirements.lock.yml')) as fp:
            self.assertNotIn('community.docker', fp.read())
        self.assertEqual(self.commits,
                         ['DebOps MCP: collection_remove '
                          'community.docker'])

    def test_remove_absent_is_unchanged(self):
        result = tool_collection_remove(self.project, self.policy,
                                        {'name': 'nosuch.coll'})
        self.assertIn('unchanged', result['content'][0]['text'])
        self.assertEqual(self.commits, [])

    def test_sync_installs(self):
        with mock.patch.object(galaxy, '_galaxy_install',
                               return_value='installed\n') as install:
            result = tool_collection_sync(self.project, self.policy,
                                          {}, self.context)
        self.assertFalse(result['isError'], result)
        self.assertIn('installed', result['content'][0]['text'])
        install.assert_called_once()
        self.assertEqual(self.commits, [])

    def test_sync_reports_the_failure(self):
        with mock.patch.object(galaxy, '_galaxy_install',
                               side_effect=FileNotFoundError('no galaxy')):
            result = tool_collection_sync(self.project, self.policy,
                                          {}, self.context)
        self.assertTrue(result['isError'])
        self.assertIn('no galaxy', result['content'][0]['text'])

    def test_sync_via_a_task(self):
        # The install is task-eligible: the client gets the handle and
        # polls for the outcome
        from debops.mcp.server import McpServer, PROTOCOL_VERSION, \
            TASKS_EXTENSION
        server = McpServer()
        server.register_tool(
                'collection_sync', 'Sync', {'type': 'object'},
                lambda arguments, context: tool_collection_sync(
                        self.project, self.policy, arguments, context),
                needs_context=True, task_eligible=True)
        meta = {'io.modelcontextprotocol/protocolVersion':
                PROTOCOL_VERSION,
                'io.modelcontextprotocol/clientCapabilities':
                {'extensions': {TASKS_EXTENSION: {}}}}
        with mock.patch.object(galaxy, '_galaxy_install',
                               return_value='installed ok\n'):
            response = server.handle({
                    'jsonrpc': '2.0', 'id': 1,
                    'method': 'tools/call',
                    'params': {'_meta': meta,
                               'name': 'collection_sync',
                               'arguments': {}}})
            result = response['result']
            self.assertEqual(result['resultType'], 'task')
            # The task runs in its own thread; the mock must cover it
            import time
            limit = time.monotonic() + 10
            task = {}
            while time.monotonic() < limit:
                task = server.handle({
                        'jsonrpc': '2.0', 'id': 2,
                        'method': 'tasks/get',
                        'params': {'_meta': meta,
                                   'taskId': result['taskId']}})['result']
                if task['status'] == 'completed':
                    break
                time.sleep(0.05)
        self.assertEqual(task['status'], 'completed')
        self.assertIn('installed ok',
                      task['result']['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
