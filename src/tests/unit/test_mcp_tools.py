# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The tools are exercised through a real server with a small
fixture Ansible Collection.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer
from debops.mcp.tools import register_project_tools
import os
import shutil
import tempfile
import unittest


ROLE_DEFAULTS = '''---
#                                                              # [[[

# .. envvar:: testrole__enabled [[[
#
# Enable or disable the test role.
#
testrole__enabled: True
#                                                              # ]]]

# .. envvar:: testrole__port
#
# The port to listen on.
testrole__port: 8080

# An undocumented variable has no envvar block.
testrole__extra: "undocumented"

# A long default value, to check truncation in brief output
testrole__long_list:
'''
# Completed programmatically to keep the fixture readable
ROLE_DEFAULTS += ''.join(
        '  - "item-{:02d}"\n'.format(index) for index in range(30))

ROLE_META = '''---
galaxy_info:
  description: A test role
  galaxy_tags: [testing]
dependencies:
  - debops.otherrole
'''

# A role with split defaults in a 'defaults/main/' directory; files are
# read in alphabetical order and later files win per variable
SPLIT_DEFAULTS_A = '''---
# .. envvar:: splitrole__alpha [[[
#
# Variable from the first file.
splitrole__alpha: "a"

# .. envvar:: splitrole__shared [[[
#
# Documented in the first file.
splitrole__shared: "from-a"
'''

SPLIT_DEFAULTS_B = '''---
# .. envvar:: splitrole__beta [[[
#
# Variable from the second file.
splitrole__beta: "b"

splitrole__shared: "from-b"
'''

SPEC_ROLE_ARGUMENT_SPECS = '''---
argument_specs:
  main:
    short_description: A role with argument specs
    options:
      specrole__state:
        description:
          - Desired state of the thing.
        type: str
        choices: ['present', 'absent']
        default: present
      specrole__password:
        description: Secret password.
        type: str
        required: true
'''

SPEC_ROLE_DEFAULTS = '''---
specrole__state: "present"
specrole__extra_default: "not in the specs"
'''

PLAIN_ROLE_META = '''---
galaxy_info:
  description: A plainly documented role
  galaxy_tags: [fixture]
'''

PLAIN_ROLE_DEFAULTS = '''---
# Configure the listen port.
plainrole__port: 9000

plainrole__undocumented: true
'''

SITE_PLAYBOOK = '''---
- name: Manage everything
  import_playbook: service/testrole.yml
'''

SERVICE_PLAYBOOK = '''---
- name: Manage the test role
  collections: ['debops.debops']
  hosts: ['debops_service_testrole']
  become: True

  roles:
    - role: keyring
    - role: testrole
'''

CUSTOM_PLAYBOOK = '''---
- name: Custom project playbook
  hosts: custom_group
  tasks:
    - name: Include a role dynamically
      ansible.builtin.include_role:
        name: testrole
'''

COLLECTION_PLAYBOOK = '''---
- name: Install the thing
  hosts: localhost
  tasks:
    - name: Include the role
      ansible.builtin.include_role:
        name: testns.testcoll.plainrole
'''


class ToolsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-tools-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        # A minimal project and a fixture collection with one role
        project_dir = os.path.join(self.tmpdir, 'project')
        os.makedirs(os.path.join(project_dir, 'ansible', 'inventory'))
        self.project = type('Project', (), {
            'path': project_dir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'config': type('Config', (), {'raw': {}})(),
        })()

        self.roles_path = os.path.join(self.tmpdir, 'roles')
        role = os.path.join(self.roles_path, 'testrole')
        os.makedirs(os.path.join(role, 'defaults'))
        os.makedirs(os.path.join(role, 'meta'))
        os.makedirs(os.path.join(role, 'tasks'))
        os.makedirs(os.path.join(role, 'templates'))
        with open(os.path.join(role, 'defaults', 'main.yml'), 'w') as fp:
            fp.write(ROLE_DEFAULTS)
        with open(os.path.join(role, 'meta', 'main.yml'), 'w') as fp:
            fp.write(ROLE_META)
        with open(os.path.join(role, 'tasks', 'main.yml'), 'w') as fp:
            fp.write('---\n[]\n')

        splitrole = os.path.join(self.roles_path, 'splitrole')
        os.makedirs(os.path.join(splitrole, 'defaults', 'main'))
        with open(os.path.join(splitrole, 'defaults', 'main', 'a.yml'),
                  'w') as fp:
            fp.write(SPLIT_DEFAULTS_A)
        with open(os.path.join(splitrole, 'defaults', 'main', 'b.yml'),
                  'w') as fp:
            fp.write(SPLIT_DEFAULTS_B)

        # A fixture Ansible Collection with roles in various doc styles
        collection = os.path.join(self.tmpdir, 'ansible_collections',
                                  'testns', 'testcoll')
        specrole = os.path.join(collection, 'roles', 'specrole')
        plainrole = os.path.join(collection, 'roles', 'plainrole')
        emptyrole = os.path.join(collection, 'roles', 'emptyrole')
        os.makedirs(os.path.join(specrole, 'meta'))
        os.makedirs(os.path.join(specrole, 'defaults'))
        os.makedirs(os.path.join(plainrole, 'meta'))
        os.makedirs(os.path.join(plainrole, 'defaults'))
        os.makedirs(emptyrole)
        with open(os.path.join(specrole, 'meta', 'argument_specs.yml'),
                  'w') as fp:
            fp.write(SPEC_ROLE_ARGUMENT_SPECS)
        with open(os.path.join(specrole, 'defaults', 'main.yml'), 'w') as fp:
            fp.write(SPEC_ROLE_DEFAULTS)
        with open(os.path.join(plainrole, 'meta', 'main.yml'), 'w') as fp:
            fp.write(PLAIN_ROLE_META)
        with open(os.path.join(plainrole, 'defaults', 'main.yml'), 'w') as fp:
            fp.write(PLAIN_ROLE_DEFAULTS)

        # Playbooks: one project playbook, a fixture bundled collection
        os.makedirs(os.path.join(project_dir, 'ansible', 'playbooks'))
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'custom.yml'), 'w') as fp:
            fp.write(CUSTOM_PLAYBOOK)

        self.playbooks_path = os.path.join(self.tmpdir, 'debops-playbooks')
        os.makedirs(os.path.join(self.playbooks_path, 'service'))
        os.makedirs(os.path.join(self.playbooks_path, 'templates'))
        with open(os.path.join(self.playbooks_path, 'site.yml'), 'w') as fp:
            fp.write(SITE_PLAYBOOK)
        with open(os.path.join(self.playbooks_path, 'service',
                               'testrole.yml'), 'w') as fp:
            fp.write(SERVICE_PLAYBOOK)
        # Support directory contents are not runnable playbooks
        with open(os.path.join(self.playbooks_path, 'templates',
                               'vars.yml'), 'w') as fp:
            fp.write('---\n{}\n')

        os.makedirs(os.path.join(collection, 'playbooks'))
        with open(os.path.join(collection, 'playbooks', 'install.yml'),
                  'w') as fp:
            fp.write(COLLECTION_PLAYBOOK)

        self.collection_index = {
            'testns.testcoll': {'path': collection, 'version': '1.0.0'},
        }

        self.server = McpServer(project=self.project,
                                policy=McpPolicy(self.project))
        register_project_tools(
                self.server, roles_path=self.roles_path,
                playbooks_path=self.playbooks_path,
                collection_index_provider=lambda: self.collection_index)

    def call_tool(self, name, arguments=None, request_id=1):
        return self.server.handle({
            'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments or {}},
        })

    def result_text(self, response):
        content = response['result']['content']
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]['type'], 'text')
        return content[0]['text']

    # -- registration --------------------------------------------------

    def test_tools_are_listed(self):
        response = self.server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                'params': {}})
        names = [tool['name'] for tool in response['result']['tools']]
        self.assertEqual(sorted(names),
                         ['check', 'collection_playbook_info',
                          'collection_playbooks', 'collection_role_info',
                          'collection_role_variables', 'collection_roles',
                          'config_get', 'inventory_query', 'playbook_info',
                          'playbooks', 'project_files', 'project_pull',
                          'project_push', 'project_read', 'project_status',
                          'role_info', 'role_variables'])

    def test_denied_tool_is_not_registered(self):
        self.project.config.raw['project'] = {'mcp': {'deny': ['role_info']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.roles_path)
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                'params': {}})
        names = [tool['name'] for tool in response['result']['tools']]
        self.assertNotIn('role_info', names)

    # -- project_status --------------------------------------------------

    def test_project_status(self):
        response = self.call_tool('project_status')
        text = self.result_text(response)
        self.assertFalse(response['result']['isError'])
        self.assertIn('Project: project', text)
        self.assertIn('Type: legacy', text)
        self.assertIn('View: system', text)
        self.assertIn('Inventory: present', text)
        self.assertIn('Encryption: none', text)

    # -- role_info -------------------------------------------------------

    def test_role_info(self):
        response = self.call_tool('role_info', {'name': 'testrole'})
        text = self.result_text(response)
        self.assertIn('# debops.testrole', text)
        self.assertIn('A test role', text)
        self.assertIn('variables: 4', text)
        self.assertIn('dependencies: debops.otherrole', text)

    def test_role_info_accepts_qualified_name(self):
        response = self.call_tool('role_info',
                                  {'name': 'debops.testrole'})
        self.assertIn('# debops.testrole', self.result_text(response))

    def test_role_info_unknown_role(self):
        response = self.call_tool('role_info', {'name': 'nosuch'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('nosuch', self.result_text(response))

    def test_role_info_rejects_path_traversal(self):
        response = self.call_tool('role_info', {'name': '../../etc'})
        self.assertTrue(response['result']['isError'])

    # -- role_variables --------------------------------------------------

    def test_role_variables_full(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole', 'detail': 'full'})
        text = self.result_text(response)
        # Documented variables carry their description and default value
        self.assertIn('## testrole__enabled', text)
        self.assertIn('Enable or disable the test role.', text)
        self.assertIn('true', text)
        # The default renders as a bare scalar, without the PyYAML
        # end-of-document marker
        self.assertIn('```yaml\n8080\n```', text)
        self.assertNotIn('\n...\n', text)
        # Undocumented variables are listed too
        self.assertIn('## testrole__extra', text)

    def test_role_variables_names_detail(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole', 'detail': 'names'})
        text = self.result_text(response)
        self.assertIn('(4)', text)
        self.assertIn('testrole__enabled', text)
        self.assertIn('testrole__long_list', text)
        self.assertNotIn('##', text)
        self.assertNotIn('```', text)

    def test_role_variables_brief_is_the_default(self):
        response = self.call_tool('role_variables', {'name': 'testrole'})
        text = self.result_text(response)
        # One compact line per variable, no full documentation
        self.assertIn('- **testrole__enabled** — Enable or disable the '
                      'test role. (default: `true`)', text)
        self.assertNotIn('```yaml', text)
        self.assertNotIn('## testrole__enabled', text)

    def test_role_variables_brief_truncates_long_defaults(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole',
                                   'filter': 'long_list'})
        text = self.result_text(response)
        (line,) = [line for line in text.splitlines()
                   if line.startswith('- **')]
        self.assertLess(len(line), 140)
        self.assertTrue(line.endswith('…`)'))

    def test_role_variables_invalid_detail(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole', 'detail': 'everything'})
        self.assertTrue(response['result']['isError'])

    def test_role_variables_filter(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole', 'filter': 'port',
                                   'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('testrole__port', text)
        self.assertNotIn('testrole__enabled', text)

    def test_role_variables_no_match(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole', 'filter': 'zzz'})
        self.assertIn('no matching variables', self.result_text(response))

    def test_role_variables_unknown_role(self):
        response = self.call_tool('role_variables', {'name': 'nosuch'})
        self.assertTrue(response['result']['isError'])

    # -- split defaults files ----------------------------------------------

    def test_role_variables_split_defaults(self):
        response = self.call_tool('role_variables',
                                  {'name': 'splitrole', 'detail': 'full'})
        text = self.result_text(response)
        self.assertFalse(response['result']['isError'])
        # Both files contribute their variables, with file separators
        self.assertIn('(2 files)', text)
        self.assertIn('**File: a.yml**', text)
        self.assertIn('**File: b.yml**', text)
        self.assertIn('## splitrole__alpha', text)
        self.assertIn('## splitrole__beta', text)
        # A shared variable is documented once, but the later file wins
        self.assertEqual(text.count('## splitrole__shared'), 1)
        self.assertIn('Documented in the first file.', text)
        self.assertIn('from-b', text)

    def test_role_info_counts_split_defaults(self):
        response = self.call_tool('role_info', {'name': 'splitrole'})
        text = self.result_text(response)
        self.assertIn('variables: 3', text)

    # -- collection_roles --------------------------------------------------

    def test_collection_roles(self):
        response = self.call_tool('collection_roles')
        text = self.result_text(response)
        self.assertIn('## testns.testcoll (1.0.0)', text)
        self.assertIn('- emptyrole', text)
        self.assertIn('- plainrole', text)
        self.assertIn('- specrole', text)

    def test_collection_roles_filter(self):
        response = self.call_tool('collection_roles', {'filter': 'zzz'})
        self.assertIn('no collections with roles',
                      self.result_text(response))

    def test_collection_roles_without_ansible_galaxy(self):
        def failing_provider():
            raise RuntimeError('the ansible-galaxy command was not found')
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.roles_path,
                               collection_index_provider=failing_provider)
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'collection_roles', 'arguments': {}}})
        self.assertTrue(response['result']['isError'])
        self.assertIn('ansible-galaxy',
                      response['result']['content'][0]['text'])

    # -- collection_role_info ----------------------------------------------

    def test_collection_role_info_plain(self):
        response = self.call_tool('collection_role_info',
                                  {'role': 'testns.testcoll.plainrole'})
        text = self.result_text(response)
        self.assertIn('# testns.testcoll.plainrole', text)
        self.assertIn('A plainly documented role', text)
        self.assertIn('argument specs: none', text)
        self.assertIn('variables: 2', text)
        self.assertIn('resolved at:', text)

    def test_collection_role_info_with_specs(self):
        response = self.call_tool('collection_role_info',
                                  {'role': 'testns.testcoll.specrole'})
        self.assertIn('argument specs: main', self.result_text(response))

    def test_collection_role_info_rejects_bad_names(self):
        for name in ('two.parts', '../..', 'testns.testcoll.nosuch',
                     'nosuch.collection.role'):
            response = self.call_tool('collection_role_info',
                                      {'role': name})
            self.assertTrue(response['result']['isError'], name)

    # -- collection_role_variables -----------------------------------------

    def test_collection_role_variables_from_argument_specs(self):
        response = self.call_tool('collection_role_variables',
                                  {'role': 'testns.testcoll.specrole'})
        text = self.result_text(response)
        # Specs carry type, required and choices
        self.assertIn('- **specrole__state** (str)', text)
        self.assertIn('- **specrole__password** (str, required)', text)
        # Variables present in defaults but not in specs are listed too
        self.assertIn('- **specrole__extra_default**', text)

    def test_collection_role_variables_specs_full(self):
        response = self.call_tool('collection_role_variables',
                                  {'role': 'testns.testcoll.specrole',
                                   'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('## specrole__state', text)
        self.assertIn('choices: present, absent', text)
        self.assertIn('Desired state of the thing.', text)
        self.assertIn('```yaml\npresent\n```', text)

    def test_collection_role_variables_plain_comment_association(self):
        response = self.call_tool('collection_role_variables',
                                  {'role': 'testns.testcoll.plainrole'})
        text = self.result_text(response)
        self.assertIn('- **plainrole__port** — Configure the listen port. '
                      '(default: `9000`)', text)
        self.assertIn('- **plainrole__undocumented** (default: `true`)',
                      text)

    def test_collection_role_variables_names_detail(self):
        response = self.call_tool('collection_role_variables',
                                  {'role': 'testns.testcoll.specrole',
                                   'detail': 'names'})
        text = self.result_text(response)
        self.assertIn('specrole__state', text)
        self.assertNotIn('**', text)

    # -- playbooks ----------------------------------------------------------

    def test_playbooks_list(self):
        response = self.call_tool('playbooks')
        text = self.result_text(response)
        self.assertIn('## Project playbooks (view "system")', text)
        self.assertIn('- `custom` — Custom project playbook '
                      '(hosts: custom_group)', text)
        self.assertIn('## DebOps collection: service/', text)
        self.assertIn('- `service/testrole` — Manage the test role '
                      '(hosts: debops_service_testrole)', text)
        self.assertIn('- `site`', text)
        # Support directories are not listed as playbooks
        self.assertNotIn('vars', text)

    def test_playbooks_filter(self):
        response = self.call_tool('playbooks', {'filter': 'testrole'})
        text = self.result_text(response)
        self.assertIn('service/testrole', text)
        self.assertNotIn('`site`', text)

    def test_playbooks_sets_from_view_configuration(self):
        self.project.config.raw['views'] = {
                'system': {'playbook_sets': {
                    'web': ['service/nginx', 'service/php_fpm']}}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(
                server, roles_path=self.roles_path,
                playbooks_path=self.playbooks_path,
                collection_index_provider=lambda: self.collection_index)
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'playbooks', 'arguments': {}}})
        text = response['result']['content'][0]['text']
        self.assertIn('## Playbook sets (view configuration)', text)
        self.assertIn('- `web` → service/nginx, service/php_fpm', text)

    def test_playbook_info(self):
        response = self.call_tool('playbook_info',
                                  {'playbook': 'service/testrole'})
        text = self.result_text(response)
        self.assertFalse(response['result']['isError'])
        self.assertIn('Execution target: `service/testrole`', text)
        self.assertIn('- Manage the test role — hosts: '
                      'debops_service_testrole (become)', text)
        # Short role names are normalized using the play's collections
        self.assertIn('- debops.debops.keyring', text)
        self.assertIn('- debops.debops.testrole', text)
        self.assertIn('## Inventory groups in use', text)
        self.assertIn('- debops_service_testrole', text)

    def test_playbook_info_imports(self):
        response = self.call_tool('playbook_info', {'playbook': 'site'})
        text = self.result_text(response)
        self.assertIn('## Imported playbooks', text)
        self.assertIn('- service/testrole.yml', text)

    def test_playbook_info_project_playbook(self):
        response = self.call_tool('playbook_info', {'playbook': 'custom'})
        text = self.result_text(response)
        # The task-level include_role statement is found
        self.assertIn('- testrole', text)

    def test_playbook_info_rejects_traversal(self):
        for name in ('../defaults/main', '/etc/passwd', 'nosuch'):
            response = self.call_tool('playbook_info', {'playbook': name})
            self.assertTrue(response['result']['isError'], name)

    # -- collection playbooks ----------------------------------------------

    def test_collection_playbooks(self):
        response = self.call_tool('collection_playbooks',
                                  {'collection': 'testns.testcoll'})
        text = self.result_text(response)
        self.assertIn('# Playbooks in testns.testcoll (1.0.0)', text)
        self.assertIn('- `testns.testcoll/install` — Install the thing '
                      '(hosts: localhost)', text)

    def test_collection_playbooks_no_playbooks_dir(self):
        response = self.call_tool('collection_playbooks',
                                  {'collection': 'testns.nosuch'})
        self.assertTrue(response['result']['isError'])

    def test_collection_playbook_info(self):
        response = self.call_tool('collection_playbook_info',
                                  {'collection': 'testns.testcoll',
                                   'playbook': 'install'})
        text = self.result_text(response)
        self.assertIn('Execution target: `testns.testcoll/install`',
                      text)
        # The task-level include_role statement is found
        self.assertIn('- testns.testcoll.plainrole', text)

    def test_collection_playbook_info_rejects_traversal(self):
        response = self.call_tool('collection_playbook_info',
                                  {'collection': 'testns.testcoll',
                                   'playbook': '../roles/plainrole'})
        self.assertTrue(response['result']['isError'])


if __name__ == '__main__':
    unittest.main()
