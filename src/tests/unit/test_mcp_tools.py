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
from debops.mcp.tools.playbooks import playbook_summary
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


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


class ViewWriteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-write-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       check=True)
        # The tool commits explicitly; the stub records the messages
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
        self.view_root = os.path.join(self.tmpdir, 'ansible')

    def result_text(self, result):
        # The tool is called directly, so the result has no envelope
        content = result['content']
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]['type'], 'text')
        return content[0]['text']

    def write(self, path, content='x\n', mode=None):
        from debops.mcp.tools.files import tool_view_write
        arguments = {'path': path, 'content': content}
        if mode is not None:
            arguments['mode'] = mode
        return tool_view_write(self.project, self.policy, arguments)

    def written(self, relative):
        with open(os.path.join(self.view_root, relative)) as fp:
            return fp.read()

    def written_mode(self, relative):
        return stat.S_IMODE(os.stat(
                os.path.join(self.view_root, relative)).st_mode)

    def test_create(self):
        response = self.write('resources/scripts/hello.sh',
                              content='#!/bin/sh\n', mode='0755')
        text = self.result_text(response)
        self.assertIn('created: resources/scripts/hello.sh', text)
        self.assertEqual(self.written('resources/scripts/hello.sh'),
                         '#!/bin/sh\n')
        self.assertEqual(self.written_mode('resources/scripts/hello.sh'),
                         0o755)
        # The commit is part of the write
        self.assertEqual(self.commits,
                         ['DebOps MCP: view_write '
                          'resources/scripts/hello.sh'])

    def test_unchanged_content_commits_nothing(self):
        self.write('playbooks/deploy.yml')
        self.write('playbooks/deploy.yml')
        self.assertEqual(len(self.commits), 1)

    def test_default_mode_for_new_files(self):
        response = self.write('playbooks/deploy.yml')
        self.assertIn('mode 0644', self.result_text(response))
        self.assertEqual(self.written_mode('playbooks/deploy.yml'),
                         0o644)

    def test_overwrite_preserves_the_mode(self):
        self.write('resources/hello.sh', content='#!/bin/sh\n',
                   mode='0750')
        response = self.write('resources/hello.sh',
                              content='#!/bin/sh\ntrue\n')
        self.assertIn('overwritten:', self.result_text(response))
        self.assertEqual(self.written_mode('resources/hello.sh'), 0o750)
        self.assertEqual(self.written('resources/hello.sh'),
                         '#!/bin/sh\ntrue\n')

    def test_unchanged_short_circuit(self):
        self.write('playbooks/deploy.yml')
        response = self.write('playbooks/deploy.yml')
        self.assertIn('unchanged:', self.result_text(response))

    def test_mode_change_on_identical_content(self):
        self.write('resources/hello.sh', content='#!/bin/sh\n')
        response = self.write('resources/hello.sh',
                              content='#!/bin/sh\n', mode='0700')
        self.assertIn('mode changed:', self.result_text(response))
        self.assertEqual(self.written_mode('resources/hello.sh'), 0o700)

    def test_escape_is_rejected(self):
        for path in ('../outside.yml', '/etc/passwd',
                     'playbooks/../../outside.yml'):
            response = self.write(path)
            self.assertTrue(response['isError'], path)

    def test_secret_is_never_writable(self):
        self.project.config.raw['project'] = {
                'mcp': {'write_allow': ['secret/**', 'resources/**']}}
        policy = McpPolicy(self.project)
        from debops.mcp.tools.files import tool_view_write
        response = tool_view_write(
                self.project, policy,
                {'path': 'secret/password', 'content': 'x'})
        self.assertTrue(response['isError'])
        self.assertIn('denied by policy',
                      response['content'][0]['text'])

    def test_path_outside_the_allowlist_is_rejected(self):
        response = self.write('inventory/hosts')
        self.assertTrue(response['isError'])
        self.assertIn('not writable', response['content'][0]
                      ['text'])

    def test_symlink_escape_is_rejected(self):
        outside = os.path.join(self.tmpdir, 'outside')
        os.makedirs(outside)
        os.makedirs(os.path.join(self.view_root, 'playbooks'))
        os.symlink(outside, os.path.join(self.view_root, 'playbooks',
                                         'link'))
        response = self.write('playbooks/link/file.yml')
        self.assertTrue(response['isError'])

    def test_mode_validation(self):
        for bad in ('755', '7777', '04755', 'abc', '01777'):
            response = self.write('playbooks/x.yml', mode=bad)
            self.assertTrue(response['isError'], bad)

    def test_non_git_project_is_refused(self):
        os.unlink(os.path.join(self.tmpdir, '.git', 'HEAD'))
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        response = self.write('playbooks/deploy.yml')
        self.assertTrue(response['isError'])
        self.assertIn('not a git repository',
                      response['content'][0]['text'])

    def test_directory_is_not_a_file(self):
        os.makedirs(os.path.join(self.view_root, 'playbooks', 'sub'))
        response = self.write('playbooks/sub')
        self.assertTrue(response['isError'])

    def test_disk_full_is_a_clean_refusal(self):
        # Through the registered server, so the refusal is converted
        # to a tool error instead of raising through the dispatch
        self.project.config.raw['project'] = {
                'mcp': {'min_free_mb': 2 ** 40}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.tmpdir)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1,
                'method': 'tools/call',
                'params': {'name': 'view_write',
                           'arguments': {'path': 'playbooks/x.yml',
                                         'content': '---\n'}}})
        result = response['result']
        self.assertTrue(result['isError'])
        self.assertIn('not enough free disk space',
                      result['content'][0]['text'])

    def test_write_failure_is_a_clean_error(self):
        with mock.patch('debops.mcp.tools.files.os.replace',
                        side_effect=OSError(28, 'No space left')):
            response = self.write('playbooks/x.yml', content='---\n')
        self.assertTrue(response['isError'])
        self.assertIn('No space left', response['content'][0]['text'])

    def test_commit_failure_is_reported(self):
        def raising(instance, message=None):
            raise RuntimeError('disk full mid-commit')
        self.project.commit = raising
        response = self.write('playbooks/x.yml', content='---\n')
        self.assertTrue(response['isError'])
        text = response['content'][0]['text']
        self.assertIn('the commit failed', text)
        self.assertIn('not recorded', text)
        # The write itself landed
        self.assertEqual(self.written('playbooks/x.yml'), '---\n')

    # -- view_delete ---------------------------------------------------

    def delete(self, path):
        from debops.mcp.tools.files import tool_view_delete
        return tool_view_delete(self.project, self.policy,
                                {'path': path})

    def test_delete(self):
        self.write('playbooks/deploy.yml', content='---\n')
        response = self.delete('playbooks/deploy.yml')
        self.assertIn('deleted: playbooks/deploy.yml',
                      self.result_text(response))
        self.assertFalse(os.path.exists(
                os.path.join(self.view_root, 'playbooks', 'deploy.yml')))
        self.assertEqual(self.commits[-1],
                         'DebOps MCP: view_delete playbooks/deploy.yml')

    def test_delete_missing_file_is_an_error(self):
        response = self.delete('playbooks/nosuch.yml')
        self.assertTrue(response['isError'])
        self.assertIn('not found', response['content'][0]['text'])

    def test_delete_refuses_directories(self):
        os.makedirs(os.path.join(self.view_root, 'playbooks', 'sub'))
        response = self.delete('playbooks/sub')
        self.assertTrue(response['isError'])

    def test_delete_escape_is_rejected(self):
        response = self.delete('../outside.yml')
        self.assertTrue(response['isError'])

    def test_delete_secret_is_never_allowed(self):
        response = self.delete('secret/password')
        self.assertTrue(response['isError'])
        self.assertIn('denied by policy',
                      response['content'][0]['text'])

    def test_delete_outside_the_allowlist_is_rejected(self):
        response = self.delete('inventory/hosts')
        self.assertTrue(response['isError'])
        self.assertIn('not writable', response['content'][0]['text'])

    def test_delete_non_git_project_is_refused(self):
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        response = self.delete('playbooks/deploy.yml')
        self.assertTrue(response['isError'])
        self.assertIn('not a git repository',
                      response['content'][0]['text'])

    # -- scoped writes into '.debops/mcp/' and the anchors --------------

    GOOD_SKILL = ('---\nname: demo\ndescription: A demo skill\n---\n\n'
                  '# Demo\n')

    def write_scoped(self, path, content='x\n', allow=None):
        from debops.mcp.tools.files import tool_view_write
        if allow is not None:
            self.project.config.raw['project'] = {
                    'mcp': {'write_allow': allow}}
            self.policy = McpPolicy(self.project)
        return tool_view_write(self.project, self.policy,
                               {'path': path, 'content': content})

    def test_mcp_content_is_not_writable_by_default(self):
        response = self.write_scoped('.debops/mcp/skills/demo/SKILL.md',
                                     content=self.GOOD_SKILL)
        self.assertTrue(response['isError'])
        self.assertIn('not writable', response['content'][0]['text'])

    def test_mcp_content_with_an_opt_in_pattern(self):
        response = self.write_scoped(
                '.debops/mcp/skills/demo/SKILL.md', content=self.GOOD_SKILL,
                allow=['resources/**', 'playbooks/**',
                       '.debops/mcp/skills/**'])
        self.assertIn('created: .debops/mcp/skills/demo/SKILL.md',
                      self.result_text(response))
        # The project anchor: outside the view root, under the project
        with open(os.path.join(self.tmpdir, '.debops', 'mcp', 'skills',
                               'demo', 'SKILL.md')) as fp:
            self.assertEqual(fp.read(), self.GOOD_SKILL)

    def test_invalid_skill_content_is_refused(self):
        bad = '---\nname: other\ndescription: Mismatch\n---\n'
        response = self.write_scoped('.debops/mcp/skills/demo/SKILL.md',
                                     content=bad,
                                     allow=['.debops/mcp/skills/**'])
        self.assertTrue(response['isError'])
        self.assertIn('does not match', response['content'][0]['text'])
        response = self.write_scoped('.debops/mcp/skills/demo/SKILL.md',
                                     content='---\nname: demo\n---\n',
                                     allow=['.debops/mcp/skills/**'])
        self.assertTrue(response['isError'])
        self.assertIn('no description', response['content'][0]['text'])

    def test_prompt_write_and_validation(self):
        good = '---\ndescription: A prompt\n---\n\nDo it.\n'
        response = self.write_scoped('.debops/mcp/prompts/demo.md',
                                     content=good,
                                     allow=['.debops/mcp/prompts/**'])
        self.assertIn('created:', self.result_text(response))
        # Prompts are flat '<name>.md' files
        response = self.write_scoped('.debops/mcp/prompts/sub/demo.md',
                                     content=good,
                                     allow=['.debops/mcp/prompts/**'])
        self.assertTrue(response['isError'])
        response = self.write_scoped('.debops/mcp/prompts/demo.md',
                                     content='no frontmatter\n',
                                     allow=['.debops/mcp/prompts/**'])
        self.assertTrue(response['isError'])
        self.assertIn('no description', response['content'][0]['text'])

    def test_mcp_content_write_refused_when_content_disabled(self):
        self.project.config.raw['project'] = {
                'mcp': {'project_content': False,
                        'write_allow': ['.debops/mcp/skills/**']}}
        self.policy = McpPolicy(self.project)
        response = self.write_scoped('.debops/mcp/skills/demo/SKILL.md',
                                     content=self.GOOD_SKILL)
        self.assertTrue(response['isError'])
        self.assertIn('disabled', response['content'][0]['text'])

    def test_ansible_prefix_anchors_at_the_project(self):
        # In a legacy project the view root is 'ansible/': both
        # spellings land in the same place
        response = self.write_scoped('ansible/playbooks/deploy.yml',
                                     content='---\n',
                                     allow=['ansible/playbooks/**'])
        self.assertIn('created: ansible/playbooks/deploy.yml',
                      self.result_text(response))

    def test_leading_slash_anchors_at_the_project_root(self):
        response = self.write_scoped('/playbooks/deploy.yml',
                                     content='---\n',
                                     allow=['/playbooks/**'])
        self.assertIn('created: /playbooks/deploy.yml',
                      self.result_text(response))
        # Project-root-anchored, outside the view
        self.assertTrue(os.path.isfile(os.path.join(
                self.tmpdir, 'playbooks', 'deploy.yml')))

    def test_anchored_patterns_do_not_cross(self):
        # A view pattern must not authorize a root path, nor the
        # reverse
        response = self.write_scoped('/playbooks/deploy.yml',
                                     content='---\n',
                                     allow=['playbooks/**'])
        self.assertTrue(response['isError'])
        self.assertIn('not writable', response['content'][0]['text'])
        response = self.write_scoped('playbooks/deploy.yml',
                                     content='---\n',
                                     allow=['/playbooks/**'])
        self.assertTrue(response['isError'])

    def test_absolute_looking_paths_stay_in_the_project(self):
        # '/etc/passwd' is project-root-anchored, not filesystem-absolute
        response = self.write_scoped('/etc/passwd', allow=['/**'])
        self.assertFalse(response['isError'])
        self.assertTrue(os.path.isfile(os.path.join(
                self.tmpdir, 'etc', 'passwd')))
        # ... and without a matching pattern it is refused
        response = self.write_scoped('/etc/shadow', allow=[])
        self.assertTrue(response['isError'])
        self.assertIn('not writable', response['content'][0]['text'])

    def test_mcp_content_still_scoped_with_the_new_anchor(self):
        # '.debops/' patterns keep their prefix and their restriction
        response = self.write_scoped('.debops/conf.d/evil.toml',
                                     allow=['.debops/**'])
        self.assertTrue(response['isError'])
        self.assertIn('MCP content directories',
                      response['content'][0]['text'])

    def test_default_covers_the_full_resolution_surface(self):
        # The default write_allow reaches every directory the playbook
        # runner resolves playbooks from (legacy view == ansible/ here)
        for path in ('playbooks/in-view.yml', 'ansible/playbooks/shared.yml',
                     '/playbooks/at-root.yml'):
            response = self.write(path)
            self.assertFalse(response['isError'],
                             '{}: {}'.format(
                                 path, response['content'][0]['text']))
        self.assertTrue(os.path.isfile(os.path.join(
                self.view_root, 'playbooks', 'in-view.yml')))
        self.assertTrue(os.path.isfile(os.path.join(
                self.tmpdir, 'playbooks', 'at-root.yml')))

    def test_git_metadata_is_never_writable(self):
        response = self.write_scoped('.git/config',
                                     allow=['.git/**'])
        self.assertTrue(response['isError'])
        self.assertIn('denied by policy',
                      response['content'][0]['text'])

    def test_dotenv_files_are_never_writable(self):
        for path in ('playbooks/.env', 'resources/.env.local'):
            response = self.write_scoped(path)
            self.assertTrue(response['isError'], path)
            self.assertIn('denied by policy',
                          response['content'][0]['text'], path)

    def test_project_machinery_is_never_writable(self):
        # Even a wildcard allow cannot open the configuration, the
        # hooks or the environment: only the MCP content directories
        # exist under '.debops/'
        for path in ('.debops/conf.d/mcp.toml',
                     '.debops/hooks/pre-run',
                     '.debops/environment',
                     '.debops/mcp/other/x',
                     '.debops/mcp/skills'):
            response = self.write_scoped(path, allow=['.debops/**'])
            self.assertTrue(response['isError'], path)
            self.assertIn('MCP content directories',
                          response['content'][0]['text'], path)

    def test_ansible_cfg_is_never_writable(self):
        for path in ('ansible.cfg',
                     'ansible/ansible.cfg'):
            response = self.write_scoped(path,
                                         allow=['**'])
            self.assertTrue(response['isError'], path)
            self.assertIn('denied by policy',
                          response['content'][0]['text'], path)

    def test_write_announces_mcp_content_changes(self):
        events = []
        server = type('Server', (), {
                'emit_list_changed': lambda self, kind:
                events.append(('list', kind)),
                'emit_resource_updated': lambda self, uri:
                events.append(('updated', uri)),
        })()
        from debops.mcp.tools.files import tool_view_write
        self.project.config.raw['project'] = {
                'mcp': {'write_allow': ['.debops/mcp/skills/**']}}
        self.policy = McpPolicy(self.project)
        tool_view_write(self.project, self.policy,
                        {'path': '.debops/mcp/skills/demo/SKILL.md',
                         'content': self.GOOD_SKILL}, server=server)
        self.assertEqual(events, [('list', 'resources'),
                                  ('updated', 'skill://demo')])


class OverridesWriteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-overrides-')
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
        self.target = os.path.join(self.tmpdir, 'ansible', 'overrides')

    def write(self, path, content='x\n', mode=None):
        from debops.mcp.tools.overrides import tool_overrides_write
        arguments = {'path': path, 'content': content}
        if mode is not None:
            arguments['mode'] = mode
        return tool_overrides_write(self.project, self.policy, arguments)

    def result_text(self, result):
        content = result['content']
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]['type'], 'text')
        return content[0]['text']

    def test_not_registered_by_default(self):
        server = McpServer(project=self.project, policy=self.policy)
        register_project_tools(server, roles_path=self.tmpdir)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1,
                'method': 'tools/list', 'params': {}})
        names = [tool['name'] for tool in response['result']['tools']]
        self.assertNotIn('overrides_write', names)

    def test_registered_when_enabled(self):
        self.project.config.raw['project'] = {'mcp': {'overrides': True}}
        policy = McpPolicy(self.project)
        server = McpServer(project=self.project, policy=policy)
        register_project_tools(server, roles_path=self.tmpdir)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1,
                'method': 'tools/list', 'params': {}})
        names = [tool['name'] for tool in response['result']['tools']]
        self.assertIn('overrides_write', names)

    def test_write_lands_and_commits(self):
        response = self.write('tasks/extra.yml',
                              content='---\n- name: extra\n')
        self.assertIn('created: ansible/overrides/tasks/extra.yml',
                      self.result_text(response))
        with open(os.path.join(self.target, 'tasks', 'extra.yml')) as fp:
            self.assertEqual(fp.read(), '---\n- name: extra\n')
        self.assertEqual(self.commits,
                         ['DebOps MCP: overrides_write tasks/extra.yml'])

    def test_only_the_known_subdirectories(self):
        for path in ('other/x', 'files', 'files/', 'files/../out',
                     'templates/x.j2'):
            response = self.write(path)
            if path == 'templates/x.j2':
                self.assertFalse(response['isError'])
            else:
                self.assertTrue(response['isError'], path)

    def test_deny_components_apply(self):
        response = self.write('files/secret/x')
        self.assertTrue(response['isError'])
        self.assertIn('denied by policy',
                      response['content'][0]['text'])

    def test_non_git_project_is_refused(self):
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        response = self.write('tasks/extra.yml')
        self.assertTrue(response['isError'])
        self.assertIn('not a git repository',
                      response['content'][0]['text'])


class InventorySpecTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-spec-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       check=True)
        self.inventory_dir = os.path.join(self.tmpdir, 'ansible',
                                          'inventory')
        os.makedirs(self.inventory_dir)
        self.commits = []

        def _record_commit(instance, message=None):
            self.commits.append(message)

        self.spec_policy = {'enabled': True, 'max_version': 3,
                            'allow_io': False}
        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'commit': _record_commit,
            'config': type('Config', (), {'raw': {}})(),
            '_inventory_spec_config': lambda instance: self.spec_policy,
            '_spec_render_context': lambda instance: {},
        })()
        self.policy = McpPolicy(self.project)

    def call_spec(self, document, **arguments):
        from debops.ansible.inventory import AnsibleInventory as real
        from debops.inventoryspec import apply_spec as real_apply
        from debops.mcp.tools.files import tool_inventory_spec
        with mock.patch('debops.ansible.inventory.AnsibleInventory') \
                as inventory:
            inventory.return_value.path = self.inventory_dir
            inventory.return_value.apply_spec = (
                    lambda files, **kwargs:
                    real_apply(files, self.inventory_dir, **kwargs))
            inventory.return_value.existing_paths = (
                    lambda: real.existing_paths(inventory.return_value))
            return tool_inventory_spec(
                    self.project, self.policy,
                    dict({'spec': document}, **arguments))

    def result_text(self, result):
        content = result['content']
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]['type'], 'text')
        return content[0]['text']

    def test_applies_a_specification(self):
        result = self.call_spec('version: 0\n'
                                'files:\n'
                                '  hosts: "[web]\\nweb1\\n"\n')
        text = self.result_text(result)
        self.assertIn('# Inventory specification applied', text)
        self.assertIn('## Written (1)', text)
        self.assertIn('- hosts', text)
        with open(os.path.join(self.inventory_dir, 'hosts')) as fp:
            self.assertEqual(fp.read(), '[web]\nweb1\n')
        self.assertEqual(self.commits,
                         ['DebOps MCP: inventory_spec'])

    def test_dry_run_writes_nothing(self):
        result = self.call_spec('version: 0\n'
                                'files:\n'
                                '  hosts: "[web]\\n"\n', dry_run=True)
        text = self.result_text(result)
        self.assertIn('dry run', text)
        self.assertIn('## Planned writes (1)', text)
        self.assertFalse(os.path.exists(
                os.path.join(self.inventory_dir, 'hosts')))
        self.assertEqual(self.commits, [])

    def test_existing_files_are_skipped_without_overwrite(self):
        with open(os.path.join(self.inventory_dir, 'hosts'), 'w') as fp:
            fp.write('[web]\nweb1\n')
        result = self.call_spec('version: 0\n'
                                'files:\n'
                                '  hosts: "[db]\\n"\n')
        text = self.result_text(result)
        self.assertIn('## Skipped (already present) (1)', text)
        with open(os.path.join(self.inventory_dir, 'hosts')) as fp:
            self.assertEqual(fp.read(), '[web]\nweb1\n')
        # Nothing changed, nothing to commit
        self.assertEqual(self.commits, [])

    def test_overwrite_replaces(self):
        with open(os.path.join(self.inventory_dir, 'hosts'), 'w') as fp:
            fp.write('[web]\nweb1\n')
        result = self.call_spec('version: 0\n'
                                'files:\n'
                                '  hosts: "[db]\\n"\n', overwrite=True)
        self.assertIn('## Written (1)', self.result_text(result))
        with open(os.path.join(self.inventory_dir, 'hosts')) as fp:
            self.assertEqual(fp.read(), '[db]\n')

    def test_parse_errors_are_reported(self):
        result = self.call_spec('version: 0\nbogus: true\n')
        self.assertTrue(result['isError'])

    def test_disabled_by_the_project(self):
        self.spec_policy['enabled'] = False
        result = self.call_spec('version: 0\nfiles: {}\n')
        self.assertTrue(result['isError'])
        self.assertIn('disabled', result['content'][0]['text'])

    def test_version_cap(self):
        self.spec_policy['max_version'] = 1
        result = self.call_spec('version: 3\nfiles: {}\n')
        self.assertTrue(result['isError'])
        self.assertIn('version', result['content'][0]['text'])

    def test_spec_is_required(self):
        from debops.mcp.tools.files import tool_inventory_spec
        result = tool_inventory_spec(self.project, self.policy, {})
        self.assertTrue(result['isError'])

    def test_non_git_project_is_refused(self):
        shutil.rmtree(os.path.join(self.tmpdir, '.git'))
        result = self.call_spec('version: 0\n'
                                'files:\n'
                                '  hosts: "[web]\\n"\n')
        self.assertTrue(result['isError'])
        self.assertIn('not a git repository',
                      result['content'][0]['text'])


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
                          'collection_playbooks', 'collection_remove',
                          'collection_require', 'collection_roles',
                          'collection_sync', 'config_get',
                          'environment_list',
                          'environment_set', 'environment_unset',
                          'inventory_query',
                          'inventory_spec', 'keyring_fetch',
                          'playbook_info',
                          'playbooks', 'project_files', 'project_pull',
                          'project_push', 'project_read', 'project_status',
                          'role_argspec', 'role_defaults', 'role_files',
                          'role_handlers', 'role_info', 'role_tasks',
                          'role_templates', 'role_variables',
                          'secret_paths', 'view_delete', 'view_write'])

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

    def _tools_by_name(self, mcp_config):
        self.project.config.raw['project'] = {'mcp': mcp_config}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.roles_path)
        response = server.handle({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': 1,
                'method': 'tools/list', 'params': {}})
        return {tool['name']: tool
                for tool in response['result']['tools']}

    def test_confirm_gated_tools_carry_the_marker(self):
        tools = self._tools_by_name({'execute': 'all'})
        # The default confirm list gates 'run' and 'project_push'
        self.assertEqual(tools['run']['_meta'],
                         {'org.debops/confirm': True})
        self.assertEqual(tools['project_push']['_meta'],
                         {'org.debops/confirm': True})
        self.assertEqual(tools['environment_set']['_meta'],
                         {'org.debops/confirm': True})
        self.assertEqual(tools['environment_unset']['_meta'],
                         {'org.debops/confirm': True})
        self.assertIn('asks the user to confirm',
                      tools['run']['description'])
        # Ungated tools say nothing about confirmation
        self.assertNotIn('_meta', tools['check'])
        self.assertNotIn('confirm', tools['check']['description'])
        self.assertNotIn('_meta', tools['view_write'])

    def test_confirm_disabled_marks_nothing(self):
        tools = self._tools_by_name({'execute': 'all', 'confirm': []})
        for tool in tools.values():
            self.assertNotIn('_meta', tool, tool['name'])
            self.assertNotIn('asks the user to confirm',
                             tool['description'], tool['name'])

    def test_confirm_without_execution_is_consistent(self):
        # 'run' is not registered at all; its confirm entry is inert
        tools = self._tools_by_name({'execute': 'check',
                                     'confirm': ['run']})
        self.assertNotIn('run', tools)
        for tool in tools.values():
            self.assertNotIn('_meta', tool, tool['name'])

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

    def _write_playbook(self, name, text):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'w') as fp:
            fp.write(text)
        return path

    def test_playbook_summary_single_play(self):
        path = self._write_playbook('single.yml', '''---
- name: Only play
  hosts: all
''')
        self.assertEqual(playbook_summary(path),
                         'Only play (hosts: all)')

    def test_playbook_summary_multiple_plays(self):
        path = self._write_playbook('multi.yml', '''---
- name: First play
  hosts: all
- name: Second play
  hosts: web
- hosts: db
''')
        self.assertEqual(playbook_summary(path),
                         'First play (+2 plays) (hosts: all, web, db)')

    def test_playbook_summary_unnamed_plays(self):
        path = self._write_playbook('unnamed.yml', '''---
- hosts: all
- hosts: web
''')
        self.assertEqual(playbook_summary(path),
                         '2 plays (hosts: all, web)')

    def test_playbook_summary_import_only(self):
        path = self._write_playbook('imports.yml', '''---
- import_playbook: a.yml
- import_playbook: b.yml
''')
        self.assertEqual(playbook_summary(path), 'imports 2 playbooks')
        path = self._write_playbook('one-import.yml', '''---
- import_playbook: a.yml
''')
        self.assertEqual(playbook_summary(path), 'imports 1 playbook')

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
