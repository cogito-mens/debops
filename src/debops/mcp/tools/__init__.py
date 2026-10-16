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
from .playbooks import (COLLECTION_PLAYBOOKS, tool_playbook_info,
                        tool_playbooks)
from .project import tool_project_status
from .roles import COLLECTION_ROLES, tool_role_info, tool_role_variables


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

    for name, description, schema, handler in tools:
        if policy is None or policy.is_tool_allowed(name):
            server.register_tool(name, description, schema, handler)
