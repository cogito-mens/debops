# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the harness integration ('debops mcp configure').

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The harness configuration files are plain data in temporary
project directories.
"""

from debops.mcp.harnesses import (configure_harness, fragment_for_display,
                                  HARNESSES)
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import toml


class ConfigureTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-configure-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def read_json(self, relative):
        with open(os.path.join(self.tmpdir, relative)) as fp:
            return json.load(fp)

    def write(self, relative, content):
        path = os.path.join(self.tmpdir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fp:
            fp.write(content)
        return path

    def test_opencode_fresh_write(self):
        status, lines = configure_harness(self.tmpdir, 'opencode')
        self.assertEqual(status, 'created')
        config = self.read_json(os.path.join('.opencode',
                                             'opencode.json'))
        entry = config['mcp']['servers']['debops']
        self.assertEqual(entry['type'], 'local')
        self.assertEqual(entry['command'], ['debops', 'mcp', 'serve'])
        # The modern protocol revision is pinned: the harness' default
        # is the legacy handshake, which the server does not speak
        self.assertEqual(entry['protocol'], '2026-07-28')
        self.assertIn('.opencode/opencode.json', lines[0])

    def test_merge_preserves_the_existing_content(self):
        self.write(os.path.join('.opencode', 'opencode.json'),
                   json.dumps({'mcp': {'servers': {'other': {
                           'type': 'remote', 'url': 'https://x.invalid'}}},
                           'model': 'acme/wise'}))
        configure_harness(self.tmpdir, 'opencode')
        config = self.read_json(os.path.join('.opencode',
                                             'opencode.json'))
        self.assertEqual(config['model'], 'acme/wise')
        self.assertEqual(config['mcp']['servers']['other']['type'],
                         'remote')
        self.assertIn('debops', config['mcp']['servers'])

    def test_rerun_is_idempotent(self):
        configure_harness(self.tmpdir, 'opencode')
        path = os.path.join(self.tmpdir, '.opencode', 'opencode.json')
        with open(path) as fp:
            before = fp.read()
        status, _lines = configure_harness(self.tmpdir, 'opencode')
        self.assertEqual(status, 'unchanged')
        with open(path) as fp:
            self.assertEqual(fp.read(), before)

    def test_rerun_never_duplicates_the_entry(self):
        configure_harness(self.tmpdir, 'claude')
        configure_harness(self.tmpdir, 'claude')
        with open(os.path.join(self.tmpdir, '.mcp.json')) as fp:
            text = fp.read()
        self.assertEqual(text.count('"debops":'), 1)

    def test_a_broken_entry_is_repaired(self):
        # What 'opencode mcp add --wizard' wrote: the whole command as
        # one string, and no protocol pin
        broken = self.write('opencode.json', json.dumps({
                'mcp': {'servers': {'debops': {
                        'type': 'local',
                        'command': ['debops mcp serve']}}}}))
        configure_harness(self.tmpdir, 'opencode', filename=broken)
        config = self.read_json('opencode.json')
        entry = config['mcp']['servers']['debops']
        self.assertEqual(entry['command'], ['debops', 'mcp', 'serve'])
        self.assertEqual(entry['protocol'], '2026-07-28')

    def test_root_opencode_file_gets_a_precedence_note(self):
        self.write('opencode.json', '{}')
        _status, lines = configure_harness(self.tmpdir, 'opencode')
        self.assertTrue(any('takes precedence' in line
                            for line in lines), lines)

    def test_jsonc_is_refused_and_the_file_untouched(self):
        path = self.write(os.path.join('.opencode', 'opencode.json'),
                          '{\n  // a comment\n  "mcp": {}\n}\n')
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'opencode')
        self.assertIn('merge by hand', str(context.exception))
        with open(path) as fp:
            self.assertIn('// a comment', fp.read())
        # The refusal path can show what to add
        self.assertIn('"debops"', fragment_for_display('opencode'))

    def test_claude_fresh_write(self):
        configure_harness(self.tmpdir, 'claude')
        config = self.read_json('.mcp.json')
        entry = config['mcpServers']['debops']
        self.assertEqual(entry['type'], 'stdio')
        self.assertEqual(entry['command'], 'debops')
        self.assertEqual(entry['args'], ['mcp', 'serve'])

    def test_codex_fresh_write(self):
        configure_harness(self.tmpdir, 'codex')
        with open(os.path.join(self.tmpdir, '.codex', 'config.toml')) \
                as fp:
            config = toml.loads(fp.read())
        entry = config['mcp_servers']['debops']
        self.assertEqual(entry['command'], 'debops')
        self.assertEqual(entry['args'], ['mcp', 'serve'])

    def test_codex_merge_preserves_the_existing_content(self):
        self.write(os.path.join('.codex', 'config.toml'),
                   'model = "acme-wise"\n\n[mcp_servers.docs]\n'
                   'command = "docs-mcp"\n')
        configure_harness(self.tmpdir, 'codex')
        with open(os.path.join(self.tmpdir, '.codex', 'config.toml')) \
                as fp:
            config = toml.loads(fp.read())
        self.assertEqual(config['model'], 'acme-wise')
        self.assertEqual(config['mcp_servers']['docs']['command'],
                         'docs-mcp')
        self.assertIn('debops', config['mcp_servers'])

    def test_codex_commented_file_is_refused(self):
        path = self.write(os.path.join('.codex', 'config.toml'),
                          '# hand-tended\nmodel = "acme-wise"\n')
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'codex')
        self.assertIn('comments', str(context.exception))
        with open(path) as fp:
            self.assertEqual(fp.read(), '# hand-tended\n'
                                        'model = "acme-wise"\n')

    def test_codex_carries_a_trust_note(self):
        _status, lines = configure_harness(self.tmpdir, 'codex')
        self.assertTrue(any('trusted' in line for line in lines), lines)

    def test_print_only_needs_no_project(self):
        # Printing asks for no directory at all
        status, lines = configure_harness(None, 'opencode',
                                          print_only=True)
        self.assertEqual(status, 'printed')
        self.assertIn('"mcp"', '\n'.join(lines))

    def test_unknown_harness_is_refused(self):
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'windsurf')
        self.assertIn('unknown harness', str(context.exception))
        self.assertIn('opencode', str(context.exception))

    def test_filename_override(self):
        custom = self.write('custom.json', '{}')
        configure_harness(self.tmpdir, 'claude', filename=custom)
        config = self.read_json('custom.json')
        self.assertIn('debops', config['mcpServers'])
        self.assertFalse(os.path.exists(
                os.path.join(self.tmpdir, '.mcp.json')))

    def test_filename_may_be_absolute(self):
        path = os.path.join(self.tmpdir, 'absolute.json')
        configure_harness(self.tmpdir, 'claude', filename=path)
        self.assertTrue(os.path.isfile(path))

    def test_default_files_are_recorded(self):
        self.assertEqual(HARNESSES['opencode'].default_filename,
                         os.path.join('.opencode', 'opencode.json'))
        self.assertEqual(HARNESSES['claude'].default_filename,
                         '.mcp.json')
        self.assertEqual(HARNESSES['codex'].default_filename,
                         os.path.join('.codex', 'config.toml'))


class UserModeTestCase(unittest.TestCase):
    """The account-level configuration ('--user'), needing no
    project."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-cfguser-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # The account-level paths resolve at call time from the
        # environment
        patcher = mock.patch.dict(os.environ, {'HOME': self.tmpdir})
        patcher.start()
        self.addCleanup(patcher.stop)
        # patch.dict restores the whole environment on exit
        os.environ.pop('XDG_CONFIG_HOME', None)

    def test_user_mode_writes_the_account_config(self):
        # The spawn entry works from anywhere: a project directory gets
        # the project mode, anywhere else answers the documentation
        # mode
        status, _lines = configure_harness(None, 'opencode', user=True)
        self.assertEqual(status, 'created')
        with open(os.path.join(
                self.tmpdir, '.config', 'opencode', 'opencode.json')) \
                as fp:
            config = json.load(fp)
        entry = config['mcp']['servers']['debops']
        self.assertEqual(entry['command'], ['debops', 'mcp', 'serve'])
        self.assertEqual(entry['protocol'], '2026-07-28')

    def test_user_mode_respects_xdg_config_home(self):
        xdg = os.path.join(self.tmpdir, 'xdg')
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': xdg}):
            configure_harness(None, 'opencode', user=True)
        self.assertTrue(os.path.isfile(
                os.path.join(xdg, 'opencode', 'opencode.json')))

    def test_user_mode_claude(self):
        configure_harness(None, 'claude', user=True)
        with open(os.path.join(self.tmpdir, '.claude.json')) as fp:
            config = json.load(fp)
        self.assertEqual(config['mcpServers']['debops']['args'],
                         ['mcp', 'serve'])

    def test_user_mode_codex(self):
        configure_harness(None, 'codex', user=True)
        with open(os.path.join(self.tmpdir, '.codex',
                               'config.toml')) as fp:
            config = toml.loads(fp.read())
        self.assertEqual(config['mcp_servers']['debops']['args'],
                         ['mcp', 'serve'])

    def test_user_mode_merges_a_busy_state_file(self):
        # Claude Code's user file holds much more than MCP servers
        with open(os.path.join(self.tmpdir, '.claude.json'), 'w') as fp:
            json.dump({'projects': {'/old': {}}, 'oauth': {'x': 1},
                       'mcpServers': {'other': {'command': 'x'}}}, fp)
        configure_harness(None, 'claude', user=True)
        with open(os.path.join(self.tmpdir, '.claude.json')) as fp:
            config = json.load(fp)
        self.assertEqual(config['projects'], {'/old': {}})
        self.assertEqual(config['oauth'], {'x': 1})
        self.assertIn('other', config['mcpServers'])
        self.assertIn('debops', config['mcpServers'])

    def test_project_mode_needs_a_project(self):
        with self.assertRaises(ValueError) as context:
            configure_harness(None, 'opencode')
        self.assertIn('not a DebOps project', str(context.exception))

    def test_print_only_writes_nothing(self):
        status, lines = configure_harness(self.tmpdir, 'claude',
                                          print_only=True)
        self.assertEqual(status, 'printed')
        self.assertIn('"mcpServers"', '\n'.join(lines))
        self.assertFalse(os.path.exists(
                os.path.join(self.tmpdir, '.mcp.json')))

    def test_user_mode_filename_override_may_be_relative(self):
        # A relative override in account mode resolves against the
        # current directory
        old_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        try:
            configure_harness(None, 'opencode', user=True,
                              filename='custom.json')
            self.assertTrue(os.path.isfile(
                    os.path.join(self.tmpdir, 'custom.json')))
        finally:
            os.chdir(old_cwd)


if __name__ == '__main__':
    unittest.main()
