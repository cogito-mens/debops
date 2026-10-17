# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server policy.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.policy import McpPolicy, KillSwitch
import os
import shutil
import tempfile
import toml
import unittest


class FakeProject(object):
    """A stand-in for ProjectDir with only the attributes policy needs."""

    def __init__(self, path, project_type='legacy', view='system',
                 raw_config=None):
        self.path = path
        self.name = os.path.basename(path)
        self.project_type = project_type
        self.view = view
        self.config = type('Config', (), {'raw': raw_config or {}})()


class ViewRootTestCase(unittest.TestCase):

    def test_legacy_view_root(self):
        project = FakeProject('/srv/projects/example', 'legacy')
        policy = McpPolicy(project)
        self.assertEqual(policy.view_root, '/srv/projects/example/ansible')

    def test_modern_view_root(self):
        project = FakeProject('/srv/projects/example', 'modern', 'infra')
        policy = McpPolicy(project)
        self.assertEqual(policy.view_root,
                         '/srv/projects/example/ansible/views/infra')


class ConfineTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-policy-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'inventory'))
        self.policy = McpPolicy(FakeProject(self.tmpdir, 'legacy'))

    def test_path_inside_view_root(self):
        confined = self.policy.confine('inventory/hosts')
        self.assertEqual(confined, os.path.join(self.policy.view_root,
                                                'inventory', 'hosts'))

    def test_view_root_itself_allowed(self):
        self.assertEqual(self.policy.confine(''), self.policy.view_root)

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            self.policy.confine('../outside')

    def test_absolute_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            self.policy.confine('/etc/passwd')

    def test_symlink_escape_rejected(self):
        outside = tempfile.mkdtemp(prefix='debops-mcp-outside-')
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        os.symlink(outside, os.path.join(self.policy.view_root, 'link'))
        with self.assertRaises(ValueError):
            self.policy.confine('link')


class ToolAccessTestCase(unittest.TestCase):

    def test_all_tools_allowed_by_default(self):
        policy = McpPolicy(FakeProject('/srv/projects/example'))
        self.assertTrue(policy.is_tool_allowed('role_info'))
        self.assertTrue(policy.is_tool_allowed('anything'))

    def test_deny_list(self):
        policy = McpPolicy(FakeProject(
                '/srv/projects/example',
                raw_config={'project': {'mcp': {'deny': ['role_info']}}}))
        self.assertFalse(policy.is_tool_allowed('role_info'))
        self.assertTrue(policy.is_tool_allowed('role_variables'))

    def test_allow_list_restricts(self):
        policy = McpPolicy(FakeProject(
                '/srv/projects/example',
                raw_config={'project': {'mcp': {'allow': ['role_info']}}}))
        self.assertTrue(policy.is_tool_allowed('role_info'))
        self.assertFalse(policy.is_tool_allowed('role_variables'))

    def test_deny_wins_over_allow(self):
        policy = McpPolicy(FakeProject(
                '/srv/projects/example',
                raw_config={'project': {'mcp': {'allow': ['role_info'],
                                                'deny': ['role_info']}}}))
        self.assertFalse(policy.is_tool_allowed('role_info'))

    def test_misshapen_configuration_tolerated(self):
        policy = McpPolicy(FakeProject(
                '/srv/projects/example',
                raw_config={'project': {'mcp': 'bogus'}}))
        self.assertTrue(policy.is_tool_allowed('role_info'))


class KillSwitchTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-kill-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.conf_d = os.path.join(self.tmpdir, 'conf.d')
        os.makedirs(self.conf_d)
        self.marker = os.path.join(self.conf_d, 'mcp-killswitch.toml')

    def _factory(self):
        def factory():
            raw = {}
            if os.path.isfile(self.marker):
                with open(self.marker) as fp:
                    raw = toml.loads(fp.read())
            return type('Config', (), {'raw': raw,
                                       '_config_dirs': [self.conf_d]})()
        return factory

    def test_enabled_by_default(self):
        switch = KillSwitch(config_factory=self._factory())
        self.assertTrue(switch.is_enabled())

    def test_disabled_by_configuration(self):
        with open(self.marker, 'w') as fp:
            fp.write('[mcp]\nenabled = false\n')
        switch = KillSwitch(config_factory=self._factory())
        self.assertFalse(switch.is_enabled())

    def test_reloads_when_configuration_changes(self):
        switch = KillSwitch(config_factory=self._factory())
        self.assertTrue(switch.is_enabled())
        with open(self.marker, 'w') as fp:
            fp.write('[mcp]\nenabled = false\n')
        self.assertFalse(switch.is_enabled())
        os.unlink(self.marker)
        self.assertTrue(switch.is_enabled())


if __name__ == '__main__':
    unittest.main()
