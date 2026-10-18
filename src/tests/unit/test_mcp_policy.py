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


class ResourcePolicyTestCase(unittest.TestCase):

    def _policy(self, mcp_config):
        return McpPolicy(FakeProject('/srv/projects/example',
                                     raw_config={'project': {'mcp':
                                                             mcp_config}}))

    def test_min_free_default(self):
        self.assertEqual(self._policy({}).min_free_bytes,
                         64 * 1024 * 1024)

    def test_min_free_override(self):
        self.assertEqual(self._policy({'min_free_mb': 128})
                         .min_free_bytes, 128 * 1024 * 1024)

    def test_min_free_bad_value_falls_back(self):
        self.assertEqual(self._policy({'min_free_mb': 'lots'})
                         .min_free_bytes, 64 * 1024 * 1024)


class McpConfigTemplateTestCase(unittest.TestCase):
    """The shipped 'mcp' configuration template must not drift from the
    defaults in the code."""

    def _template_values(self):
        import jinja2
        import toml
        from debops.projectdir import _read_config_template
        rendered = jinja2.Template(_read_config_template('mcp.toml'),
                                   trim_blocks=True).render(
                env={}, view='system', default_view='system',
                project={'name': 'example', 'path': '/srv/x',
                         'type': 'modern'})
        return toml.loads(rendered)['project']['mcp']

    def test_template_matches_the_policy_defaults(self):
        from debops.mcp.policy import _canonical_execute_option
        values = self._template_values()
        policy = McpPolicy(FakeProject('/srv/projects/example'))

        self.assertEqual(values['execute'], policy.execute)
        self.assertEqual(values['execute_timeout'], policy.execute_timeout)
        self.assertEqual(values['sync_interval'], policy.sync_interval)
        self.assertEqual(values['min_free_mb'],
                         policy.min_free_bytes // (1024 * 1024))
        self.assertEqual(sorted(values['confirm']),
                         sorted(policy.confirm))
        self.assertEqual(values['write_allow'], policy.write_allow)
        # The template uses the CLI spelling of the options; the policy
        # canonicalizes them
        self.assertEqual([_canonical_execute_option(option)
                          for option in values['execute_options']],
                         policy.execute_options)
        self.assertEqual(values['bundled_content'],
                         policy.bundled_content)
        self.assertEqual(values['external_content'],
                         policy.external_content)


class ConfirmationPolicyTestCase(unittest.TestCase):

    def _policy(self, mcp_config):
        return McpPolicy(FakeProject('/srv/projects/example',
                                     raw_config={'project': {'mcp':
                                                             mcp_config}}))

    def test_default_confirmations(self):
        policy = self._policy({})
        self.assertEqual(policy.confirm, {'run', 'project_push'})

    def test_confirm_list_overrides(self):
        policy = self._policy({'confirm': ['run']})
        self.assertEqual(policy.confirm, {'run'})

    def test_confirm_empty_disables(self):
        policy = self._policy({'confirm': []})
        self.assertEqual(policy.confirm, set())

    def test_confirm_string_normalized(self):
        policy = self._policy({'confirm': 'run'})
        self.assertEqual(policy.confirm, {'run'})


class ContentPolicyTestCase(unittest.TestCase):

    def _policy(self, mcp_config):
        return McpPolicy(FakeProject('/srv/projects/example',
                                     raw_config={'project': {'mcp':
                                                             mcp_config}}))

    def test_content_allowed_by_default(self):
        policy = self._policy({})
        self.assertTrue(policy.bundled_content)
        self.assertTrue(policy.external_content)

    def test_content_flags(self):
        policy = self._policy({'bundled_content': False,
                               'external_content': False})
        self.assertFalse(policy.bundled_content)
        self.assertFalse(policy.external_content)

    def test_string_values_parsed(self):
        policy = self._policy({'bundled_content': 'false',
                               'external_content': 'yes'})
        self.assertFalse(policy.bundled_content)
        self.assertTrue(policy.external_content)


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
