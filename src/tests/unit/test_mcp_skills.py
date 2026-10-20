# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the skill library and the SEP-2640 skills extension.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.server import McpServer, PROTOCOL_VERSION
from debops.mcp.server import LIST_PAGE_SIZE
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

NESTED_SKILL = '''---
name: refunds
description: Process customer refund requests per company policy
license: Apache-2.0
---

# Refunds
'''


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}


class FakeSource(object):
    """A generated skill source: one 'dynamic' skill, one prefix."""

    prefixes = {'generated'}

    def entries(self):
        return [{
            'uri': 'skill://generated/thing/SKILL.md',
            'frontmatter': {'name': 'thing', 'description': 'Made'},
            'resources': 'dynamic',
        }]

    def read(self, rest):
        if rest == 'generated/thing/SKILL.md':
            return '---\nname: thing\ndescription: Made\n---\n\nMade.\n'
        return None


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

    def test_list_yields_every_file(self):
        resources = self.library.list()
        self.assertEqual(
                [resource['uri'] for resource in resources],
                ['skill://good-skill/SKILL.md',
                 'skill://good-skill/references/notes.md'])
        self.assertEqual(resources[0]['mimeType'], 'text/markdown')

    def test_list_missing_directory(self):
        library = SkillLibrary(os.path.join(self.tmpdir, 'nosuch'))
        self.assertEqual(library.list(), [])

    def test_entries_carry_the_verbatim_frontmatter_and_digests(self):
        (entry,) = self.library.entries()
        self.assertEqual(entry['uri'], 'skill://good-skill/SKILL.md')
        self.assertEqual(entry['frontmatter']['name'], 'good-skill')
        self.assertEqual(entry['frontmatter']['description'],
                         'A valid skill')
        self.assertEqual(
                sorted(resource['uri'] for resource in
                       entry['resources']),
                ['skill://good-skill/SKILL.md',
                 'skill://good-skill/references/notes.md'])
        import hashlib
        expected = {'skill://good-skill/SKILL.md': GOOD_SKILL.encode(),
                    'skill://good-skill/references/notes.md':
                    b'# Notes\n'}
        for resource in entry['resources']:
            content = expected[resource['uri']]
            self.assertEqual(
                    resource['digest'],
                    'sha256:' + hashlib.sha256(content).hexdigest())
            self.assertEqual(resource['size'], len(content))

    def test_read_skill(self):
        content = self.library.read('skill://good-skill/SKILL.md')
        self.assertEqual(content, GOOD_SKILL)

    def test_read_supporting_file(self):
        content = self.library.read(
                'skill://good-skill/references/notes.md')
        self.assertEqual(content, '# Notes\n')

    def test_read_rejects_a_directory_uri(self):
        with self.assertRaises(ValueError):
            self.library.read('skill://good-skill/')

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
        self.assertIsNone(self.library.read('skill://nosuch/SKILL.md'))


class NestedSkillsTestCase(unittest.TestCase):
    """Skills nest to arbitrary depth; the last segment is the name."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-nested-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        skill_dir = os.path.join(self.tmpdir, 'acme', 'billing',
                                 'refunds')
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as fp:
            fp.write(NESTED_SKILL)
        os.makedirs(os.path.join(skill_dir, 'examples'))
        with open(os.path.join(skill_dir, 'examples', 'email.md'),
                  'w') as fp:
            fp.write('# Example\n')
        self.library = SkillLibrary(self.tmpdir)

    def test_the_nested_skill_is_found(self):
        (entry,) = self.library.entries()
        self.assertEqual(entry['uri'],
                         'skill://acme/billing/refunds/SKILL.md')
        # The last segment is the name; the prefix is organizational
        self.assertEqual(entry['frontmatter']['name'], 'refunds')
        # Non-required frontmatter passes through verbatim
        self.assertEqual(entry['frontmatter']['license'], 'Apache-2.0')

    def test_the_nested_files_read(self):
        self.assertEqual(self.library.read(
                'skill://acme/billing/refunds/SKILL.md'), NESTED_SKILL)
        self.assertEqual(self.library.read(
                'skill://acme/billing/refunds/examples/email.md'),
                '# Example\n')

    def test_directory_listing_is_not_recursive(self):
        root = self.library.directory('skill://')
        self.assertEqual(root, [{'uri': 'skill://acme',
                                 'name': 'acme',
                                 'mimeType': 'inode/directory'}])
        billing = self.library.directory('skill://acme/billing')
        self.assertEqual(billing, [{'uri': 'skill://acme/billing/refunds',
                                    'name': 'refunds',
                                    'mimeType': 'inode/directory'}])
        skill = self.library.directory('skill://acme/billing/refunds')
        self.assertEqual(
                [child['name'] for child in skill],
                ['SKILL.md', 'examples'])
        self.assertEqual(skill[1]['mimeType'], 'inode/directory')

    def test_a_file_uri_is_not_a_directory(self):
        self.assertIsNone(self.library.directory(
                'skill://acme/billing/refunds/SKILL.md'))

    def test_a_missing_uri_is_not_a_directory(self):
        self.assertIsNone(self.library.directory('skill://nosuch'))


class GeneratedSourceTestCase(unittest.TestCase):
    """Generated sources merge with the filesystem, shadowed by it."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-gen-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.package = os.path.join(self.tmpdir, 'package')
        self.project = os.path.join(self.tmpdir, 'project')
        os.makedirs(self.package)
        os.makedirs(self.project)

    def test_generated_entries_list_and_read(self):
        library = SkillLibrary(self.package, sources=[FakeSource()])
        uris = [entry['uri'] for entry in library.entries()]
        self.assertIn('skill://generated/thing/SKILL.md', uris)
        # 'dynamic' publishes no digests
        (entry,) = [entry for entry in library.entries()
                    if 'generated' in entry['uri']]
        self.assertEqual(entry['resources'], 'dynamic')
        content = library.read('skill://generated/thing/SKILL.md')
        self.assertIn('Made.', content)
        # The generated prefix joins the directory tree
        root = library.directory('skill://')
        self.assertIn({'uri': 'skill://generated', 'name': 'generated',
                       'mimeType': 'inode/directory'}, root)

    def test_a_project_skill_shadows_the_generated_one(self):
        skill_dir = os.path.join(self.project, 'generated', 'thing')
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as fp:
            fp.write('---\nname: thing\ndescription: Authored\n---\n')
        library = SkillLibrary(self.package, project_path=self.project,
                               sources=[FakeSource()])
        (entry,) = [entry for entry in library.entries()
                    if 'generated' in entry['uri']]
        self.assertEqual(entry['frontmatter']['description'],
                         'Authored')
        # The authored file answers with digests
        self.assertNotEqual(entry['resources'], 'dynamic')

    def test_a_package_skill_at_a_reserved_prefix_is_refused(self):
        skill_dir = os.path.join(self.package, 'generated', 'thing')
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as fp:
            fp.write('---\nname: thing\ndescription: Squatter\n---\n')
        with self.assertLogs('debops.mcp.skills', level='WARNING'):
            library = SkillLibrary(self.package, sources=[FakeSource()])
            entries = library.entries()
        (entry,) = [entry for entry in entries
                    if 'generated' in entry['uri']]
        self.assertEqual(entry['frontmatter']['description'], 'Made')


class ServerExtensionTestCase(unittest.TestCase):
    """The skills extension methods of the server (SEP-2640)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-skills-srv-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.makedirs(os.path.join(self.tmpdir, 'good-skill'))
        with open(os.path.join(self.tmpdir, 'good-skill', 'SKILL.md'),
                  'w') as fp:
            fp.write(GOOD_SKILL)
        self.library = SkillLibrary(self.tmpdir)
        self.server = McpServer(skills=self.library)

    def call(self, method, params=None, request_id=1):
        return self.server.handle({
                'jsonrpc': '2.0', 'id': request_id, 'method': method,
                'params': dict(params or {}, _meta=REQUEST_META)})

    def test_discover_declares_the_extension(self):
        response = self.call('server/discover')
        extensions = response['result']['capabilities']['extensions']
        self.assertEqual(
                extensions['io.modelcontextprotocol/skills'],
                {'directoryRead': True})
        self.assertIn('resources',
                      response['result']['capabilities'])

    def test_no_extension_without_a_library(self):
        server = McpServer()
        response = server.handle({'jsonrpc': '2.0', 'id': 1,
                                  'method': 'server/discover',
                                  'params': {'_meta': REQUEST_META}})
        extensions = response['result']['capabilities']['extensions']
        self.assertNotIn('io.modelcontextprotocol/skills', extensions)

    def test_skills_list(self):
        response = self.call('skills/list')
        (entry,) = response['result']['skills']
        self.assertEqual(entry['uri'], 'skill://good-skill/SKILL.md')
        self.assertEqual(entry['frontmatter']['name'], 'good-skill')
        self.assertNotIn('nextCursor', response['result'])

    def test_skills_get(self):
        response = self.call('skills/get',
                             {'uri': 'skill://good-skill/SKILL.md'})
        self.assertEqual(response['result']['skill']['frontmatter']
                         ['description'], 'A valid skill')

    def test_skills_get_unknown(self):
        response = self.call('skills/get',
                             {'uri': 'skill://nosuch/SKILL.md'})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('unknown skill',
                      response['error']['data']['details'])

    def test_skills_get_rejects_a_non_skill_uri(self):
        response = self.call('skills/get',
                             {'uri': 'skill://good-skill'})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)

    def test_directory_read(self):
        response = self.call('resources/directory/read',
                             {'uri': 'skill://good-skill'})
        self.assertEqual(response['result']['resources'],
                         [{'uri': 'skill://good-skill/SKILL.md',
                           'name': 'SKILL.md',
                           'mimeType': 'text/markdown'}])

    def test_directory_read_of_a_file_is_invalid_params(self):
        response = self.call('resources/directory/read',
                             {'uri': 'skill://good-skill/SKILL.md'})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)

    def test_resources_list_and_read(self):
        response = self.call('resources/list')
        self.assertEqual(
                [resource['uri']
                 for resource in response['result']['resources']],
                ['skill://good-skill/SKILL.md'])

        response = self.call('resources/read',
                             {'uri': 'skill://good-skill/SKILL.md'})
        contents = response['result']['contents']
        self.assertEqual(contents[0]['text'], GOOD_SKILL)
        self.assertEqual(contents[0]['mimeType'], 'text/markdown')

    def test_resources_list_rejects_a_cursor(self):
        response = self.call('resources/list', {'cursor': 'e30='})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)

    def test_resources_read_unknown_uri(self):
        response = self.call('resources/read',
                             {'uri': 'skill://nosuch/SKILL.md'},
                             request_id=3)
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('not found', response['error']['data']['details'])
        # The error echoes the request id
        self.assertEqual(response['id'], 3)

    def test_resources_read_traversal_echoes_request_id(self):
        response = self.call(
                'resources/read',
                {'uri': 'skill://good-skill/../../etc/passwd'},
                request_id=7)
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertEqual(response['id'], 7)

    def test_resources_read_rejects_other_schemes(self):
        response = self.call('resources/read',
                             {'uri': 'file:///etc/passwd'},
                             request_id=4)
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('unsupported URI scheme',
                      response['error']['data']['details'])


class PaginationTestCase(unittest.TestCase):
    """The catalogs paginate when they outgrow the page size."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-pages-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        for index in range(LIST_PAGE_SIZE + 5):
            name = 'skill-{:03d}'.format(index)
            os.makedirs(os.path.join(self.tmpdir, name))
            with open(os.path.join(self.tmpdir, name, 'SKILL.md'),
                      'w') as fp:
                fp.write('---\nname: {}\ndescription: N {}\n---\n'
                         .format(name, index))
        self.server = McpServer(skills=SkillLibrary(self.tmpdir))

    def call(self, params=None):
        return self.server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'skills/list',
                'params': dict(params or {}, _meta=REQUEST_META)})

    def test_first_page_and_cursor(self):
        response = self.call()
        result = response['result']
        self.assertEqual(len(result['skills']), LIST_PAGE_SIZE)
        cursor = result['nextCursor']
        response = self.call({'cursor': cursor})
        rest = response['result']['skills']
        self.assertEqual(len(rest), 5)
        self.assertNotIn('nextCursor', response['result'])
        # No overlap between the pages
        self.assertFalse(
                {entry['uri'] for entry in rest}
                & {entry['uri'] for entry in result['skills']})

    def test_a_cursor_we_never_issued_is_invalid(self):
        response = self.call({'cursor': 'e30='})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('invalid cursor',
                      response['error']['data']['details'])

    def test_an_exhausted_cursor_is_invalid(self):
        import base64
        import json
        cursor = base64.urlsafe_b64encode(
                json.dumps({'offset': 10 ** 6}).encode()).decode()
        response = self.call({'cursor': cursor})
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)


if __name__ == '__main__':
    unittest.main()
