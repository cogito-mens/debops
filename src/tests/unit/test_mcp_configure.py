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

    def test_socket_mode_writes_the_bridge_command(self):
        configure_harness(self.tmpdir, 'opencode',
                          socket_path='/run/debops-mcp/x-system.sock')
        config = self.read_json(os.path.join('.opencode',
                                             'opencode.json'))
        self.assertEqual(
                config['mcp']['servers']['debops']['command'],
                ['debops', 'mcp', 'connect',
                 '/run/debops-mcp/x-system.sock'])

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


if __name__ == '__main__':
    unittest.main()
