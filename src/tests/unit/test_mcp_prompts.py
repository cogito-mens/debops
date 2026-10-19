# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the project-local MCP prompts.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.prompts import PromptLibrary
from debops.mcp.server import McpServer, PROTOCOL_VERSION
import os
import shutil
import tempfile
import unittest


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}

DEPLOY_PROMPT = '''---
description: Deploy a service
arguments:
  - name: service
    description: The service playbook to run
    required: true
  - name: extra
    description: An optional extra
---

Deploy the {{ service }} service{% if extra %}, {{ extra }}{% endif %}.
'''

PLAIN_PROMPT = '''---
description: Housekeeping
---

Do the housekeeping.
'''

NO_DESCRIPTION = '''---
name: no-description
---

No description here.
'''


class PromptLibraryTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-prompts-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        with open(os.path.join(self.tmpdir, 'deploy.md'), 'w') as fp:
            fp.write(DEPLOY_PROMPT)
        with open(os.path.join(self.tmpdir, 'housekeeping.md'), 'w') as fp:
            fp.write(PLAIN_PROMPT)
        with open(os.path.join(self.tmpdir, 'no-description.md'),
                  'w') as fp:
            fp.write(NO_DESCRIPTION)
        os.makedirs(os.path.join(self.tmpdir, 'not-a-prompt.md.md'))
        self.library = PromptLibrary(self.tmpdir)

    def test_available(self):
        self.assertTrue(self.library.available())
        self.assertFalse(
                PromptLibrary(os.path.join(self.tmpdir, 'nosuch'))
                .available())

    def test_list(self):
        prompts = {prompt['name']: prompt
                   for prompt in self.library.list()}
        self.assertEqual(sorted(prompts), ['deploy', 'housekeeping'])
        deploy = prompts['deploy']
        self.assertEqual(deploy['description'], 'Deploy a service')
        arguments = {argument['name']: argument
                     for argument in deploy['arguments']}
        self.assertTrue(arguments['service']['required'])
        self.assertFalse(arguments['extra']['required'])
        # A prompt without arguments omits the key
        self.assertNotIn('arguments', prompts['housekeeping'])

    def test_render(self):
        rendered = self.library.render('deploy', {'service': 'nginx'})
        self.assertEqual(rendered['description'], 'Deploy a service')
        self.assertEqual(rendered['text'],
                         'Deploy the nginx service.\n')

    def test_render_with_an_optional_argument(self):
        rendered = self.library.render('deploy', {'service': 'nginx',
                                                  'extra': 'carefully'})
        self.assertEqual(rendered['text'],
                         'Deploy the nginx service, carefully.\n')

    def test_render_unknown_prompt(self):
        with self.assertRaises(ValueError):
            self.library.render('nosuch', {})

    def test_render_missing_required_argument(self):
        with self.assertRaises(ValueError) as context:
            self.library.render('deploy', {})
        self.assertIn('service', str(context.exception))


class PackagePromptsTestCase(unittest.TestCase):
    """Package prompts merge with the project's and are shadowed."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-pprompts-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.package = os.path.join(self.tmpdir, 'package')
        self.project = os.path.join(self.tmpdir, 'project')
        for root, name, description in (
                (self.package, 'shared', 'The packaged prompt'),
                (self.package, 'packaged', 'Only in the package'),
                (self.project, 'shared', 'The project override')):
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, name + '.md'), 'w') as fp:
                fp.write('---\ndescription: {}\n---\n\n{} body.\n'
                         .format(description, name))
        self.library = PromptLibrary(self.project,
                                     package_path=self.package)

    def test_list_merges_and_shadows(self):
        prompts = {prompt['name']: prompt
                   for prompt in self.library.list()}
        self.assertEqual(sorted(prompts), ['packaged', 'shared'])
        # The project prompt shadows the packaged one
        self.assertEqual(prompts['shared']['description'],
                         'The project override')

    def test_render_prefers_the_project(self):
        rendered = self.library.render('shared', {})
        self.assertEqual(rendered['text'], 'shared body.\n')
        self.assertEqual(rendered['description'], 'The project override')

    def test_available_with_only_package_content(self):
        library = PromptLibrary(os.path.join(self.tmpdir, 'nosuch'),
                                package_path=self.package)
        self.assertTrue(library.available())
        names = [prompt['name'] for prompt in library.list()]
        self.assertEqual(sorted(names), ['packaged', 'shared'])


class ServerPromptsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-sprompts-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        with open(os.path.join(self.tmpdir, 'deploy.md'), 'w') as fp:
            fp.write(DEPLOY_PROMPT)
        self.library = PromptLibrary(self.tmpdir)
        self.server = McpServer(prompts=self.library)

    def call(self, method, params):
        return self.server.handle({
                'jsonrpc': '2.0', 'id': 1,
                'method': method,
                'params': dict(params or {}, _meta=REQUEST_META)})

    def test_capability_advertised(self):
        result = self.call('server/discover', {})['result']
        self.assertIn('prompts', result['capabilities'])

    def test_capability_absent_without_content(self):
        empty = os.path.join(self.tmpdir, 'empty')
        os.makedirs(empty)
        server = McpServer(prompts=PromptLibrary(empty))
        result = server.handle({
                'jsonrpc': '2.0', 'id': 1,
                'method': 'server/discover',
                'params': {'_meta': REQUEST_META}})['result']
        self.assertNotIn('prompts', result['capabilities'])

    def test_no_library_no_methods(self):
        server = McpServer()
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1,
                'method': 'prompts/list',
                'params': {'_meta': REQUEST_META}})
        self.assertEqual(response['error']['code'], -32601)

    def test_list(self):
        result = self.call('prompts/list', {})['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertEqual(result['ttlMs'], 60000)
        self.assertEqual(result['cacheScope'], 'private')
        self.assertEqual([prompt['name'] for prompt in result['prompts']],
                         ['deploy'])

    def test_get(self):
        result = self.call('prompts/get', {
                'name': 'deploy',
                'arguments': {'service': 'nginx'}})['result']
        self.assertEqual(result['description'], 'Deploy a service')
        message = result['messages'][0]
        self.assertEqual(message['role'], 'user')
        self.assertEqual(message['content']['text'],
                         'Deploy the nginx service.\n')

    def test_get_missing_argument(self):
        response = self.call('prompts/get', {'name': 'deploy',
                                             'arguments': {}})
        self.assertEqual(response['error']['code'], -32602)
        self.assertIn('service', response['error']['data']['details'])


if __name__ == '__main__':
    unittest.main()
