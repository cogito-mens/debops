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

        self.server = McpServer(project=self.project,
                                policy=McpPolicy(self.project))
        register_project_tools(self.server,
                               roles_path=self.roles_path)

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
                         ['project_status', 'role_info', 'role_variables'])

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


if __name__ == '__main__':
    unittest.main()
