# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the skill library and its MCP resources.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.server import McpServer
from debops.mcp.skills import SkillLibrary
import os
import shutil
import tempfile
import unittest


GOOD_SKILL = '''---
name: good-skill
description: A valid skill
---

# Good skill

Do the good thing.
'''

NO_FRONTMATTER = '# Just markdown\n'

WRONG_NAME = '''---
name: other-name
description: The name does not match the directory
---
'''


class SkillLibraryTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-skills-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        os.makedirs(os.path.join(self.tmpdir, 'good-skill',
                                 'references'))
        with open(os.path.join(self.tmpdir, 'good-skill', 'SKILL.md'),
                  'w') as fp:
            fp.write(GOOD_SKILL)
        with open(os.path.join(self.tmpdir, 'good-skill', 'references',
                               'notes.md'), 'w') as fp:
            fp.write('# Notes\n')
        os.makedirs(os.path.join(self.tmpdir, 'no-frontmatter'))
        with open(os.path.join(self.tmpdir, 'no-frontmatter', 'SKILL.md'),
                  'w') as fp:
            fp.write(NO_FRONTMATTER)
        os.makedirs(os.path.join(self.tmpdir, 'wrong-name'))
        with open(os.path.join(self.tmpdir, 'wrong-name', 'SKILL.md'),
                  'w') as fp:
            fp.write(WRONG_NAME)
        os.makedirs(os.path.join(self.tmpdir, 'empty-dir'))

        self.library = SkillLibrary(self.tmpdir)

    def test_list(self):
        skills = self.library.list()
        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill['uri'], 'skill://good-skill')
        self.assertEqual(skill['name'], 'good-skill')
        self.assertEqual(skill['description'], 'A valid skill')
        self.assertEqual(skill['mimeType'], 'text/markdown')

    def test_list_missing_directory(self):
        library = SkillLibrary(os.path.join(self.tmpdir, 'nosuch'))
        self.assertEqual(library.list(), [])

    def test_read_skill(self):
        content = self.library.read('skill://good-skill')
        self.assertEqual(content, GOOD_SKILL)

    def test_read_supporting_file(self):
        content = self.library.read(
                'skill://good-skill/references/notes.md')
        self.assertEqual(content, '# Notes\n')

    def test_read_rejects_traversal(self):
        with self.assertRaises(ValueError):
            self.library.read('skill://good-skill/../../etc/passwd')

    def test_read_rejects_invalid_names(self):
        with self.assertRaises(ValueError):
            self.library.read('skill://Not_A_Skill')

    def test_read_rejects_other_schemes(self):
        with self.assertRaises(ValueError):
            self.library.read('file:///etc/passwd')

    def test_read_unknown_skill(self):
        self.assertIsNone(self.library.read('skill://nosuch'))


class ServerResourcesTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-skills-srv-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.makedirs(os.path.join(self.tmpdir, 'good-skill'))
        with open(os.path.join(self.tmpdir, 'good-skill', 'SKILL.md'),
                  'w') as fp:
            fp.write(GOOD_SKILL)
        self.library = SkillLibrary(self.tmpdir)

    def test_discover_advertises_resources_with_a_library(self):
        server = McpServer(skills=self.library)
        response = server.handle({'jsonrpc': '2.0', 'id': 1,
                                  'method': 'server/discover'})
        self.assertIn('resources', response['result']['capabilities'])

    def test_discover_without_a_library(self):
        server = McpServer()
        response = server.handle({'jsonrpc': '2.0', 'id': 1,
                                  'method': 'server/discover'})
        self.assertNotIn('resources', response['result']['capabilities'])

    def test_resources_list_and_read(self):
        server = McpServer(skills=self.library)
        response = server.handle({'jsonrpc': '2.0', 'id': 1,
                                  'method': 'resources/list',
                                  'params': {}})
        resources = response['result']['resources']
        self.assertEqual([resource['uri'] for resource in resources],
                         ['skill://good-skill'])

        response = server.handle({'jsonrpc': '2.0', 'id': 2,
                                  'method': 'resources/read',
                                  'params': {'uri': 'skill://good-skill'}})
        contents = response['result']['contents']
        self.assertEqual(contents[0]['text'], GOOD_SKILL)
        self.assertEqual(contents[0]['mimeType'], 'text/markdown')

    def test_resources_read_unknown_uri(self):
        server = McpServer(skills=self.library)
        response = server.handle({'jsonrpc': '2.0', 'id': 3,
                                  'method': 'resources/read',
                                  'params': {'uri': 'skill://nosuch'}})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('not found', response['error']['data']['details'])
        # The error echoes the request id
        self.assertEqual(response['id'], 3)

    def test_resources_read_traversal_echoes_request_id(self):
        server = McpServer(skills=self.library)
        response = server.handle({'jsonrpc': '2.0', 'id': 7,
                                  'method': 'resources/read',
                                  'params': {'uri': 'skill://good-skill/'
                                             '../../etc/passwd'}})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertEqual(response['id'], 7)

    def test_resources_read_rejects_other_schemes(self):
        server = McpServer(skills=self.library)
        response = server.handle({'jsonrpc': '2.0', 'id': 4,
                                  'method': 'resources/read',
                                  'params': {'uri': 'file:///etc/passwd'}})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('unsupported URI scheme',
                      response['error']['data']['details'])


if __name__ == '__main__':
    unittest.main()
