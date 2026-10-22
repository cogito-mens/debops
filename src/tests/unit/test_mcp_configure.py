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
        # Direct tool calls, not the Code Mode wrapper — the
        # confirmation elicitations need them
        self.assertEqual(entry['codemode'], False)
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

    def test_pi_fresh_write(self):
        configure_harness(self.tmpdir, 'pi')
        config = self.read_json(os.path.join('.pi', 'mcp.json'))
        entry = config['mcpServers']['debops']
        self.assertEqual(entry['command'], 'debops')
        # Pi speaks the 2025-era revision of the protocol
        self.assertEqual(entry['args'], ['mcp', 'serve', '--legacy'])
        # Direct tool calls, not the codemode wrapper
        self.assertEqual(entry['exposure'], 'direct')
        self.assertIn('description', entry)

    def test_pi_merge_preserves_the_existing_content(self):
        self.write(os.path.join('.pi', 'mcp.json'),
                   json.dumps({'mcpServers': {'other': {
                           'command': 'other-mcp'}}}))
        configure_harness(self.tmpdir, 'pi')
        config = self.read_json(os.path.join('.pi', 'mcp.json'))
        self.assertEqual(config['mcpServers']['other']['command'],
                         'other-mcp')
        self.assertIn('debops', config['mcpServers'])

    def test_pi_carries_the_trust_and_elicitation_notes(self):
        _status, lines = configure_harness(self.tmpdir, 'pi')
        self.assertTrue(any('trusted' in line for line in lines), lines)
        self.assertTrue(any('confirm' in line for line in lines), lines)

    def test_antigravity_fresh_write(self):
        status, lines = configure_harness(self.tmpdir, 'antigravity')
        self.assertEqual(status, 'created')
        config = self.read_json(os.path.join('.agents',
                                             'mcp_config.json'))
        self.assertEqual(config['mcpServers']['debops'],
                         {'command': 'debops',
                          'args': ['mcp', 'serve', '--legacy']})
        self.assertTrue(any('legacy support only' in line
                            for line in lines), lines)

    def test_cursor_fresh_write(self):
        configure_harness(self.tmpdir, 'cursor')
        config = self.read_json(os.path.join('.cursor', 'mcp.json'))
        self.assertEqual(config['mcpServers']['debops']['args'],
                         ['mcp', 'serve', '--legacy'])

    def test_kiro_fresh_write(self):
        configure_harness(self.tmpdir, 'kiro')
        config = self.read_json(os.path.join('.kiro', 'settings',
                                             'mcp.json'))
        self.assertEqual(config['mcpServers']['debops']['args'],
                         ['mcp', 'serve', '--legacy'])

    def test_zed_fresh_write(self):
        configure_harness(self.tmpdir, 'zed')
        config = self.read_json(os.path.join('.zed', 'settings.json'))
        self.assertEqual(config['context_servers']['debops'],
                         {'command': 'debops',
                          'args': ['mcp', 'serve', '--legacy'],
                          'env': {}})

    def test_zed_merge_preserves_the_existing_settings(self):
        # The settings file carries the harness' whole configuration;
        # the entry merges in without touching it
        self.write(os.path.join('.zed', 'settings.json'),
                   json.dumps({'theme': 'Ayu Dark', 'vim_mode': True}))
        configure_harness(self.tmpdir, 'zed')
        config = self.read_json(os.path.join('.zed', 'settings.json'))
        self.assertEqual(config['theme'], 'Ayu Dark')
        self.assertTrue(config['vim_mode'])
        self.assertIn('debops', config['context_servers'])

    def test_zed_a_commented_settings_file_is_refused(self):
        # Zed settings are JSONC; a commented file is a hand-merge
        path = self.write(os.path.join('.zed', 'settings.json'),
                          '// my settings\n{}\n')
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'zed')
        self.assertIn('not plain JSON', str(context.exception))
        with open(path) as fp:
            self.assertTrue(fp.read().startswith('//'))

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

    def test_connect_writes_the_bridge_entry(self):
        socket_path = os.path.join(self.tmpdir, 'daemon.sock')
        status, lines = configure_harness(self.tmpdir, 'opencode',
                                          name='prod',
                                          connect=socket_path)
        self.assertEqual(status, 'created')
        config = self.read_json(os.path.join('.opencode',
                                             'opencode.json'))
        entry = config['mcp']['servers']['prod']
        self.assertEqual(entry['command'],
                         ['debops', 'mcp', 'connect', socket_path])
        # The socket does not exist yet; the note says so
        self.assertTrue(any('does not exist' in line
                            for line in lines), lines)

    def test_connect_refuses_a_non_socket_path(self):
        existing = self.write('not-a-socket', '')
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'opencode', name='prod',
                              connect=existing)
        self.assertIn('not a socket', str(context.exception))

    def test_connect_never_takes_the_default_name(self):
        # The default entry launches the server (the documentation
        # mode outside a project); a bridge must not replace it
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'opencode', name='debops',
                              connect='/run/x.sock')
        self.assertIn('its own --name', str(context.exception))

    def test_invalid_names_are_refused(self):
        for name in ('a b', 'prod/dev', ''):
            with self.assertRaises(ValueError, msg=name):
                configure_harness(self.tmpdir, 'opencode', name=name)

    def test_connect_entries_coexist_with_the_launcher(self):
        configure_harness(self.tmpdir, 'pi')
        configure_harness(self.tmpdir, 'pi', name='prod',
                          connect='/run/prod.sock')
        configure_harness(self.tmpdir, 'pi', name='dmz',
                          connect='/run/dmz.sock')
        config = self.read_json(os.path.join('.pi', 'mcp.json'))
        servers = config['mcpServers']
        self.assertEqual(sorted(servers), ['debops', 'dmz', 'prod'])
        self.assertEqual(servers['debops']['args'][:2],
                         ['mcp', 'serve'])
        self.assertEqual(servers['prod']['args'],
                         ['mcp', 'connect', '/run/prod.sock'])

    def test_verbosity_and_log_ride_the_serve_entry(self):
        configure_harness(self.tmpdir, 'opencode', verbosity=3,
                          log='/var/log/debops-mcp.log')
        config = self.read_json(os.path.join('.opencode',
                                             'opencode.json'))
        self.assertEqual(
                config['mcp']['servers']['debops']['command'],
                ['debops', 'mcp', 'serve', '--log',
                 '/var/log/debops-mcp.log', '-vvv'])

    def test_docs_rides_the_serve_entry(self):
        # The documentation mode, enforced in the spawned server
        configure_harness(self.tmpdir, 'opencode', docs=True,
                          verbosity=2, log='/var/log/debops-mcp.log')
        config = self.read_json(os.path.join('.opencode',
                                             'opencode.json'))
        self.assertEqual(
                config['mcp']['servers']['debops']['command'],
                ['debops', 'mcp', 'serve', '--docs', '--log',
                 '/var/log/debops-mcp.log', '-vv'])

    def test_docs_refuses_a_bridge(self):
        # A bridge takes the daemon's mode; an enforcement flag must
        # never silently drop
        with self.assertRaises(ValueError) as context:
            configure_harness(self.tmpdir, 'claude', name='prod',
                              connect='/run/prod.sock', docs=True)
        self.assertIn('--docs', str(context.exception))

    def test_bare_log_means_syslog(self):
        configure_harness(self.tmpdir, 'claude', log='')
        config = self.read_json('.mcp.json')
        self.assertEqual(config['mcpServers']['debops']['args'],
                         ['mcp', 'serve', '--log'])

    def test_connect_ignores_the_debug_flags_with_a_note(self):
        _status, lines = configure_harness(
                self.tmpdir, 'claude', name='prod',
                connect='/run/prod.sock', verbosity=3, log='')
        config = self.read_json('.mcp.json')
        self.assertEqual(config['mcpServers']['prod']['args'],
                         ['mcp', 'connect', '/run/prod.sock'])
        self.assertTrue(any('dumb relay' in line for line in lines),
                        lines)

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
        self.assertEqual(HARNESSES['antigravity'].default_filename,
                         os.path.join('.agents', 'mcp_config.json'))
        self.assertEqual(HARNESSES['cursor'].default_filename,
                         os.path.join('.cursor', 'mcp.json'))
        self.assertEqual(HARNESSES['kiro'].default_filename,
                         os.path.join('.kiro', 'settings', 'mcp.json'))
        self.assertEqual(HARNESSES['zed'].default_filename,
                         os.path.join('.zed', 'settings.json'))


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


class AgentsMdTestCase(unittest.TestCase):
    """The AGENTS.md guidance section ('configure --agents')."""

    def setUp(self):
        from debops.mcp.harnesses import configure_agents_md
        self.configure = configure_agents_md
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-agents-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = os.path.join(self.tmpdir, 'AGENTS.md')

    def read(self):
        with open(self.path) as fp:
            return fp.read()

    def test_creates_the_file(self):
        status, _lines = self.configure(self.tmpdir)
        self.assertEqual(status, 'created')
        content = self.read()
        self.assertIn('## MCP server', content)
        self.assertIn('Prefer them over reading the project files',
                      content)
        self.assertIn('<!-- debops mcp: begin -->', content)

    def test_rerun_is_unchanged(self):
        self.configure(self.tmpdir)
        status, _lines = self.configure(self.tmpdir)
        self.assertEqual(status, 'unchanged')

    def test_updates_between_the_markers_only(self):
        with open(self.path, 'w') as fp:
            fp.write('# My project\n\n<!-- debops mcp: begin -->\n'
                     'stale\n<!-- debops mcp: end -->\n'
                     '## My own section\n')
        status, _lines = self.configure(self.tmpdir)
        self.assertEqual(status, 'updated')
        content = self.read()
        self.assertIn('# My project', content)
        self.assertIn('## My own section', content)
        self.assertNotIn('stale', content)
        self.assertIn('`check` before `run`', content)

    def test_appends_to_an_unmarked_file(self):
        with open(self.path, 'w') as fp:
            fp.write('# My project\n')
        status, _lines = self.configure(self.tmpdir)
        self.assertEqual(status, 'updated')
        content = self.read()
        self.assertTrue(content.startswith('# My project\n'))
        self.assertIn('## MCP server', content)

    def test_needs_a_project_unless_printing(self):
        with self.assertRaises(ValueError):
            self.configure(None)
        status, lines = self.configure(None, print_only=True)
        self.assertEqual(status, 'printed')
        self.assertIn('## MCP server', '\n'.join(lines))

    def test_claude_with_a_claude_md_gets_the_import_note(self):
        with open(os.path.join(self.tmpdir, 'CLAUDE.md'), 'w') as fp:
            fp.write('# Claude notes\n')
        _status, lines = self.configure(self.tmpdir,
                                        harness_name='claude')
        self.assertTrue(any('@AGENTS.md' in line for line in lines))

    def test_the_docs_variant_is_read_only(self):
        # The entry enforces the documentation mode; the guidance must
        # not nudge the agent towards the execution tools it lacks
        status, _lines = self.configure(self.tmpdir, docs=True)
        self.assertEqual(status, 'created')
        content = self.read()
        self.assertIn('read-only', content)
        self.assertIn('ansible.doc', content)
        for word in ('`run`', '`check`', 'project.read'):
            self.assertNotIn(word, content, word)

    def test_the_variant_switches_in_place(self):
        self.configure(self.tmpdir)
        status, _lines = self.configure(self.tmpdir, docs=True)
        self.assertEqual(status, 'updated')
        self.assertIn('read-only', self.read())
        status, _lines = self.configure(self.tmpdir)
        self.assertEqual(status, 'updated')
        self.assertIn('`check` before `run`', self.read())


class DisconnectTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-disconnect-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # A management project: the launcher, two bridges, one foreign
        configure_harness(self.tmpdir, 'opencode')
        configure_harness(self.tmpdir, 'opencode', name='prod',
                          connect='/run/prod.sock')
        configure_harness(self.tmpdir, 'opencode', name='dmz',
                          connect='/run/dmz.sock')
        with open(os.path.join(self.tmpdir, '.opencode',
                               'opencode.json')) as fp:
            data = json.load(fp)
        data['mcp']['servers']['grafana'] = {
                'type': 'remote', 'url': 'https://x.invalid'}
        with open(os.path.join(self.tmpdir, '.opencode',
                               'opencode.json'), 'w') as fp:
            json.dump(data, fp)

    def read_json(self, relative):
        with open(os.path.join(self.tmpdir, relative)) as fp:
            return json.load(fp)

    def read_servers(self):
        return self.read_json(os.path.join(
                '.opencode', 'opencode.json'))['mcp']['servers']

    def test_disconnect_removes_the_bridge(self):
        from debops.mcp.harnesses import disconnect_harness
        status, _lines = disconnect_harness(self.tmpdir, 'opencode',
                                            'prod')
        self.assertEqual(status, 'removed')
        self.assertEqual(sorted(self.read_servers()),
                         ['debops', 'dmz', 'grafana'])

    def test_disconnect_of_a_missing_entry_is_a_noop(self):
        from debops.mcp.harnesses import disconnect_harness
        status, _lines = disconnect_harness(self.tmpdir, 'opencode',
                                            'missing')
        self.assertEqual(status, 'unchanged')

    def test_disconnect_refuses_the_launcher(self):
        from debops.mcp.harnesses import disconnect_harness
        with self.assertRaises(ValueError) as context:
            disconnect_harness(self.tmpdir, 'opencode', 'debops')
        self.assertIn('--remove', str(context.exception))

    def test_disconnect_refuses_a_foreign_entry(self):
        from debops.mcp.harnesses import disconnect_harness
        with self.assertRaises(ValueError) as context:
            disconnect_harness(self.tmpdir, 'opencode', 'grafana')
        self.assertIn('not ours', str(context.exception))

    def test_remove_clears_all_of_our_entries(self):
        from debops.mcp.harnesses import remove_harness
        status, _lines = remove_harness(self.tmpdir, 'opencode')
        self.assertEqual(status, 'removed')
        # Only the foreign entry stays
        self.assertEqual(sorted(self.read_servers()), ['grafana'])

    def test_remove_with_a_name_removes_the_launcher(self):
        from debops.mcp.harnesses import remove_harness
        status, _lines = remove_harness(self.tmpdir, 'opencode',
                                        name='debops')
        self.assertEqual(status, 'removed')
        self.assertEqual(sorted(self.read_servers()),
                         ['dmz', 'grafana', 'prod'])

    def test_remove_without_a_file_is_not_configured(self):
        from debops.mcp.harnesses import remove_harness
        empty = os.path.join(self.tmpdir, 'elsewhere')
        os.makedirs(empty)
        status, lines = remove_harness(empty, 'opencode')
        self.assertEqual(status, 'not configured')
        self.assertIn('Nothing', lines[0])


if __name__ == '__main__':
    unittest.main()
