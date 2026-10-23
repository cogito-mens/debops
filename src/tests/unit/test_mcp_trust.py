# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the trust model: the passphrase, the bearer tokens,
the elevation."""

import os
import logging
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, PROTOCOL_VERSION
from debops.mcp import trust
from tests.unit.test_projectdir import make_project_at

REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {'elicitation': {}},
}


def _request(method, params, request_id=1, token=None):
    meta = dict(REQUEST_META)
    if token is not None:
        meta['org.debops/token'] = token
    return {'jsonrpc': '2.0', 'id': request_id, 'method': method,
            'params': dict(params or {}, _meta=meta)}


class PassphraseTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-trust-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = make_project_at(self.tmpdir)
        self.project.view = 'system'

    def test_four_words_from_the_list_space_joined(self):
        from debops.constants import DEBOPS_PACKAGE_DATA
        with open(os.path.join(DEBOPS_PACKAGE_DATA, 'mcp', 'wordlists',
                               'eff_large_wordlist.txt')) as fp:
            words = set(line.strip() for line in fp)
        phrase = trust.word_passphrase()
        parts = phrase.split(' ')
        self.assertEqual(len(parts), 4)
        # Space-joined for the human typing it
        self.assertNotIn('-', phrase)
        for word in parts:
            self.assertIn(word, words)

    def test_normalize_folds_case_hyphens_and_whitespace(self):
        self.assertEqual(trust.normalize_passphrase(
                '  Correct  Horse\tBATTERY-Staple \n'),
                'correct horse battery staple')

    def test_matches_is_normalization_invariant(self):
        self.assertTrue(trust.passphrase_matches(
                'correct-horse  BATTERY staple',
                'correct horse battery staple'))
        self.assertFalse(trust.passphrase_matches(
                'correct horse battery',
                'correct horse battery staple'))

    def test_ensure_creates_the_file_once_with_the_permissions(self):
        self.assertTrue(trust.ensure_passphrase(self.project))
        # And never twice
        self.assertFalse(trust.ensure_passphrase(self.project))
        path = os.path.join(self.tmpdir, trust.TRUST_PATH)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(
                os.path.dirname(path)).st_mode), 0o700)
        with open(path) as fp:
            self.assertEqual(len(fp.read().split()), 4)
        # The marker ignores the whole directory, itself included —
        # 'git status' never mentions it
        with open(os.path.join(os.path.dirname(path),
                               '.gitignore')) as fp:
            self.assertEqual(fp.read(), '*\n')

    def test_an_unwritable_secret_directory_is_not_fatal(self):
        # A project shared between accounts: the secret directory of
        # one account's daemon is closed to the other's — serve
        # without the trust rather than not at all
        blocker = os.path.join(self.tmpdir,
                               os.path.dirname(trust.TRUST_PATH))
        os.makedirs(os.path.dirname(blocker))
        with open(blocker, 'w') as fp:
            fp.write('not a directory\n')
        self.assertFalse(trust.ensure_passphrase(self.project))

    def test_the_marker_is_repaired_to_the_invisible_form(self):
        trust.ensure_passphrase(self.project)
        path = os.path.join(self.tmpdir, trust.TRUST_PATH)
        marker = os.path.join(os.path.dirname(path), '.gitignore')
        with open(marker, 'w') as fp:
            fp.write('*\n!.gitignore\n')  # the first form
        self.assertFalse(trust.ensure_passphrase(self.project))
        with open(marker) as fp:
            self.assertEqual(fp.read(), '*\n')

    def test_git_never_sees_the_passphrase(self):
        # The operator's 'git add -A' and the MCP commit machinery
        # alike: the directory is ignored wholesale
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       env=env, check=True)
        trust.ensure_passphrase(self.project)
        subprocess.run(['git', 'add', '-A'], cwd=self.tmpdir, env=env,
                       check=True)
        status = subprocess.run(['git', 'status', '--porcelain'],
                                cwd=self.tmpdir, env=env,
                                capture_output=True, text=True)
        # Nothing staged at all: the marker stays invisible too
        self.assertEqual(status.stdout, '')


class DenyListTestCase(unittest.TestCase):
    """The trust file is unreachable through the MCP tools."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-deny-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = make_project_at(self.tmpdir)
        self.project.view = 'system'
        self.policy = McpPolicy(self.project)

    def test_the_read_deny(self):
        self.assertTrue(self.policy.is_read_denied(
                '.debops/mcp/secret/trust'))
        self.assertTrue(self.policy.is_read_denied(
                '.debops/mcp/secret/.gitignore'))


class TrustFlowTestCase(unittest.TestCase):
    """The authenticate/elevate flow against a server."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-flow-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = make_project_at(self.tmpdir)
        self.project.view = 'system'
        self.project.view = 'system'
        self.policy = McpPolicy(self.project)
        self.server = McpServer(project=self.project, policy=self.policy)
        trust.ensure_passphrase(self.project)
        with open(os.path.join(self.tmpdir, trust.TRUST_PATH)) as fp:
            self.passphrase = fp.read()
        # A confirm-gated probe tool
        self.server.register_tool(
                'probe', 'the probe', {'type': 'object'},
                lambda arguments: {'content': [{'type': 'text',
                                                'text': 'probed'}],
                                   'isError': False})
        self.policy.confirm = {'probe'}

    def call(self, method, params=None, token=None):
        return self.server.handle(_request(method, params,
                                           token=token))

    def authenticate(self, passphrase=None):
        response = self.call('server/authenticate',
                             {'passphrase': (self.passphrase
                                             if passphrase is None
                                             else passphrase)})
        return response

    def test_the_full_flow(self):
        answer = self.authenticate()
        self.assertNotIn('error', answer)
        result = answer['result']
        self.assertEqual(result['tokenType'], 'bearer')
        self.assertEqual(result['expiresIn'], 43200)
        token = result['token']

        # A gated tool: trusted alone still asks; elevated proceeds
        gated = self.call('tools/call', {'name': 'probe',
                                         'arguments': {}},
                          token=token)
        self.assertEqual(gated['result']['resultType'],
                         'input_required')

        answer = self.call('server/elevate',
                           {'passphrase': self.passphrase},
                           token=token)
        self.assertNotIn('error', answer)
        self.assertTrue(answer['result']['elevated'])
        self.assertEqual(answer['result']['expiresIn'], 900)

        ran = self.call('tools/call', {'name': 'probe',
                                       'arguments': {}},
                        token=token)
        self.assertEqual(ran['result']['content'][0]['text'],
                         'probed')

    def test_the_elevation_ttl_slides_with_use(self):
        token = self.authenticate()['result']['token']
        self.call('server/elevate', {'passphrase': self.passphrase},
                  token=token)
        first = self.server._bearers[token]['elevated_until']
        import time
        with unittest.mock.patch('debops.mcp.server.time.monotonic',
                                 return_value=time.monotonic() + 60):
            self.call('tools/list', {}, token=token)
        later = self.server._bearers[token]['elevated_until']
        self.assertGreater(later, first)

    def test_a_wrong_passphrase_then_the_rate_limit(self):
        answer = self.authenticate('wrong horse')
        self.assertIn('error', answer)
        self.assertIn('did not match', answer['error']['data']
                      ['details'])
        # The next attempt right away is rate-limited
        answer = self.authenticate(self.passphrase)
        self.assertIn('error', answer)
        self.assertIn('retry in', answer['error']['data']['details'])

    def test_elevation_needs_a_session_first(self):
        answer = self.call('server/elevate',
                           {'passphrase': self.passphrase})
        self.assertIn('error', answer)
        self.assertIn('authenticate', answer['error']['data']
                      ['details'])

    def test_an_expired_token_is_no_token(self):
        token = self.authenticate()['result']['token']
        self.server._bearers[token]['expires'] = 0.0
        gated = self.call('tools/call', {'name': 'probe',
                                         'arguments': {}},
                          token=token)
        # Untrusted again: the gate fires
        self.assertEqual(gated['result']['resultType'],
                         'input_required')

    def test_docs_mode_has_no_trust(self):
        server = McpServer(project=None,
                           policy=McpPolicy.for_documentation())
        response = server.handle(_request('server/authenticate',
                                          {'passphrase': 'x'}))
        self.assertIn('error', response)
        self.assertIn('documentation mode',
                      response['error']['data']['details'])

    def test_legacy_mode_has_no_trust(self):
        server = McpServer(project=self.project, policy=self.policy,
                           legacy=True)
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1,
                'method': 'server/authenticate',
                'params': {'passphrase': self.passphrase}})
        self.assertIn('error', response)
        self.assertIn('_meta', response['error']['data']['details'])


class TrustedPresentationTestCase(unittest.TestCase):
    """Trusted clients get the human form of the server-local paths:
    the monikers go, the '~' home masking stays."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-tpres-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = make_project_at(self.tmpdir)
        self.project.view = 'system'
        self.policy = McpPolicy(self.project)
        trust.ensure_passphrase(self.project)
        with open(os.path.join(self.tmpdir, trust.TRUST_PATH)) as fp:
            self.passphrase = fp.read()

    def test_the_trusted_form_of_the_paths(self):
        import unittest.mock
        from debops.constants import DEBOPS_PACKAGE_DATA
        from debops.mcp import runs
        with unittest.mock.patch.dict(
                os.environ,
                {'XDG_CACHE_HOME': os.path.join(self.tmpdir,
                                                'cache')}):
            server = McpServer(project=self.project, policy=self.policy,
                               transport='unix')
            # The moniker table is read at server construction
            log_line = os.path.join(runs.cache_root(), 'x',
                                    'execution-1.log')
        pkg = os.path.join(DEBOPS_PACKAGE_DATA, 'mcp', 'ansible.cfg')
        server.register_tool(
                'probe', 'the probe', {'type': 'object'},
                lambda arguments: {
                    'content': [{'type': 'text',
                                 'text': 'log: {} pkg: {} home: {}'
                                 .format(log_line, pkg,
                                         os.path.expanduser('~/x'))}],
                    'isError': False})

        def call(token=None):
            return server.handle(_request(
                    'tools/call', {'name': 'probe', 'arguments': {}},
                    token=token))

        # Untrusted: monikers plus the home masking
        text = call()['result']['content'][0]['text']
        self.assertIn('run-logs:/x/execution-1.log', text)
        self.assertIn('bundled:/mcp/ansible.cfg', text)
        self.assertIn('home: ~/x', text)

        # Trusted: the real roots, the home still masked
        token = server.handle(_request(
                'server/authenticate',
                {'passphrase': self.passphrase}))['result']['token']
        text = call(token)['result']['content'][0]['text']
        self.assertIn('log: {}'.format(log_line), text)
        self.assertNotIn('bundled:', text)
        self.assertIn('debops/_data/mcp/ansible.cfg', text)
        self.assertIn('home: ~/x', text)


class ActivityTopicTestCase(unittest.TestCase):
    """The activity topic: the audit lines to a trusted listener."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-activity-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = make_project_at(self.tmpdir)
        self.project.view = 'system'
        self.policy = McpPolicy(self.project)
        self.server = McpServer(project=self.project,
                                policy=self.policy)
        trust.ensure_passphrase(self.project)
        with open(os.path.join(self.tmpdir, trust.TRUST_PATH)) as fp:
            self.passphrase = fp.read()

    def test_activity_streams_to_a_trusted_listener(self):
        import threading
        token = self.server.handle(_request(
                'server/authenticate',
                {'passphrase': self.passphrase}))['result']['token']
        messages = []
        done = threading.Event()

        def notify(message):
            messages.append(message)

        meta = dict(REQUEST_META)
        meta['org.debops/token'] = token

        def listen():
            self.server.handle(
                    {'jsonrpc': '2.0', 'id': 42,
                     'method': 'subscriptions/listen',
                     'params': {'_meta': meta,
                                'notifications': {'activity': True}}},
                    notify=notify, dead=done)

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        try:
            # The acknowledgment comes first and honors the topic
            deadline = time.monotonic() + 5
            while not messages and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(messages)
            self.assertEqual(messages[0]['method'],
                             'notifications/subscriptions/acknowledged')
            self.assertEqual(messages[0]['params']['notifications'],
                             {'activity': True})

            # The feed rides the notice level; the default level of
            # the logger hides those from any handler
            debops_log = logging.getLogger('debops')
            old_level = debops_log.level
            debops_log.setLevel(logging.NOTICE)
            try:
                debops_log.notice('the test marker')
            finally:
                debops_log.setLevel(old_level)
            count = len(messages)
            deadline = time.monotonic() + 5
            while len(messages) <= count \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            activity = [message for message in messages
                        if message['method']
                        == 'notifications/activity']
            self.assertTrue(activity, messages)
            self.assertEqual(activity[0]['params']['line'],
                             'the test marker')
        finally:
            done.set()
            thread.join(timeout=5)
        # The feed detached with the listener
        self.assertIsNone(self.server._activity_handler)

    def test_an_untrusted_listener_is_refused(self):
        response = self.server.handle(_request(
                'subscriptions/listen',
                {'notifications': {'activity': True}}),
                notify=lambda message: None, dead=threading.Event())
        self.assertIn('error', response)
        self.assertIn('trusted', response['error']['data']['details'])


if __name__ == '__main__':
    unittest.main()
