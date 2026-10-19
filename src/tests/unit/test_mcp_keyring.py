# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the keyring_fetch tool.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The fetch pipeline tests need gpg and are skipped without
it; no network is used: the URL fetch is mocked with fixture key content.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import ToolContext
from debops.mcp.tools import keyring
from debops.mcp.tools.keyring import (format_fingerprint,
                                      normalize_fingerprint,
                                      tool_keyring_fetch)
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


def _auto_commit_noop(self, message=None):
    pass


GOOD_FPR = 'B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8'


class FingerprintTestCase(unittest.TestCase):

    def test_normalize(self):
        self.assertEqual(normalize_fingerprint(GOOD_FPR), GOOD_FPR)
        self.assertEqual(normalize_fingerprint('0x' + GOOD_FPR.lower()),
                         GOOD_FPR)
        # The spaced display form is accepted
        self.assertEqual(normalize_fingerprint(format_fingerprint(
                GOOD_FPR)), GOOD_FPR)

    def test_normalize_rejects_short_ids(self):
        for bad in ('ACCC4CF8', 'not-a-key', '', None,
                    GOOD_FPR + 'FF'):
            with self.assertRaises(ValueError):
                normalize_fingerprint(bad)

    def test_format_groups_of_four(self):
        self.assertEqual(format_fingerprint(GOOD_FPR),
                         'B97B 0AFC AA1A 47F0 44F2 44A0 7FCC 7D46 '
                         'ACCC 4CF8')


class KeyringFetchTestCase(unittest.TestCase):

    def setUp(self):
        if shutil.which('gpg') is None:
            self.skipTest('gpg is not installed')
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-keyring-')
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
        self.policy.confirm = set()  # no confirmation in pipeline tests
        self.context = ToolContext(1, __import__('threading').Event())

        # A throwaway GPG key as the "remote" fixture
        self.gpg_home = os.path.join(self.tmpdir, 'gpghome')
        os.makedirs(self.gpg_home, 0o700)
        subprocess.run(
                ['gpg', '--batch', '--no-tty', '--homedir', self.gpg_home,
                 '--passphrase', '', '--quick-generate-key',
                 'DebOps MCP Test <test@example.org>', 'ed25519', 'sign',
                 '1d'],
                check=True, capture_output=True)
        listing = subprocess.run(
                ['gpg', '--batch', '--homedir', self.gpg_home,
                 '--with-colons', '--list-keys'],
                check=True, capture_output=True, text=True).stdout
        self.fingerprint = [line.split(':')[9]
                            for line in listing.splitlines()
                            if line.startswith('fpr:')][0]
        self.armored = subprocess.run(
                ['gpg', '--batch', '--homedir', self.gpg_home,
                 '--export', '--armor', self.fingerprint],
                check=True, capture_output=True, text=True).stdout

    def fetch(self, content=None, **arguments):
        with mock.patch.object(keyring, '_fetch_url',
                               return_value=(content if content is not None
                                             else self.armored)):
            return tool_keyring_fetch(self.project, self.policy,
                                      arguments, self.context)

    def test_fetch_and_store(self):
        result = self.fetch(url='https://keys.example.org/k.asc')
        self.assertFalse(result['isError'], result)
        text = result['content'][0]['text']
        self.assertIn(format_fingerprint(self.fingerprint), text)
        self.assertIn('DebOps MCP Test', text)
        structured = result['structuredContent']
        self.assertEqual(structured['fingerprint'], self.fingerprint)
        self.assertEqual(structured['file'],
                         'ansible/keyring/0x{}.asc'
                         .format(self.fingerprint))
        self.assertIn('DebOps MCP Test', structured['userIds'][0])
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        self.assertEqual(check_schema(structured,
                                      TOOL_OUTPUT_SCHEMAS
                                      ['keyring_fetch']), [])
        written = os.path.join(self.tmpdir, 'ansible', 'keyring',
                               '0x{}.asc'.format(self.fingerprint))
        with open(written) as fp:
            self.assertIn('BEGIN PGP PUBLIC KEY BLOCK', fp.read())
        self.assertEqual(self.commits,
                         ['DebOps MCP: keyring_fetch 0x{}.asc'
                          .format(self.fingerprint)])

    def test_fingerprint_match(self):
        result = self.fetch(url='https://keys.example.org/k.asc',
                            fingerprint=self.fingerprint.lower())
        self.assertFalse(result['isError'], result)

    def test_fingerprint_mismatch_refuses(self):
        result = self.fetch(url='https://keys.example.org/k.asc',
                            fingerprint=GOOD_FPR)
        self.assertTrue(result['isError'])
        self.assertIn('fingerprint mismatch',
                      result['content'][0]['text'])
        self.assertFalse(os.path.isdir(os.path.join(
                self.tmpdir, 'ansible', 'keyring')))
        self.assertEqual(self.commits, [])

    def test_not_a_key_refused(self):
        result = self.fetch(content='<html>oops</html>',
                            url='https://keys.example.org/k.asc')
        self.assertTrue(result['isError'])
        self.assertIn('no GPG key', result['content'][0]['text'])

    def test_multi_key_bundle_refused(self):
        both = subprocess.run(
                ['gpg', '--batch', '--homedir', self.gpg_home,
                 '--export', '--armor'], check=True, capture_output=True,
                text=True).stdout
        second_home = os.path.join(self.tmpdir, 'gpghome2')
        os.makedirs(second_home, 0o700)
        subprocess.run(
                ['gpg', '--batch', '--no-tty', '--homedir', second_home,
                 '--passphrase', '', '--quick-generate-key',
                 'DebOps MCP Test 2 <test2@example.org>', 'ed25519',
                 'sign', '1d'], check=True, capture_output=True)
        second = subprocess.run(
                ['gpg', '--batch', '--homedir', second_home,
                 '--export', '--armor'], check=True, capture_output=True,
                text=True).stdout
        result = self.fetch(content=both + second,
                            url='https://keys.example.org/k.asc')
        self.assertTrue(result['isError'])
        self.assertIn('keys; fetch one', result['content'][0]['text'])

    def test_http_refused(self):
        result = self.fetch(url='http://keys.example.org/k.asc')
        self.assertTrue(result['isError'])
        self.assertIn('https://', result['content'][0]['text'])

    def test_keyserver_needs_a_fingerprint(self):
        result = self.fetch(keyserver='keyserver.ubuntu.com')
        self.assertTrue(result['isError'])
        self.assertIn('fingerprint', result['content'][0]['text'])

    def test_source_allowlist(self):
        self.policy.keyring_sources = ['keyserver.ubuntu.com']
        result = self.fetch(url='https://keys.example.org/k.asc')
        self.assertTrue(result['isError'])
        self.assertIn('keyring_sources', result['content'][0]['text'])
        # The allowed host passes the check (the fetch is mocked)
        result = self.fetch(url='https://keyserver.ubuntu.org/'
                                'nope.asc')
        self.assertTrue(result['isError'])
        result = self.fetch(url='https://keyserver.ubuntu.com/k.asc')
        self.assertFalse(result['isError'], result)

    def test_non_git_project_refused_before_fetching(self):
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        with mock.patch.object(keyring, '_fetch_key') as fetch:
            result = tool_keyring_fetch(
                    self.project, self.policy,
                    {'url': 'https://keys.example.org/k.asc'},
                    self.context)
        self.assertTrue(result['isError'])
        self.assertIn('not a git repository',
                      result['content'][0]['text'])
        fetch.assert_not_called()


class KeyringConfirmationTestCase(unittest.TestCase):
    """The confirmation flow: the dialog carries the computed key."""

    def setUp(self):
        if shutil.which('gpg') is None:
            self.skipTest('gpg is not installed')
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-keyconf-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       check=True)
        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'commit': _auto_commit_noop,
            'config': type('Config', (), {'raw': {}})(),
        })()
        self.policy = McpPolicy(self.project)
        # The default confirm set includes keyring_fetch
        self.assertIn('keyring_fetch', self.policy.confirm)
        self.eliciting = ToolContext(
                1, __import__('threading').Event(),
                client_capabilities={'elicitation': {'form': {}}})
        self.plain = ToolContext(1, __import__('threading').Event())

        self.gpg_home = os.path.join(self.tmpdir, 'gpghome')
        os.makedirs(self.gpg_home, 0o700)
        subprocess.run(
                ['gpg', '--batch', '--no-tty', '--homedir', self.gpg_home,
                 '--passphrase', '', '--quick-generate-key',
                 'DebOps MCP Test <test@example.org>', 'ed25519', 'sign',
                 '1d'], check=True, capture_output=True)
        listing = subprocess.run(
                ['gpg', '--batch', '--homedir', self.gpg_home,
                 '--with-colons', '--list-keys'],
                check=True, capture_output=True, text=True).stdout
        self.fingerprint = [line.split(':')[9]
                            for line in listing.splitlines()
                            if line.startswith('fpr:')][0]
        self.armored = subprocess.run(
                ['gpg', '--batch', '--homedir', self.gpg_home,
                 '--export', '--armor', self.fingerprint],
                check=True, capture_output=True, text=True).stdout

    def call(self, context, input_responses=None):
        if input_responses is not None:
            context.input_responses = input_responses
        with mock.patch.object(keyring, '_fetch_url',
                               return_value=self.armored):
            return tool_keyring_fetch(
                    self.project, self.policy,
                    {'url': 'https://keys.example.org/k.asc'}, context)

    def test_first_call_asks_with_the_computed_fingerprint(self):
        result = self.call(self.eliciting)
        self.assertEqual(result['resultType'], 'input_required')
        request = result['inputRequests']['confirmation']
        message = request['params']['message']
        self.assertIn(format_fingerprint(self.fingerprint), message)
        self.assertIn('https://keys.example.org/k.asc', message)
        self.assertIn('DebOps MCP Test', message)
        # The fingerprint rides the form, so the retry can verify the
        # confirmed key is the fetched key
        schema = request['params']['requestedSchema']
        self.assertEqual(schema['properties']['fingerprint']['default'],
                         self.fingerprint)
        # Nothing written yet
        self.assertFalse(os.path.isdir(os.path.join(
                self.tmpdir, 'ansible', 'keyring')))

    def test_confirmed_retry_writes(self):
        result = self.call(self.eliciting, input_responses={
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': True,
                                             'fingerprint':
                                             self.fingerprint}}})
        self.assertFalse(result['isError'], result)
        self.assertTrue(os.path.isfile(os.path.join(
                self.tmpdir, 'ansible', 'keyring',
                '0x{}.asc'.format(self.fingerprint))))

    def test_declined_writes_nothing(self):
        result = self.call(self.eliciting, input_responses={
                'confirmation': {'action': 'decline', 'content': {}}})
        self.assertTrue(result['isError'])
        self.assertIn('declined', result['content'][0]['text'])
        self.assertFalse(os.path.isdir(os.path.join(
                self.tmpdir, 'ansible', 'keyring')))

    def test_echo_mismatch_refuses(self):
        result = self.call(self.eliciting, input_responses={
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': True,
                                             'fingerprint': GOOD_FPR}}})
        self.assertTrue(result['isError'])
        self.assertIn('does not match', result['content'][0]['text'])

    def test_client_without_elicitation_proceeds(self):
        result = self.call(self.plain)
        self.assertFalse(result['isError'], result)


if __name__ == '__main__':
    unittest.main()
