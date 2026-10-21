# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the MCP server environment.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.tools.common import mcp_environ
from debops.mcp.tools.environment import tool_environment_list
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


def _auto_commit_noop(self, message=None, paths=None):
    pass


class McpEnvironTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-env-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {
                '_env_vars': {'PROJECT_VAR': 'project'},
                'raw': {}})(),
        })()

    def write_env(self, name, text):
        target = os.path.join(self.tmpdir, '.debops', 'mcp', name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w') as fp:
            fp.write(text)

    def test_mcp_files_override(self):
        self.write_env('environment', 'REGION=eu\nOVERRIDE=environment\n')
        self.write_env('.env', 'OVERRIDE=token\nTOKEN=s3cret\n')
        with mock.patch.dict(os.environ,
                             {'OVERRIDE': 'process', 'PATH': '/bin'}):
            self.project.config._env_vars['OVERRIDE'] = 'project'
            env = mcp_environ(self.project)
        # The process environment is the base, then the project, then
        # the tracked file, then the tokens file
        self.assertEqual(env['OVERRIDE'], 'token')
        self.assertEqual(env['REGION'], 'eu')
        self.assertEqual(env['TOKEN'], 's3cret')
        self.assertEqual(env['PATH'], '/bin')
        self.assertEqual(env['PROJECT_VAR'], 'project')

    def test_missing_files_are_fine(self):
        env = mcp_environ(self.project)
        self.assertEqual(env['PROJECT_VAR'], 'project')

    def test_unreadable_file_reads_as_empty(self):
        self.write_env('.env', 'not a dotenv line without equals\n\x00\n')
        env = mcp_environ(self.project)
        self.assertNotIn('\x00', env)


class EnvironmentListTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-envlist-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {
                '_env_vars': {'PROJECT_VAR': 'project'},
                'raw': {}})(),
        })()
        self.policy = McpPolicy(self.project)

    def test_list_shows_names_and_safe_values(self):
        env_dir = os.path.join(self.tmpdir, '.debops', 'mcp')
        os.makedirs(env_dir)
        with open(os.path.join(env_dir, 'environment'), 'w') as fp:
            fp.write('REGION=eu-central-1\n')
        with open(os.path.join(env_dir, '.env'), 'w') as fp:
            fp.write('TOKEN=t0k3n\n')
        with mock.patch.dict(os.environ, {'MCPLISTVAR': '1'}):
            result = tool_environment_list(self.project, self.policy, {})
        text = result['content'][0]['text']
        # The tracked file shows values
        self.assertIn('REGION = eu-central-1', text)
        # The tokens file shows names only
        self.assertIn('- TOKEN', text)
        self.assertNotIn('t0k3n', text)
        # The project and process environments are names-only
        self.assertIn('- PROJECT_VAR', text)
        self.assertIn('- MCPLISTVAR', text)
        self.assertNotIn('= project', text)

    def test_empty_is_fine(self):
        with mock.patch.dict(os.environ, {'MCPLISTVAR': '1'}):
            result = tool_environment_list(self.project, self.policy, {})
        text = result['content'][0]['text']
        self.assertIn('(none)', text)

    def test_filter(self):
        env_dir = os.path.join(self.tmpdir, '.debops', 'mcp')
        os.makedirs(env_dir)
        with open(os.path.join(env_dir, 'environment'), 'w') as fp:
            fp.write('REGION=eu\nOTHER=x\n')
        result = tool_environment_list(self.project, self.policy,
                                       {'filter': 'reg'})
        text = result['content'][0]['text']
        self.assertIn('REGION', text)
        self.assertNotIn('OTHER', text)


class EnvironmentWriteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-envwrite-')
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
            'config': type('Config', (), {
                '_env_vars': {}, 'raw': {}})(),
        })()
        self.policy = McpPolicy(self.project)

    def env_path(self, name='environment'):
        return os.path.join(self.tmpdir, '.debops', 'mcp', name)

    def test_set_in_the_tracked_file_commits(self):
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, self.policy,
                {'name': 'REGION', 'value': 'eu-central-1'})
        self.assertFalse(result['isError'], result)
        self.assertIn('REGION = eu-central-1',
                      result['content'][0]['text'])
        with open(self.env_path()) as fp:
            self.assertEqual(fp.read(), 'REGION=eu-central-1\n')
        self.assertEqual(self.commits,
                         ['DebOps MCP: environment.set REGION'])

    def test_code_path_variables_are_refused(self):
        # A variable which redirects code loading plus a planted file
        # is controller-side code execution; the tools refuse them
        from debops.mcp.tools.environment import tool_environment_set
        for name in ('PATH', 'LD_PRELOAD', 'PYTHONPATH',
                     'ANSIBLE_CONFIG', 'ANSIBLE_LIBRARY',
                     'ANSIBLE_CALLBACK_PLUGINS', 'ANSIBLE_ROLES_PATH',
                     'ANSIBLE_COLLECTIONS_PATH'):
            result = tool_environment_set(
                    self.project, self.policy,
                    {'name': name, 'value': '/tmp/planted'})
            self.assertTrue(result['isError'], name)
            self.assertIn('redirects where code is loaded',
                          result['content'][0]['text'], name)
        self.assertFalse(os.path.exists(self.env_path()))

    def test_benign_variables_still_work(self):
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, self.policy,
                {'name': 'ANSIBLE_FORCE_COLOR', 'value': '1'})
        self.assertFalse(result['isError'], result)

    def test_policy_extends_the_deny_list(self):
        self.project.config.raw['project'] = {
                'mcp': {'environment_deny': ['AWS_PROFILE']}}
        policy = McpPolicy(self.project)
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, policy,
                {'name': 'AWS_PROFILE', 'value': 'prod'})
        self.assertTrue(result['isError'])
        self.assertIn('environment_deny', result['content'][0]['text'])
        # The built-in list still applies on top
        result = tool_environment_set(
                self.project, policy,
                {'name': 'PATH', 'value': '/tmp/x'})
        self.assertTrue(result['isError'])

    def test_set_preserves_comments_and_other_entries(self):
        os.makedirs(os.path.dirname(self.env_path()))
        with open(self.env_path(), 'w') as fp:
            fp.write('# a comment\nOTHER=1\n\n')
        from debops.mcp.tools.environment import tool_environment_set
        tool_environment_set(self.project, self.policy,
                             {'name': 'REGION', 'value': 'eu'})
        with open(self.env_path()) as fp:
            self.assertEqual(fp.read(),
                             '# a comment\nOTHER=1\n\nREGION=eu\n')

    def test_set_updates_in_place(self):
        from debops.mcp.tools.environment import tool_environment_set
        tool_environment_set(self.project, self.policy,
                             {'name': 'REGION', 'value': 'eu'})
        tool_environment_set(self.project, self.policy,
                             {'name': 'REGION', 'value': 'us'})
        with open(self.env_path()) as fp:
            self.assertEqual(fp.read(), 'REGION=us\n')

    def test_secret_set_is_never_committed(self):
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, self.policy,
                {'name': 'TOKEN', 'value': 's3cret', 'secret': True})
        self.assertFalse(result['isError'], result)
        # The result does not echo the value
        self.assertNotIn('s3cret', result['content'][0]['text'])
        path = self.env_path('.env')
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # Registered in the local exclude and not committed
        with open(os.path.join(self.tmpdir, '.git', 'info',
                               'exclude')) as fp:
            self.assertIn('/.debops/mcp/.env', fp.read().splitlines())
        self.assertEqual(self.commits, [])
        out = subprocess.run(['git', 'status', '--porcelain'],
                             cwd=self.tmpdir, capture_output=True,
                             text=True).stdout
        self.assertEqual(out, '')

    def test_secret_set_refused_when_tracked(self):
        os.makedirs(os.path.dirname(self.env_path('.env')))
        target = self.env_path('.env')
        with open(target, 'w') as fp:
            fp.write('TOKEN=old\n')
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='T', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='T', GIT_COMMITTER_EMAIL='t@t')
        subprocess.run(['git', 'add', '.debops/mcp/.env'],
                       cwd=self.tmpdir, env=env, check=True)
        subprocess.run(['git', 'commit', '-qm', 'track it'],
                       cwd=self.tmpdir, env=env, check=True)
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, self.policy,
                {'name': 'TOKEN', 'value': 'new', 'secret': True})
        self.assertTrue(result['isError'])
        self.assertIn('untrack', result['content'][0]['text'])
        with open(target) as fp:
            self.assertEqual(fp.read(), 'TOKEN=old\n')

    def test_unset(self):
        from debops.mcp.tools.environment import (tool_environment_set,
                                                  tool_environment_unset)
        tool_environment_set(self.project, self.policy,
                             {'name': 'REGION', 'value': 'eu'})
        result = tool_environment_unset(self.project, self.policy,
                                        {'name': 'REGION'})
        self.assertFalse(result['isError'], result)
        with open(self.env_path()) as fp:
            self.assertEqual(fp.read(), '\n')
        # Unsetting a missing variable is unchanged, not an error
        result = tool_environment_unset(self.project, self.policy,
                                        {'name': 'REGION'})
        self.assertIn('unchanged', result['content'][0]['text'])

    def test_invalid_names_rejected(self):
        from debops.mcp.tools.environment import tool_environment_set
        for name in ('1BAD', 'HAS SPACE', 'HAS-DASH', ''):
            result = tool_environment_set(
                    self.project, self.policy,
                    {'name': name, 'value': 'x'})
            self.assertTrue(result['isError'], name)
            self.assertIn('invalid variable name',
                          result['content'][0]['text'])

    def test_multiline_values_rejected(self):
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, self.policy,
                {'name': 'REGION', 'value': 'eu\nINJECT=1'})
        self.assertTrue(result['isError'])
        self.assertIn('must be a single line',
                      result['content'][0]['text'])

    def test_quoting_round_trip(self):
        from debops.mcp.tools.environment import tool_environment_set
        from debops.mcp.tools.common import mcp_environ
        value = 'with space and $pecial "quotes"'
        tool_environment_set(self.project, self.policy,
                             {'name': 'QUOTED', 'value': value})
        env = mcp_environ(self.project)
        self.assertEqual(env['QUOTED'], value)

    def test_non_git_project_refused(self):
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        from debops.mcp.tools.environment import tool_environment_set
        result = tool_environment_set(
                self.project, self.policy,
                {'name': 'REGION', 'value': 'eu'})
        self.assertTrue(result['isError'])
        self.assertIn('not a git repository',
                      result['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
