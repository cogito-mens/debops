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


def _auto_commit_noop(self, message=None, paths=None):
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


class GalaxyListTestCase(unittest.TestCase):
    """The ansible-galaxy collection list parse."""

    def _run(self, stdout, returncode=0):
        from debops.mcp.tools.collections import galaxy_collection_list
        project = type('Project', (), {
            'path': '/srv/projects/example',
            'config': type('Config', (), {'_env_vars': {},
                                          'raw': {}})(),
        })()
        with mock.patch(
                'debops.mcp.tools.collections.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=returncode, stdout=stdout,
                    stderr='')
            return galaxy_collection_list(project)

    def test_parses_the_json(self):
        index = self._run('{"ansible/collections": {"debops.debops": '
                          '{"version": "3.2.0"}}}')
        self.assertEqual(
                index['ansible/collections']['debops.debops']
                ['version'], '3.2.0')

    def test_garbage_is_a_clean_tool_error(self):
        # A warning-chatty or broken ansible-galaxy writes anything but
        # JSON; that is a tool-logic failure, not an internal error
        with self.assertRaises(RuntimeError) as context:
            self._run('[WARNING]: something broke\nnot json at all\n')
        self.assertIn('did not return JSON', str(context.exception))


class DocsProviderTestCase(unittest.TestCase):
    """The documentation-mode collection index and its roots."""

    def setUp(self):
        from debops.mcp.tools import collections
        collections._account_roots_cache['roots'] = None
        self.addCleanup(collections._account_roots_cache.update,
                        roots=None)

    def test_parse_collections_paths(self):
        from debops.mcp.tools.collections import _parse_collections_paths
        self.assertEqual(_parse_collections_paths(
                "CONFIGURABLE TEXT\n"
                "COLLECTIONS_PATHS(default) = "
                "['/home/x/.ansible/collections', "
                "'/usr/share/ansible/collections']\n"),
                ['/home/x/.ansible/collections',
                 '/usr/share/ansible/collections'])
        self.assertEqual(_parse_collections_paths(
                "COLLECTIONS_PATHS(env: ANSIBLE_COLLECTIONS_PATH) = "
                "['/srv/x']\n"), ['/srv/x'])
        with self.assertRaises(ValueError):
            _parse_collections_paths('garbage\n')
        with self.assertRaises(ValueError):
            _parse_collections_paths("COLLECTIONS_PATHS(default) = 42\n")

    def test_account_roots_without_ansible_fall_back_to_defaults(self):
        from debops.mcp.tools import collections
        with mock.patch.object(collections, 'account_ansible_binary',
                               return_value=None):
            roots = collections.account_collection_roots()
        self.assertEqual(roots,
                         [os.path.expanduser('~/.ansible/collections'),
                          '/usr/share/ansible/collections'])

    def test_account_roots_come_from_the_ansible_config_dump(self):
        from debops.mcp.tools import collections
        dump = ("COLLECTIONS_PATHS(default) = "
                "['/acct/.ansible/collections', "
                "'/usr/share/ansible/collections']\n")
        with mock.patch.object(collections, 'account_ansible_binary',
                               return_value='/x/bin/ansible-config'), \
                mock.patch.object(collections.subprocess,
                                  'run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=dump, stderr='')
            roots = collections.account_collection_roots()
            # The discovery result is cached for the process
            self.assertEqual(collections.account_collection_roots(),
                             roots)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0],
                         ['/x/bin/ansible-config', 'dump'])
        self.assertEqual(roots, ['/acct/.ansible/collections',
                                 '/usr/share/ansible/collections'])
        # No variable, no ANSIBLE_HOME: the defaults are the account's
        # real ones; the neutral config blocks config smuggling
        env = run.call_args[1]['env']
        self.assertNotIn('ANSIBLE_COLLECTIONS_PATH', env)
        self.assertNotIn('ANSIBLE_HOME', env)
        self.assertTrue(env['ANSIBLE_CONFIG'].endswith(
                os.path.join('mcp', 'ansible.cfg')))

    def test_documentation_roots_with_a_defined_variable(self):
        from debops.mcp.tools import collections
        roots = collections.documentation_collection_roots(
                {'ANSIBLE_COLLECTIONS_PATH': '/srv/a:/srv/b'})
        # The variable rules; the bundled collection rides last
        self.assertEqual(roots, ['/srv/a', '/srv/b',
                                 collections.BUNDLED_COLLECTIONS_ROOT])
        # ... and is not duplicated when the operator named it
        roots = collections.documentation_collection_roots(
                {'ANSIBLE_COLLECTIONS_PATH':
                 collections.BUNDLED_COLLECTIONS_ROOT})
        self.assertEqual(roots, [collections.BUNDLED_COLLECTIONS_ROOT])

    def test_documentation_roots_without_a_variable(self):
        from debops.mcp.tools import collections
        with mock.patch.object(collections, 'account_collection_roots',
                               return_value=['/acct/collections']):
            roots = collections.documentation_collection_roots({})
        self.assertEqual(roots, ['/acct/collections',
                                 collections.BUNDLED_COLLECTIONS_ROOT])

    def test_collection_content_dirs(self):
        from debops.mcp.tools import collections
        tmpdir = tempfile.mkdtemp(prefix='debops-mcp-roots-')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        for root in ('a', 'b'):
            os.makedirs(os.path.join(tmpdir, root, 'ansible_collections',
                                     'ns{}'.format(root),
                                     'coll', 'roles'))
        # A collection without a roles directory is skipped
        os.makedirs(os.path.join(tmpdir, 'b', 'ansible_collections',
                                 'nsb', 'noroles'))
        dirs = collections.collection_content_dirs(
                [os.path.join(tmpdir, 'a'), os.path.join(tmpdir, 'b')],
                'roles')
        self.assertEqual([os.path.relpath(entry, tmpdir)
                          for entry in dirs],
                         [os.path.join('a', 'ansible_collections',
                                       'nsa', 'coll', 'roles'),
                          os.path.join('b', 'ansible_collections',
                                       'nsb', 'coll', 'roles')])

    def test_filter_collections_under(self):
        from debops.mcp.tools.collections import filter_collections_under

        def provider():
            return {
                'debops.debops': {'path': '/bundled/ansible_collections/'
                                  'debops/debops', 'version': '1'},
                'community.sops': {'path': '/ambient/ansible_collections/'
                                   'community/sops', 'version': '2'},
            }

        filtered = filter_collections_under(provider, ['/bundled'])()
        self.assertEqual(sorted(filtered), ['debops.debops'])
        # No roots, no collections
        self.assertEqual(filter_collections_under(provider, [])(), {})

    def test_docs_provider_filters_and_sets_the_variable(self):
        from debops.mcp.tools import collections
        seen = {}

        def fake_list(project=None, environ=None):
            seen['environ'] = environ
            return {
                '/bundled/ansible_collections': {
                    'debops.debops': {'version': '1'}},
                '/ambient/ansible_collections': {
                    'community.sops': {'version': '2'}},
            }

        with mock.patch.object(collections, 'galaxy_collection_list',
                               fake_list):
            provider = collections.docs_collection_provider(
                    environ={'ANSIBLE_COLLECTIONS_PATH': '/bundled'})
            index = provider()
        self.assertEqual(sorted(index), ['debops.debops'])
        # The subprocess environment agrees with the filter: the
        # operator's root rules, the bundled one rides last, and the
        # neutral config blocks a stray ansible.cfg
        self.assertEqual(
                seen['environ']['ANSIBLE_COLLECTIONS_PATH'],
                os.pathsep.join(['/bundled',
                                 collections.BUNDLED_COLLECTIONS_ROOT]))
        self.assertTrue(seen['environ']['ANSIBLE_CONFIG'].endswith(
                os.path.join('mcp', 'ansible.cfg')))

    def test_docs_provider_serves_the_account_collections(self):
        from debops.mcp.tools import collections
        bundled = os.path.join(collections.BUNDLED_COLLECTIONS_ROOT,
                               'ansible_collections')

        def fake_list(project=None, environ=None):
            return {
                bundled: {'debops.debops': {'version': '1'}},
                '/ambient/ansible_collections': {
                    'community.sops': {'version': '2'}},
            }

        with mock.patch.object(collections, 'galaxy_collection_list',
                               fake_list), \
                mock.patch.object(collections,
                                  'account_collection_roots',
                                  return_value=['/ambient']):
            provider = collections.docs_collection_provider(environ={})
            index = provider()
        # The account's collections are served next to the bundled one
        self.assertEqual(sorted(index),
                         ['community.sops', 'debops.debops'])

    def test_docs_mode_galaxy_uses_the_account_binary(self):
        from debops.mcp.tools import collections
        with mock.patch.object(
                collections, 'account_ansible_binary',
                return_value='/home/x/.local/bin/ansible-galaxy'), \
                mock.patch.object(collections.subprocess,
                                  'run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout='{}', stderr='')
            collections.galaxy_collection_list(None, environ={})
        self.assertEqual(run.call_args[0][0][0],
                         '/home/x/.local/bin/ansible-galaxy')


class KillSwitchTestCase(unittest.TestCase):
    """A mid-run kill-switch flip terminates the galaxy install."""

    def test_disabled_mid_run(self):
        from debops.mcp.tools.galaxy import _galaxy_install
        tmpdir = tempfile.mkdtemp(prefix='debops-mcp-galaxy-')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        project = type('Project', (), {
            'path': tmpdir,
            'config': type('Config', (), {'_env_vars': {},
                                          'raw': {}})(),
        })()
        on_disabled = mock.Mock()
        with mock.patch('debops.mcp.tools.galaxy.shutil.which') \
                as which, \
                mock.patch('debops.mcp.tools.execute.run_cancellable') \
                as run:
            which.return_value = '/usr/bin/ansible-galaxy'
            run.return_value = ('', 0, 'disabled')
            with self.assertRaises(ChildProcessError) as context:
                _galaxy_install(project, enabled_check=lambda: False,
                                on_disabled=on_disabled)
        self.assertIn('kill switch', str(context.exception))
        kwargs = run.call_args[1]
        self.assertIs(kwargs['on_disabled'], on_disabled)
        self.assertIsNotNone(kwargs['enabled_check'])


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

        def _record_commit(instance, message=None, paths=None):
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
                         ['DebOps MCP: collection.require '
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
        self.assertIn('collection.sync', result['content'][0]['text'])
        self.assertEqual(self.commits,
                         ['DebOps MCP: collection.require '
                          'community.crypto'])

    def test_require_validates_the_name(self):
        result = tool_collection_require(self.project, self.policy,
                                         {'name': 'NoDots'},
                                         self.context)
        self.assertTrue(result['isError'])
        self.assertIn('not a collection name',
                      result['content'][0]['text'])

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
                         ['DebOps MCP: collection.remove '
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
                'collection.sync', 'Sync', {'type': 'object'},
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
                               'name': 'collection.sync',
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
