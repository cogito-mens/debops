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
import tempfile
import unittest
from unittest import mock


def _auto_commit_noop(self, message=None):
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


if __name__ == '__main__':
    unittest.main()
