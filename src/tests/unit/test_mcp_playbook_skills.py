# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the generated playbook skills ('skill://playbooks/').

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. A minimal playbook tree and a role tree in a temporary
directory stand in for the collection.
"""

from debops.mcp.playbook_skills import PlaybookSkillSource
from debops.mcp.role_skills import RoleSkillSource
from debops.mcp.skills import SkillLibrary
import os
import shutil
import tempfile
import unittest


SERVICE_PLAYBOOK = '''\
---
- name: Manage the web service
  hosts: debops_service_web
  become: true
  roles:
    - role: base
    - role: web
'''

SET_PLAYBOOK = '''\
---
- import_playbook: web-plain.yml
- import_playbook: web-tls.yml
'''

SCOPE_PLAYBOOK = '''\
---
- name: Manage the web service alone
  hosts: [debops_all_services, debops_service_web]
  tasks:
    - ansible.builtin.import_role:
        name: web
'''

NESTED_PLAYBOOK = '''\
---
- name: Tune the database
  hosts: [debops_all_services, debops_service_db]
  tasks:
    - ansible.builtin.import_role:
        name: db
'''

ROLE_META = '''\
---
galaxy_info:
  description: The web service role
'''


class PlaybookSkillsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-pbskills-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.playbooks = os.path.join(self.tmpdir, 'playbooks')
        self.roles = os.path.join(self.tmpdir, 'roles')
        os.makedirs(os.path.join(self.playbooks, 'service'))
        os.makedirs(os.path.join(self.playbooks, 'scope', 'db'))
        with open(os.path.join(self.playbooks, 'service', 'web.yml'),
                  'w') as fp:
            fp.write(SERVICE_PLAYBOOK)
        with open(os.path.join(self.playbooks, 'service', 'db.yml'),
                  'w') as fp:
            fp.write(SET_PLAYBOOK.replace('web-plain', 'db-plain')
                                 .replace('web-tls', 'db-tls'))
        with open(os.path.join(self.playbooks, 'service',
                               'db-plain.yml'), 'w') as fp:
            fp.write(SCOPE_PLAYBOOK.replace('web', 'db'))
        with open(os.path.join(self.playbooks, 'service',
                               'db-tls.yml'), 'w') as fp:
            fp.write(SCOPE_PLAYBOOK.replace('web', 'db'))
        with open(os.path.join(self.playbooks, 'scope', 'web.yml'),
                  'w') as fp:
            fp.write(SCOPE_PLAYBOOK)
        with open(os.path.join(self.playbooks, 'scope', 'db',
                               'tune.yml'), 'w') as fp:
            fp.write(NESTED_PLAYBOOK)
        with open(os.path.join(self.playbooks, 'scope', 'db',
                               'raw_sql.yml'), 'w') as fp:
            fp.write(NESTED_PLAYBOOK.replace('Tune the database',
                                             'Run raw SQL'))
        # Neither a section nor a role: never a generated brief
        with open(os.path.join(self.playbooks, 'site.yml'), 'w') as fp:
            fp.write(SCOPE_PLAYBOOK)
        for role, description in (('web', 'The web service role'),
                                  ('db', 'The database role')):
            meta_dir = os.path.join(self.roles, role, 'meta')
            os.makedirs(meta_dir)
            with open(os.path.join(meta_dir, 'main.yml'), 'w') as fp:
                fp.write(ROLE_META.replace('web service', 'database')
                         if role == 'db' else ROLE_META)
        self.role_source = RoleSkillSource([self.roles])
        self.source = PlaybookSkillSource(self.playbooks,
                                          self.role_source)

    def read(self, target):
        return self.source.read('playbooks/{}/SKILL.md'.format(target))

    def test_the_mapping(self):
        self.assertEqual(self.source._map('service/web'),
                         ('service', 'web', None, None))
        self.assertEqual(self.source._map('scope/web'),
                         ('scope', 'web', None, None))
        self.assertEqual(self.source._map('scope/db/tune'),
                         ('scope', 'db', 'tune', None))
        # The variant family strips at the first dash
        self.assertEqual(self.source._map('service/db-plain'),
                         ('service', 'db', None, 'service/db'))
        # Neither a section nor a role
        self.assertIsNone(self.source._map('site'))
        self.assertIsNone(self.source._map('layer/srv'))
        self.assertIsNone(self.source._map('service/nonexistent'))

    def test_the_service_brief(self):
        brief = self.read('service/web')
        self.assertIn('# service/web', brief)
        # The description comes from the playbook's own play name
        self.assertIn('description: Manage the web service', brief)
        self.assertIn('Runs on', brief)
        self.assertIn("'debops_service_web'", brief)
        # The stack names the roles the service playbook applies
        self.assertIn('base and web', brief)
        self.assertIn('skill://roles/web/', brief)

    def test_the_scope_brief_carries_the_prerequisite_chain(self):
        brief = self.read('scope/web')
        self.assertIn("runs only the 'web' role", brief.lower()
                      .replace('\n', ' '))
        self.assertIn("'common'", brief)
        self.assertIn("'service/web'", brief)
        self.assertIn('may fail', brief)

    def test_the_nested_action_brief(self):
        brief = self.read('scope/db/tune')
        self.assertIn('description: Tune the database', brief)
        self.assertIn("its 'tune' action", ' '.join(brief.split()))
        self.assertIn('skill://roles/db/', brief)

    def test_a_dashed_path_finds_the_underscored_playbook(self):
        # Skill path segments are dashed (the Agent Skills
        # convention); playbook names carry underscores. The fold must
        # find the file — a miss used to render the role's description
        # instead of the playbook's own
        brief = self.read('scope/db/raw-sql')
        self.assertIn('description: Run raw SQL', brief)
        self.assertNotIn('The database role', brief)

    def test_an_unknown_playbook_404s_instead_of_a_degraded_brief(self):
        self.assertIsNone(self.read('scope/db/nonexistent'))
        self.assertIsNone(self.read('service/nonexistent'))

    def test_the_variant_points_at_the_parent(self):
        brief = self.read('service/db-plain')
        self.assertIn('variant of the service/db playbook', brief)
        # A variant brief is described by its own play name
        self.assertIn('description: Manage the db service alone', brief)
        # The set parent has no play name of its own: the role's
        # description fills in, and the imports and variants are named
        parent = self.read('service/db')
        self.assertIn('description: The database role', parent)
        self.assertIn('Imports', parent)
        self.assertIn("'plain'", parent)
        self.assertIn("'tls'", parent)

    def test_docs_mode_points_at_the_tools_which_exist(self):
        docs = PlaybookSkillSource(self.playbooks, self.role_source,
                                   execution=False)
        brief = docs.read('playbooks/service/web/SKILL.md')
        self.assertNotIn("'check'", brief)
        self.assertNotIn("'run'", brief)
        self.assertIn('playbook.info', brief)
        self.assertIn('ansible.doc', brief)

    def test_a_nameless_play_gets_the_role_description(self):
        with open(os.path.join(self.playbooks, 'service',
                               'db-tools.yml'), 'w') as fp:
            fp.write('---\n- hosts: db\n  roles: [db]\n')
        source = PlaybookSkillSource(self.playbooks, self.role_source)
        brief = source.read('playbooks/service/db-tools/SKILL.md')
        self.assertIn('description: The database role', brief)

    def test_entries_carry_the_dynamic_resources(self):
        entries = self.source.entries()
        uris = [entry['uri'] for entry in entries]
        self.assertIn('skill://playbooks/service/web/SKILL.md', uris)
        self.assertIn('skill://playbooks/scope/db/tune/SKILL.md', uris)
        self.assertNotIn('skill://playbooks/site/SKILL.md', uris)
        for entry in entries:
            self.assertEqual(entry['resources'], 'dynamic')
            self.assertIn('name', entry['frontmatter'])
            self.assertIn('description', entry['frontmatter'])

    def test_the_filesystem_skill_shadows_the_generated_one(self):
        project = os.path.join(self.tmpdir, 'project-skills')
        skill_dir = os.path.join(project, 'playbooks', 'service', 'web')
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as fp:
            fp.write('---\nname: web\ndescription: The authored version'
                     '\n---\n\n# authored\n')
        library = SkillLibrary(project_path=project,
                               sources=[self.source])
        content = library.read(
                'skill://playbooks/service/web/SKILL.md')
        self.assertIn('# authored', content)

    def test_uris_for_a_role(self):
        self.assertEqual(self.source.uris_for('web'),
                         ['skill://playbooks/service/web/SKILL.md',
                          'skill://playbooks/scope/web/SKILL.md'])
        self.assertEqual(self.source.uris_for('nonexistent'), [])

    def test_the_role_brief_references_the_playbook_skills(self):
        self.role_source.playbooks = self.source
        brief = self.role_source.read('roles/web/SKILL.md')
        self.assertIn('skill://playbooks/service/web/', brief)
        self.assertIn('skill://playbooks/scope/web/', brief)

    def test_the_role_brief_without_playbooks_stays_lean(self):
        self.role_source.playbooks = self.source
        # A role no playbook maps to gets the plain brief
        os.makedirs(os.path.join(self.roles, 'standalone', 'meta'))
        brief = self.role_source.read('roles/standalone/SKILL.md')
        self.assertNotIn('playbook skill', brief)
        self.assertIn('playbook.list', brief)


if __name__ == '__main__':
    unittest.main()
