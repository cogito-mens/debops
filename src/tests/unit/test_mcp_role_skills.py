# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the generated per-role skill briefs."""

import os
import shutil
import tempfile
import unittest

from debops.mcp.role_skills import RoleSkillSource
from debops.mcp.skills import SkillLibrary

NGINX_META = '''---
dependencies: []
galaxy_info:
  author: 'DebOps'
  description: 'Install and manage the nginx webserver'
  galaxy_tags: ['web', 'nginx']
'''

AUTHORED_SKILL = '''---
name: authored
description: The author's extensive version
---

# The real deal

Written by the role author.
'''


class RoleSkillSourceTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-rolebriefs-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.roles = os.path.join(self.tmpdir, 'roles')

        nginx = os.path.join(self.roles, 'nginx', 'meta')
        os.makedirs(nginx)
        with open(os.path.join(nginx, 'main.yml'), 'w') as fp:
            fp.write(NGINX_META)

        # A role whose name needs the namespace mapping
        os.makedirs(os.path.join(self.roles, 'apt_proxy', 'meta'))

        # No usable metadata at all
        os.makedirs(os.path.join(self.roles, 'bare'))

        # An authored skill at the role root, with a supporting file
        authored = os.path.join(self.roles, 'authored')
        os.makedirs(os.path.join(authored, 'skill'))
        with open(os.path.join(authored, 'SKILL.md'), 'w') as fp:
            fp.write(AUTHORED_SKILL)
        with open(os.path.join(authored, 'skill', 'notes.md'),
                  'w') as fp:
            fp.write('# Author notes\n')

        self.source = RoleSkillSource([self.roles])

    def entries(self):
        return {entry['uri']: entry for entry in self.source.entries()}

    def test_every_role_gets_a_brief(self):
        self.assertEqual(
                sorted(self.entries()),
                ['skill://roles/apt-proxy/SKILL.md',
                 'skill://roles/authored/SKILL.md',
                 'skill://roles/bare/SKILL.md',
                 'skill://roles/nginx/SKILL.md'])

    def test_underscores_map_to_dashes(self):
        # Skill names allow no underscores; role names allow no dashes,
        # so the mapping is unambiguous in both directions
        entry = self.entries()['skill://roles/apt-proxy/SKILL.md']
        self.assertEqual(entry['frontmatter']['name'], 'apt-proxy')

    def test_generated_frontmatter_from_the_role_metadata(self):
        entry = self.entries()['skill://roles/nginx/SKILL.md']
        self.assertEqual(entry['frontmatter']['description'],
                         'Install and manage the nginx webserver')
        self.assertEqual(entry['frontmatter']['metadata'],
                         {'tags': ['web', 'nginx']})
        self.assertEqual(entry['resources'], 'dynamic')

    def test_a_role_without_metadata_still_lists(self):
        entry = self.entries()['skill://roles/bare/SKILL.md']
        self.assertIn('no description',
                      entry['frontmatter']['description'])

    def test_read_renders_the_brief(self):
        content = self.source.read('roles/nginx/SKILL.md')
        self.assertIn('# debops.nginx', content)
        self.assertIn('Install and manage the nginx webserver', content)
        self.assertIn('role.defaults', content)

    def test_read_unknown_role(self):
        self.assertIsNone(self.source.read('roles/nosuch/SKILL.md'))

    def test_authored_skill_replaces_the_brief(self):
        entry = self.entries()['skill://roles/authored/SKILL.md']
        self.assertEqual(entry['frontmatter']['description'],
                         "The author's extensive version")
        # Static content publishes its digests, the supporting file
        # included
        uris = [resource['uri'] for resource in entry['resources']]
        self.assertIn('skill://roles/authored/skill/notes.md', uris)
        for resource in entry['resources']:
            self.assertTrue(resource['digest'].startswith('sha256:'))
        content = self.source.read('roles/authored/SKILL.md')
        self.assertIn('The real deal', content)
        self.assertEqual(self.source.read(
                'roles/authored/skill/notes.md'), '# Author notes\n')

    def test_authored_support_files_are_confined(self):
        self.assertIsNone(self.source.read(
                'roles/authored/skill/../../defaults/main.yml'))

    def test_project_tree_shadows_the_corpus(self):
        project = os.path.join(self.tmpdir, 'project-skills')
        skill_dir = os.path.join(project, 'roles', 'nginx')
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as fp:
            fp.write('---\nname: nginx\ndescription: The project '
                     'override\n---\n')
        library = SkillLibrary(self.roles + '-empty',
                               project_path=project,
                               sources=[self.source])
        entries = {entry['uri']: entry for entry in library.entries()}
        entry = entries['skill://roles/nginx/SKILL.md']
        self.assertEqual(entry['frontmatter']['description'],
                         'The project override')
        self.assertIn('The project override',
                      library.read('skill://roles/nginx/SKILL.md'))
        # The generated corpus answers the rest
        self.assertIn('skill://roles/bare/SKILL.md', entries)


if __name__ == '__main__':
    unittest.main()
