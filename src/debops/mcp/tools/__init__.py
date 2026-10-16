# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The tools exposed by the DebOps MCP server.

Each tool is a closure over the project and the policy, registered with
the server at startup. Tool logic errors are reported in the result with
'isError', not as JSON-RPC errors, per the MCP specification.
"""

import time

from .collections import (COLLECTION_INDEX_TTL, galaxy_collection_list,
                          index_from_galaxy, tool_collection_playbook_info,
                          tool_collection_playbooks, tool_collection_role_info,
                          tool_collection_role_variables,
                          tool_collection_roles)
from .execute import tool_check, tool_run
from .files import (tool_config_get, tool_inventory_query,
                    tool_project_files, tool_project_read)
from .playbooks import (COLLECTION_PLAYBOOKS, tool_playbook_info,
                        tool_playbooks)
from .project import tool_project_status
from .roles import COLLECTION_ROLES, tool_role_info, tool_role_variables
from ..policy import EXECUTE_OPTIONS

# The schema shared by the execution tools, with options the project
# policy disabled filtered out, so agents never see what they cannot use
OPTION_DESCRIPTIONS = {
    'limit': 'Limit execution to hosts, --limit style',
    'tags': 'Only run tagged tasks, --tags style',
    'skip_tags': 'Skip tagged tasks, --skip-tags style',
}


def execution_schema(policy):
    allowed = (set(EXECUTE_OPTIONS) if policy is None
               else set(policy.execute_options))
    properties = {
        'playbook': {'type': 'string',
                     'description': 'Playbook target, e.g. '
                     '"service/nginx" or "site"'},
    }
    for option, description in OPTION_DESCRIPTIONS.items():
        if option in allowed:
            properties[option] = {'type': 'string',
                                  'description': description}
    return {'type': 'object', 'properties': properties,
            'required': ['playbook']}


def register_project_tools(server, roles_path=COLLECTION_ROLES,
                           playbooks_path=COLLECTION_PLAYBOOKS,
                           collection_index_provider=None):
    """Register the built-in tools, honoring the server policy."""
    project = server.project
    policy = server.policy

    if collection_index_provider is None:
        cache = {'timestamp': 0.0, 'index': None}

        def collection_index_provider():
            if (cache['index'] is None
                    or time.time() - cache['timestamp']
                    > COLLECTION_INDEX_TTL):
                cache['index'] = index_from_galaxy(
                        galaxy_collection_list(project))
                cache['timestamp'] = time.time()
            return cache['index']

    tools = [
        ('project_status',
         'Report the status of the DebOps project: type, current '
         'infrastructure view, inventory and secrets presence, encryption.',
         {'type': 'object', 'properties': {}},
         lambda arguments: tool_project_status(project, policy, arguments)),
        ('project_files',
         'List the files of the DebOps project, minus denied paths '
         '(secrets, key material, virtual environments). Use this to '
         'see the project structure before reading specific files.',
         {'type': 'object',
          'properties': {'path': {'type': 'string',
                                  'description': 'Subdirectory to list, '
                                  'relative to the project directory '
                                  '(default: the whole project)'},
                         'filter': {'type': 'string',
                                    'description': 'Only include paths '
                                    'containing this string'}}},
         lambda arguments: tool_project_files(project, policy, arguments)),
        ('project_read',
         'Read a file of the DebOps project (inventory, configuration, '
         'playbooks, ...), minus denied paths (secrets, key material, '
         'virtual environments). Text files only, size-limited.',
         {'type': 'object',
          'properties': {'path': {'type': 'string',
                                  'description': 'File to read, relative '
                                  'to the project directory, e.g. '
                                  '"ansible/inventory/group_vars/all/'
                                  'nginx.yml"'}},
          'required': ['path']},
         lambda arguments: tool_project_read(project, policy, arguments)),
        ('config_get',
         'Return the value of a DebOps configuration option, like '
         '"debops config get" does; the key is a dot-separated path in '
         'the configuration tree.',
         {'type': 'object',
          'properties': {'key': {'type': 'string',
                                 'description': 'Configuration key, e.g. '
                                 '"project.default_view"'}},
          'required': ['key']},
         lambda arguments: tool_config_get(project, arguments)),
        ('inventory_query',
         'Inspect the computed Ansible inventory with '
         '"ansible-inventory": "graph" shows the group tree, "list" '
         'dumps all host variables (can be large), "host" shows one '
         'host\'s variables.',
         {'type': 'object',
          'properties': {'action': {'type': 'string',
                                    'enum': ['graph', 'list', 'host'],
                                    'description': 'What to inspect '
                                    '(default: "graph")'},
                         'host': {'type': 'string',
                                  'description': 'Host name, required '
                                  'for the "host" action'}},
          'required': []},
         lambda arguments: tool_inventory_query(project, arguments)),
        ('role_info',
         'Describe a DebOps role: purpose, tags, dependencies, and which '
         'parts (defaults, tasks, templates) it provides.',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'Role name, with or '
                                  'without the "debops." prefix'}},
          'required': ['name']},
         lambda arguments: tool_role_info(roles_path, arguments)),
        ('role_variables',
         'List the default inventory variables of a DebOps role, with '
         'their documentation and default values. Use this to learn how '
         'to configure a role through the Ansible inventory. Survey a role '
         'with the "brief" or "names" detail level, then use "filter" '
         'with the "full" detail level to read the complete documentation '
         'of specific variables.',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'Role name, with or '
                                  'without the "debops." prefix'},
                         'filter': {'type': 'string',
                                    'description': 'Only include variables '
                                    'whose name contains this string'},
                         'detail': {'type': 'string',
                                    'enum': ['names', 'brief', 'full'],
                                    'description': 'Level of detail: '
                                    '"names" lists variable names only, '
                                    '"brief" (default) gives a one-line '
                                    'summary and a truncated default per '
                                    'variable, "full" gives the complete '
                                    'documentation'}},
          'required': ['name']},
         lambda arguments: tool_role_variables(roles_path, arguments)),
        ('playbooks',
         'List the playbook targets which "run" and "check" can execute '
         'in this project: the project\'s own playbooks, the bundled '
         'DebOps collection playbooks, and playbook sets from the view '
         'configuration.',
         {'type': 'object',
          'properties': {'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'playbooks whose path contains '
                                    'this string'}}},
         lambda arguments: tool_playbooks(project, policy, playbooks_path,
                                          arguments)),
        ('playbook_info',
         'Describe a project or bundled DebOps playbook: its plays, the '
         'inventory groups it targets, the roles it applies, and the '
         'playbooks it imports. Use this to learn what a playbook does '
         'before running it.',
         {'type': 'object',
          'properties': {'playbook': {'type': 'string',
                                      'description': 'Playbook target, '
                                      'e.g. "service/nginx" or "site"'}},
          'required': ['playbook']},
         lambda arguments: tool_playbook_info(project, policy,
                                              playbooks_path, arguments)),
        ('collection_playbooks',
         'List the playbooks provided by an Ansible Collection visible '
         'to this project.',
         {'type': 'object',
          'properties': {'collection': {'type': 'string',
                                        'description': 'Collection, e.g. '
                                        '"community.sops"'},
                         'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'playbooks whose path contains '
                                    'this string'}},
          'required': ['collection']},
         lambda arguments: tool_collection_playbooks(
             collection_index_provider, arguments)),
        ('collection_playbook_info',
         'Describe a playbook from an Ansible Collection: its plays, '
         'inventory groups, roles and imported playbooks.',
         {'type': 'object',
          'properties': {'collection': {'type': 'string',
                                        'description': 'Collection, e.g. '
                                        '"community.sops"'},
                         'playbook': {'type': 'string',
                                      'description': 'Playbook name '
                                      'inside the collection, e.g. '
                                      '"install"'}},
          'required': ['collection', 'playbook']},
         lambda arguments: tool_collection_playbook_info(
             collection_index_provider, arguments)),
        ('collection_roles',
         'List the Ansible Collections visible to this project and the '
         'roles they contain. Use this to find roles outside of the '
         'bundled DebOps collection.',
         {'type': 'object',
          'properties': {'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'collections whose name contains '
                                    'this string'}}},
         lambda arguments: tool_collection_roles(
             collection_index_provider, arguments)),
        ('collection_role_info',
         'Describe a role from any Ansible Collection visible to this '
         'project: purpose, argument specs, provided parts, and where it '
         'was resolved from.',
         {'type': 'object',
          'properties': {'role': {'type': 'string',
                                  'description': 'Fully qualified role '
                                  'name, e.g. "grafana.grafana.grafana"'}},
          'required': ['role']},
         lambda arguments: tool_collection_role_info(
             collection_index_provider, arguments)),
        ('collection_role_variables',
         'List the variables of a role from any Ansible Collection '
         'visible to this project, from its argument specs when present '
         'or its defaults otherwise. Survey with the "brief" or "names" '
         'detail level, then use "filter" with "full" for details.',
         {'type': 'object',
          'properties': {'role': {'type': 'string',
                                  'description': 'Fully qualified role '
                                  'name, e.g. "grafana.grafana.grafana"'},
                         'filter': {'type': 'string',
                                    'description': 'Only include variables '
                                    'whose name contains this string'},
                         'detail': {'type': 'string',
                                    'enum': ['names', 'brief', 'full'],
                                    'description': 'Level of detail: '
                                    '"names" lists variable names only, '
                                    '"brief" (default) gives a one-line '
                                    'summary and a truncated default per '
                                    'variable, "full" gives the complete '
                                    'documentation'}},
          'required': ['role']},
         lambda arguments: tool_collection_role_variables(
             collection_index_provider, arguments)),
    ]

    # Playbook execution is gated: 'check' mode by default, changes only
    # with 'project.mcp.execute: all'
    execute_mode = policy.execute if policy is not None else 'check'
    if execute_mode in ('check', 'all'):
        tools.append((
            'check',
            'Run an Ansible playbook in check mode (no changes are '
            'made), reporting what would change. Facts are gathered '
            'live from the managed hosts. Accepts a playbook target '
            '(see the playbooks tool) and optional limit/tags.',
            execution_schema(policy),
            lambda arguments, context: tool_check(project, policy,
                                                  arguments, context),
            True))
    if execute_mode == 'all':
        tools.append((
            'run',
            'Run an Ansible playbook, changing the managed hosts. Use '
            'the "check" tool first to preview the changes. Accepts a '
            'playbook target (see the playbooks tool) and optional '
            'limit/tags.',
            execution_schema(policy),
            lambda arguments, context: tool_run(project, policy,
                                                arguments, context),
            True))

    for entry in tools:
        name, description, schema, handler = entry[:4]
        needs_context = entry[4] if len(entry) > 4 else False
        if policy is None or policy.is_tool_allowed(name):
            server.register_tool(name, description, schema, handler,
                                 needs_context=needs_context)
