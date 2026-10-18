# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The tools are exercised through a real server with a small
fixture Ansible Collection.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, PROTOCOL_VERSION
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

ROLE_TASKS = '''---
- name: Install the test packages
  ansible.builtin.apt:
    name: testrole
  when: testrole__enabled | bool
  tags: [ 'role::testrole', 'skip::testrole' ]

- name: Configure the test service
  ansible.builtin.template:
    src: 'etc/testrole/config.j2'
    dest: '/etc/testrole/config'
  tags: [ 'role::testrole' ]

- name: Internal helper tasks
  ansible.builtin.include_tasks: 'extra.yml'
'''

ROLE_TASKS_EXTRA = '''---
- name: Extra helper task
  ansible.builtin.debug:
    msg: 'extra'
'''

ROLE_VARS = '''---
# Internal state directory of the test role.
testrole__state_dir: '/run/testrole'

testrole__internal: true
'''

ROLE_HANDLERS = '''---
- name: Restart the test service
  ansible.builtin.service:
    name: 'testrole'
    state: 'restarted'
  listen: 'restart testrole'

- name: Reload the test service
  ansible.builtin.service:
    name: 'testrole'
    state: 'reloaded'
  listen: 'reload testrole'
'''

ROLE_HANDLERS_EXTRA = '''---
- name: Reload the extra service
  ansible.builtin.service:
    name: 'extra'
    state: 'reloaded'
  listen: 'reload extra'
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

PLAIN_ROLE_VARS = '''---
# The internal state of the plain role.
plainrole__internal_state: 'on'
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

# Mirrors the parameter-mangling test playbook: play-level vars with
# nested structures, a multi-collection declaration
PARAMS_PLAYBOOK = '''---
- name: Variable mangling example
  collections: ['debops.debops', 'testns.testcoll']
  hosts: ['dione']

  vars:
    test_var:
      - name: foo
        other_param: bar
    test_var_after: "{{ test_var | length }}"

  tasks:

    - name: Check the parsed value
      ansible.builtin.debug:
        var: test_var_after
'''

# Mirrors the shorthand test playbook: action shorthand, bare module
# names, task-level become, exclusion patterns
SHORTHAND_PLAYBOOK = '''---
- hosts: 'grocy'
  tasks:

    - action: ping

    - name: A file, shorthand style
      file:
        path: '/tmp/self'
        state: 'directory'
      become: true

- hosts: 'all,!grocy'
  tasks:

    - debug: var=ansible_hostname
'''

# Jinja-in-string content must survive YAML parsing untouched
ESCAPE_PLAYBOOK = '''---
- hosts: 'grocy'
  tasks:

    - debug:
        msg: '{{ "<!channel> {{ .CommonAnnotations.summary }}" | to_yaml }}'
'''

COLLECTION_PLAYBOOK = '''---
- name: Install the thing
  hosts: localhost
  tasks:
    - name: Include the role
      ansible.builtin.include_role:
        name: testns.testcoll.plainrole
'''


def _auto_commit_noop(self, message=None):
    pass


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}


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
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {'raw': {}})(),
        })()

        self.roles_path = os.path.join(self.tmpdir, 'roles')
        role = os.path.join(self.roles_path, 'testrole')
        os.makedirs(os.path.join(role, 'defaults'))
        os.makedirs(os.path.join(role, 'meta'))
        os.makedirs(os.path.join(role, 'tasks'))
        os.makedirs(os.path.join(role, 'templates', 'etc', 'testrole'))
        os.makedirs(os.path.join(role, 'files', 'etc'))
        os.makedirs(os.path.join(role, 'vars'))
        os.makedirs(os.path.join(role, 'handlers'))
        with open(os.path.join(role, 'defaults', 'main.yml'), 'w') as fp:
            fp.write(ROLE_DEFAULTS)
        with open(os.path.join(role, 'meta', 'main.yml'), 'w') as fp:
            fp.write(ROLE_META)
        with open(os.path.join(role, 'vars', 'main.yml'), 'w') as fp:
            fp.write(ROLE_VARS)
        with open(os.path.join(role, 'handlers', 'main.yml'), 'w') as fp:
            fp.write(ROLE_HANDLERS)
        with open(os.path.join(role, 'handlers', 'extra.yml'), 'w') as fp:
            fp.write(ROLE_HANDLERS_EXTRA)
        with open(os.path.join(role, 'tasks', 'main.yml'), 'w') as fp:
            fp.write(ROLE_TASKS)
        with open(os.path.join(role, 'tasks', 'extra.yml'), 'w') as fp:
            fp.write(ROLE_TASKS_EXTRA)
        with open(os.path.join(role, 'templates', 'etc', 'testrole',
                               'config.j2'), 'w') as fp:
            fp.write('# {{ ansible_managed }}\n')
        with open(os.path.join(role, 'files', 'etc', 'motd.txt'),
                  'w') as fp:
            fp.write('A static file\n')

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
        os.makedirs(os.path.join(plainrole, 'vars'))
        with open(os.path.join(plainrole, 'meta', 'main.yml'), 'w') as fp:
            fp.write(PLAIN_ROLE_META)
        with open(os.path.join(plainrole, 'defaults', 'main.yml'), 'w') as fp:
            fp.write(PLAIN_ROLE_DEFAULTS)
        with open(os.path.join(plainrole, 'vars', 'main.yml'), 'w') as fp:
            fp.write(PLAIN_ROLE_VARS)

        # Playbooks: one project playbook, a fixture bundled collection
        os.makedirs(os.path.join(project_dir, 'ansible', 'playbooks'))
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'custom.yml'), 'w') as fp:
            fp.write(CUSTOM_PLAYBOOK)
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'params.yml'), 'w') as fp:
            fp.write(PARAMS_PLAYBOOK)
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'shorthand.yml'), 'w') as fp:
            fp.write(SHORTHAND_PLAYBOOK)
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'escape.yml'), 'w') as fp:
            fp.write(ESCAPE_PLAYBOOK)

        # A project-local loose role, and a project collection role
        os.makedirs(os.path.join(project_dir, 'ansible', 'playbooks',
                                 'roles', 'shadowrole', 'meta'))
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'roles', 'shadowrole', 'meta', 'main.yml'),
                  'w') as fp:
            fp.write('---\ngalaxy_info:\n  description: The project copy\n')
        project_collection = os.path.join(
                project_dir, 'ansible', 'collections',
                'ansible_collections', 'local', 'local')
        os.makedirs(os.path.join(project_collection, 'roles', 'localrole',
                                 'defaults'))
        with open(os.path.join(project_collection, 'roles', 'localrole',
                               'defaults', 'main.yml'), 'w') as fp:
            fp.write('---\nlocalrole__enabled: true\n')

        # The same role name also exists in the bundled fixture
        os.makedirs(os.path.join(self.roles_path, 'shadowrole', 'meta'))
        with open(os.path.join(self.roles_path, 'shadowrole', 'meta',
                               'main.yml'), 'w') as fp:
            fp.write('---\ngalaxy_info:\n  description: The bundled copy\n')

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
            'jsonrpc': '2.0', '_meta': REQUEST_META,
            'id': request_id, 'method': 'tools/call',
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
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/list',
                'params': {}})
        names = [tool['name'] for tool in response['result']['tools']]
        self.assertEqual(sorted(names),
                         ['ansible_facts', 'ansible_ping', 'check',
                          'collection_playbooks', 'collection_roles',
                          'config_get', 'inventory_query', 'playbook_info',
                          'playbooks', 'project_files', 'project_pull',
                          'project_push', 'project_read', 'project_status',
                          'role_argspec', 'role_defaults', 'role_files',
                          'role_handlers', 'role_info', 'role_tasks',
                          'role_templates', 'role_variables',
                          'secret_paths'])

    def test_tool_annotations(self):
        # 'run' is only registered when the policy allows changes
        self.project.config.raw['project'] = {'mcp': {'execute': 'all'}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.roles_path)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1,
                'method': 'tools/list', 'params': {}})
        annotations = {tool['name']: tool.get('annotations', {})
                       for tool in response['result']['tools']}
        # Pure reads are marked read-only
        self.assertEqual(annotations['project_status'],
                         {'readOnlyHint': True, 'openWorldHint': False})
        self.assertEqual(annotations['role_defaults'],
                         {'readOnlyHint': True, 'openWorldHint': False})
        # Host queries are read-only but reach out over SSH
        self.assertEqual(annotations['ansible_ping'],
                         {'readOnlyHint': True})
        # Changes and pushes are destructive; check runs are not
        self.assertTrue(annotations['run']['destructiveHint'])
        self.assertTrue(annotations['project_push']['destructiveHint'])
        self.assertFalse(annotations['check']['destructiveHint'])

    def test_denied_tool_is_not_registered(self):
        self.project.config.raw['project'] = {'mcp': {'deny': ['role_info']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.roles_path)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/list',
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
        self.assertIn('resolved at:', text)
        self.assertIn('- defaults: 4 variables', text)
        self.assertIn('- vars: 2 variables', text)
        self.assertIn('- argument specs: none', text)
        self.assertIn('- tasks: 4 across 2 files', text)
        self.assertIn('- handlers: 3 across 2 files', text)
        self.assertIn('- dependencies: debops.otherrole', text)

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

    # -- documentation pointers ----------------------------------------------

    def test_docs_version_slug(self):
        from debops.mcp.tools.roles import _docs_version_slug
        self.assertEqual(_docs_version_slug('3.3.0.dev487+g547727090'),
                         'master')
        self.assertEqual(_docs_version_slug('3.3.0'), 'stable-3.3')
        self.assertEqual(_docs_version_slug('3.2.1'), 'stable-3.2')
        self.assertEqual(_docs_version_slug('bogus'), 'master')
        self.assertEqual(_docs_version_slug(None), 'master')

    def test_role_info_has_documentation_for_bundled_roles(self):
        response = self.call_tool('role_info', {'name': 'testrole'})
        text = self.result_text(response)
        self.assertIn('- documentation: https://docs.debops.org/en/master/'
                      'ansible/roles/testrole/index.html#debops-testrole',
                      text)

    def test_role_info_no_documentation_for_project_roles(self):
        response = self.call_tool('role_info', {'name': 'shadowrole'})
        self.assertNotIn('documentation:', self.result_text(response))

    def test_role_info_documentation_uses_the_server_version(self):
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project),
                           version='3.2.1')
        register_project_tools(
                server, roles_path=self.roles_path,
                playbooks_path=self.playbooks_path,
                collection_index_provider=lambda: self.collection_index)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/call',
                'params': {'name': 'role_info',
                           'arguments': {'name': 'testrole'}}})
        self.assertIn('en/stable-3.2/',
                      response['result']['content'][0]['text'])

    def test_role_defaults_full_links_to_documentation(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testrole', 'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('Detailed documentation: https://docs.debops.org/'
                      'en/master/ansible/roles/testrole/'
                      'defaults-detailed.html', text)

    def test_role_defaults_brief_has_no_link(self):
        response = self.call_tool('role_defaults', {'name': 'testrole'})
        self.assertNotIn('docs.debops.org', self.result_text(response))

    # -- role_defaults --------------------------------------------------

    def test_role_defaults_full(self):
        response = self.call_tool('role_defaults',
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

    def test_role_defaults_names_detail(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testrole', 'detail': 'names'})
        text = self.result_text(response)
        self.assertIn('(4)', text)
        self.assertIn('testrole__enabled', text)
        self.assertIn('testrole__long_list', text)
        self.assertNotIn('##', text)
        self.assertNotIn('```', text)

    def test_role_defaults_brief_is_the_default(self):
        response = self.call_tool('role_defaults', {'name': 'testrole'})
        text = self.result_text(response)
        # One compact line per variable, no full documentation
        self.assertIn('- **testrole__enabled** — Enable or disable the '
                      'test role. (default: `true`)', text)
        self.assertNotIn('```yaml', text)
        self.assertNotIn('## testrole__enabled', text)

    def test_role_defaults_brief_truncates_long_defaults(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testrole',
                                   'filter': 'long_list'})
        text = self.result_text(response)
        (line,) = [line for line in text.splitlines()
                   if line.startswith('- **')]
        self.assertLess(len(line), 140)
        self.assertTrue(line.endswith('…`)'))

    def test_role_defaults_invalid_detail(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testrole', 'detail': 'everything'})
        self.assertTrue(response['result']['isError'])

    def test_role_defaults_filter(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testrole', 'filter': 'port',
                                   'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('testrole__port', text)
        self.assertNotIn('testrole__enabled', text)

    def test_role_defaults_no_match(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testrole', 'filter': 'zzz'})
        self.assertIn('no matching variables', self.result_text(response))

    def test_role_defaults_unknown_role(self):
        response = self.call_tool('role_defaults', {'name': 'nosuch'})
        self.assertTrue(response['result']['isError'])

    # -- role_variables (vars/) ----------------------------------------------

    def test_role_variables_reads_vars(self):
        response = self.call_tool('role_variables', {'name': 'testrole'})
        text = self.result_text(response)
        self.assertIn('- **testrole__state_dir** — Internal state '
                      'directory of the test role. (default: '
                      '`/run/testrole`)', text)
        self.assertIn('- **testrole__internal** (default: `true`)', text)

    def test_role_variables_full(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testrole', 'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('## testrole__state_dir', text)
        self.assertIn('```yaml\n/run/testrole\n```', text)

    def test_role_variables_no_vars(self):
        response = self.call_tool('role_variables', {'name': 'splitrole'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('no variables', self.result_text(response))

    # -- split defaults files ----------------------------------------------

    def test_role_defaults_split_defaults(self):
        response = self.call_tool('role_defaults',
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
        self.assertIn('- defaults: 3 variables', text)

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
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/call',
                'params': {'name': 'collection_roles', 'arguments': {}}})
        self.assertTrue(response['result']['isError'])
        self.assertIn('ansible-galaxy',
                      response['result']['content'][0]['text'])

    # -- fully qualified role names (unified resolution) ---------------------

    def test_role_info_with_fqcn(self):
        response = self.call_tool('role_info',
                                  {'name': 'testns.testcoll.plainrole'})
        text = self.result_text(response)
        self.assertIn('A plainly documented role', text)
        self.assertIn('- argument specs: none', text)
        self.assertIn('resolved at:', text)

    def test_role_info_rejects_bad_fqcn(self):
        for name in ('two.parts', 'a.b.c.d', '../..'):
            response = self.call_tool('role_info', {'name': name})
            self.assertTrue(response['result']['isError'], name)

    def test_role_argspec_with_fqcn(self):
        response = self.call_tool('role_argspec',
                                  {'name': 'testns.testcoll.specrole'})
        text = self.result_text(response)
        self.assertIn('argument spec: main', text)
        self.assertIn('- **specrole__state** (str)', text)
        self.assertIn('- **specrole__password** (str, required)', text)

    def test_role_argspec_full(self):
        response = self.call_tool('role_argspec',
                                  {'name': 'testns.testcoll.specrole',
                                   'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('## specrole__state', text)
        self.assertIn('(type: str; choices: present, absent)', text)
        self.assertIn('Desired state of the thing.', text)
        self.assertIn('```yaml\npresent\n```', text)

    def test_role_argspec_none_present(self):
        response = self.call_tool('role_argspec', {'name': 'testrole'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('no argument specs', self.result_text(response))

    def test_role_variables_with_fqcn(self):
        response = self.call_tool('role_variables',
                                  {'name': 'testns.testcoll.plainrole'})
        text = self.result_text(response)
        self.assertIn('- **plainrole__internal_state** — The internal '
                      'state of the plain role. (default: `\'on\'`)', text)

    def test_role_defaults_with_fqcn(self):
        response = self.call_tool('role_defaults',
                                  {'name': 'testns.testcoll.specrole'})
        text = self.result_text(response)
        self.assertIn('- **specrole__extra_default**', text)

    def test_role_variables_fqcn_unknown_collection(self):
        response = self.call_tool('role_variables',
                                  {'name': 'nosuch.collection.role'})
        self.assertTrue(response['result']['isError'])

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
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/call',
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

    def test_collection_playbooks_all_collections(self):
        response = self.call_tool('collection_playbooks')
        text = self.result_text(response)
        self.assertIn('# Collections with playbooks visible to Ansible '
                      '(1)', text)
        self.assertIn('## testns.testcoll (1.0.0)', text)
        self.assertIn('- `testns.testcoll/install` — Install the thing '
                      '(hosts: localhost)', text)

    def test_collection_playbooks_all_collections_filtered(self):
        # The filter matches the qualified 'namespace.collection/path'
        response = self.call_tool('collection_playbooks',
                                  {'filter': 'testcoll/inst'})
        self.assertIn('install', self.result_text(response))
        response = self.call_tool('collection_playbooks',
                                  {'filter': 'nomatch'})
        self.assertIn('no collections with playbooks found',
                      self.result_text(response))

    def test_playbook_info_slash_collection_form(self):
        response = self.call_tool('playbook_info',
                                  {'playbook': 'testns.testcoll/install'})
        text = self.result_text(response)
        self.assertIn('Execution target: `testns.testcoll/install`',
                      text)
        # The task-level include_role statement is found
        self.assertIn('- testns.testcoll.plainrole', text)

    def test_playbook_info_fqcn_form(self):
        response = self.call_tool('playbook_info',
                                  {'playbook': 'testns.testcoll.install'})
        text = self.result_text(response)
        # The dotted form is itself a valid execution target
        self.assertIn('Execution target: `testns.testcoll.install`', text)

    def test_playbook_info_collection_traversal_rejected(self):
        response = self.call_tool('playbook_info',
                                  {'playbook':
                                   'testns.testcoll/../roles/plainrole'})
        self.assertTrue(response['result']['isError'])

    # -- content policy -----------------------------------------------------
    # -- role_tasks / role_templates / role_files ----------------------------

    def test_role_tasks_overview(self):
        response = self.call_tool('role_tasks', {'name': 'testrole'})
        text = self.result_text(response)
        self.assertIn('- `main` — 3 tasks', text)
        self.assertIn('- `extra` — 1 task', text)
        self.assertIn('(4 tasks)', text)

    def test_role_tasks_file_brief(self):
        response = self.call_tool('role_tasks',
                                  {'name': 'testrole', 'file': 'main'})
        text = self.result_text(response)
        self.assertIn('- Install the test packages — ansible.builtin.apt',
                      text)
        self.assertIn('- Internal helper tasks — '
                      'ansible.builtin.include_tasks', text)
        self.assertNotIn('when:', text)

    def test_role_tasks_file_full(self):
        response = self.call_tool('role_tasks',
                                  {'name': 'testrole', 'file': 'main',
                                   'detail': 'full'})
        text = self.result_text(response)
        self.assertIn('when: testrole__enabled | bool', text)
        self.assertIn('tags: role::testrole, skip::testrole', text)

    def test_role_tasks_filter(self):
        response = self.call_tool('role_tasks',
                                  {'name': 'testrole', 'file': 'main',
                                   'filter': 'configure'})
        text = self.result_text(response)
        self.assertIn('Configure the test service', text)
        self.assertNotIn('Install the test packages', text)

    def test_role_tasks_unknown_file(self):
        response = self.call_tool('role_tasks',
                                  {'name': 'testrole', 'file': 'nosuch'})
        self.assertTrue(response['result']['isError'])

    def test_role_tasks_traversal_rejected(self):
        response = self.call_tool('role_tasks',
                                  {'name': 'testrole',
                                   'file': '../defaults/main'})
        self.assertTrue(response['result']['isError'])

    def test_role_tasks_no_tasks_dir(self):
        response = self.call_tool('role_tasks', {'name': 'splitrole'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('no tasks', self.result_text(response))

    def test_role_templates(self):
        response = self.call_tool('role_templates', {'name': 'testrole'})
        text = self.result_text(response)
        self.assertIn('- etc/testrole/config.j2', text)

        response = self.call_tool('role_templates',
                                  {'name': 'testrole',
                                   'path': 'etc/testrole/config.j2'})
        self.assertIn('ansible_managed', self.result_text(response))

    def test_role_templates_traversal_rejected(self):
        response = self.call_tool('role_templates',
                                  {'name': 'testrole',
                                   'path': '../../defaults/main.yml'})
        self.assertTrue(response['result']['isError'])

    def test_role_files(self):
        response = self.call_tool('role_files', {'name': 'testrole'})
        self.assertIn('- etc/motd.txt', self.result_text(response))

        response = self.call_tool('role_files',
                                  {'name': 'testrole',
                                   'path': 'etc/motd.txt'})
        self.assertIn('A static file', self.result_text(response))

    def test_role_files_missing(self):
        response = self.call_tool('role_files', {'name': 'splitrole'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('no files', self.result_text(response))

    # -- role_handlers -------------------------------------------------------

    def test_role_handlers_overview(self):
        response = self.call_tool('role_handlers', {'name': 'testrole'})
        text = self.result_text(response)
        self.assertIn('- `main` — 2 handlers', text)
        self.assertIn('- `extra` — 1 handler', text)
        self.assertIn('(3 handlers)', text)

    def test_role_handlers_file_with_listen(self):
        response = self.call_tool('role_handlers',
                                  {'name': 'testrole', 'file': 'main'})
        text = self.result_text(response)
        self.assertIn('- Restart the test service — '
                      'ansible.builtin.service (listen: restart testrole)',
                      text)

    def test_role_handlers_no_handlers_dir(self):
        response = self.call_tool('role_handlers', {'name': 'splitrole'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('no handlers', self.result_text(response))

    # -- content policy -----------------------------------------------------

    def _server_with_policy(self, mcp_config, index=None):
        self.project.config.raw['project'] = {'mcp': mcp_config}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(
                server, roles_path=self.roles_path,
                playbooks_path=self.playbooks_path,
                collection_index_provider=(
                        (lambda: index) if index is not None
                        else (lambda: self.collection_index)))
        return server

    def test_role_tools_search_project_first(self):
        response = self.call_tool('role_info', {'name': 'shadowrole'})
        self.assertIn('The project copy', self.result_text(response))

    def test_bundled_content_off_hides_bundled_roles(self):
        server = self._server_with_policy({'bundled_content': False})
        # The project role still works...
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/call',
                'params': {'name': 'role_info',
                           'arguments': {'name': 'shadowrole'}}})
        self.assertIn('The project copy',
                      response['result']['content'][0]['text'])
        # ... and the bundled-only role is not found
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 2, 'method': 'tools/call',
                'params': {'name': 'role_info',
                           'arguments': {'name': 'testrole'}}})
        self.assertTrue(response['result']['isError'])

    def test_bundled_off_without_project_roles_unregisters_role_tools(self):
        import shutil as _shutil
        _shutil.rmtree(os.path.join(self.project.path, 'ansible',
                                    'playbooks', 'roles'))
        _shutil.rmtree(os.path.join(self.project.path, 'ansible',
                                    'collections'))
        server = self._server_with_policy({'bundled_content': False})
        self.assertNotIn('role_info', server.tools)
        self.assertNotIn('role_variables', server.tools)

    def test_bundled_content_off_removes_collection_playbooks(self):
        server = self._server_with_policy({'bundled_content': False})
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/call',
                'params': {'name': 'playbooks', 'arguments': {}}})
        text = response['result']['content'][0]['text']
        self.assertIn('Project playbooks', text)
        self.assertNotIn('DebOps collection', text)

    def test_external_content_off_filters_the_index(self):
        project_root = os.path.join(self.project.path, 'ansible',
                                    'collections', 'ansible_collections')
        # The bundled entry needs a real roles/ directory to be listed
        bundled = os.path.join(self.tmpdir, 'bundled', 'debops', 'debops')
        os.makedirs(os.path.join(bundled, 'roles', 'testrole'))
        index = {
            'debops.debops': {'path': bundled,
                              'version': '1.0'},
            'local.local': {'path': os.path.join(project_root, 'local',
                                                 'local'),
                            'version': '1.0'},
            'grafana.grafana': {'path': '/usr/share/grafana/grafana',
                                'version': '6.0'},
        }
        server = self._server_with_policy({'external_content': False},
                                          index=index)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1, 'method': 'tools/call',
                'params': {'name': 'collection_roles', 'arguments': {}}})
        text = response['result']['content'][0]['text']
        self.assertIn('debops.debops', text)
        self.assertIn('local.local', text)
        self.assertNotIn('grafana', text)


if __name__ == '__main__':
    unittest.main()
