# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The tools exposed by the DebOps MCP server.

Each tool is a closure over the project and the policy, registered with
the server at startup. Tool logic errors are reported in the result with
'isError', not as JSON-RPC errors, per the MCP specification.
"""

from .collections import (docs_collection_provider,
                          make_galaxy_index_provider, restrict_content,
                          tool_collection_playbooks,
                          tool_collection_roles)
from .custom import discover_custom_tools, make_custom_tool
from .environment import (tool_environment_list, tool_environment_set,
                          tool_environment_unset)
from .execute import tool_check, tool_run
from .executions import tool_execution_list, tool_execution_output
from .files import (tool_config_get, tool_inventory_query,
                    tool_inventory_spec, tool_project_files,
                    tool_project_read, tool_file_delete, tool_file_write)
from .galaxy import (tool_collection_remove, tool_collection_require,
                     tool_collection_sync)
from .keyring import tool_keyring_fetch
from .overrides import tool_overrides_write
from .hosts import tool_ansible_facts, tool_ansible_ping
from .playbooks import (COLLECTION_PLAYBOOKS, tool_playbook_info,
                        tool_playbooks)
from .common import (ProjectLockedError, ProjectResourceError,
                     shared_project)
from .common import error_result as _error_result
from .project import tool_project_status
from .roles import (COLLECTION_ROLES, project_role_paths, tool_role_argspec,
                    tool_role_defaults, tool_role_files, tool_role_handlers,
                    tool_role_info, tool_role_tasks, tool_role_templates,
                    tool_role_variables)
from .secrets import tool_secret_paths
from .sync import make_sync_tool
from .views import (NO_ACTIVE_VIEW, tool_view_create, tool_view_list,
                    tool_view_switch)
from .doc import PLUGIN_TYPES, tool_ansible_doc
from ..policy import EXECUTE_OPTIONS
from ..server import Tool

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
        'playbook': {'oneOf': [{'type': 'string'},
                               {'type': 'array',
                                'items': {'type': 'string'},
                                'minItems': 1}],
                     'description': 'Playbook target or list of '
                     'targets (run in order), e.g. "service/nginx" or '
                     '["service/nginx", "site"]'},
    }
    for option, description in OPTION_DESCRIPTIONS.items():
        if option in allowed:
            properties[option] = {'type': 'string',
                                  'description': description}
    if policy is None or policy.execute_debug:
        properties['debug'] = {
            'type': 'boolean',
            'description': 'Run with Ansible debugging (-vv, '
                           'ANSIBLE_DEBUG, the remote module scripts '
                           'kept on the hosts). Prints task arguments '
                           '— secrets included — into the answer; '
                           'the project enables it with '
                           'project.mcp.execute_debug'}
    return {'type': 'object', 'properties': properties,
            'required': ['playbook']}


# The document tools serve rendered text for humans by default, or
# one machine document ('format': 'json', 'yaml' or 'toml')
FORMAT_NOTE = (' Returns text; pass "format": "json" (or "yaml", '
               '"toml") for the parsed document.')
FORMAT_PROPERTY = {'type': 'string', 'enum': ['text', 'json', 'yaml',
                                              'toml'],
                   'description': 'Render as text (default) or as one '
                                  'document (json, yaml, toml)'}


# Tools which read the project directory; they run under the shared
# project lock so a writer (a run, a sync, secrets) cannot interleave
READ_LOCKED_TOOLS = frozenset((
    'project.status', 'project.files', 'project.read', 'config.get',
    'environment.list', 'inventory.query', 'playbook.list', 'playbook.info',
    'view.list', 'role.info',
    'role.defaults', 'role.vars', 'role.argspec', 'role.tasks',
    'role.handlers', 'role.templates', 'role.files', 'collection.roles',
    'collection.playbooks',
))

# The output schemas of the tools which return structuredContent;
# keyed by name, like the annotations
# The per-host play recap counters of a check/run, mapped by host name
_RECAP_SCHEMA = {'type': 'object',
                 'additionalProperties': {
                     'type': 'object',
                     'additionalProperties': {'type': 'integer'}}}

TOOL_OUTPUT_SCHEMAS = {
    'playbook.list': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'target': {'type': 'string'},
                       'section': {'type': 'string'},
                       'summary': {'type': 'string'}},
        'required': ['target', 'section', 'summary']}},
    'collection.roles': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'collection': {'type': 'string'},
                       'version': {'type': 'string'},
                       'roles': {'type': 'array',
                                 'items': {'type': 'string'}}},
        'required': ['collection', 'version', 'roles']}},
    'collection.playbooks': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'collection': {'type': 'string'},
                       'target': {'type': 'string'},
                       'summary': {'type': 'string'}},
        'required': ['collection', 'target', 'summary']}},
    'secret.paths': {'type': 'array', 'items': {'type': 'string'}},
    'view.list': {'type': 'object', 'properties': {
        'active': {'type': ['string', 'null']},
        'locked': {'type': 'boolean'},
        'views': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
                'active': {'type': 'boolean'},
                'present': {'type': 'boolean'}},
            'required': ['name', 'active', 'present']}}},
        'required': ['active', 'locked', 'views']},
    'project.status': {'type': 'object', 'properties': {
        'name': {'type': 'string'}, 'path': {'type': 'string'},
        'type': {'type': 'string'}, 'view': {'type': ['string', 'null']},
        'views': {'type': 'array', 'items': {'type': 'string'}},
        'viewLocked': {'type': 'boolean'},
        'viewRoot': {'type': ['string', 'null']},
        'viewRootPresent': {'type': 'boolean'},
        'inventory': {'type': 'string'}, 'secrets': {'type': 'string'},
        'encryption': {'type': 'string'}},
        'required': ['name', 'path', 'type', 'view', 'views',
                     'viewLocked', 'viewRoot', 'viewRootPresent',
                     'inventory', 'secrets', 'encryption']},
    'project.files': {'type': 'array', 'items': {'type': 'string'}},
    'config.get': {'type': 'object', 'properties': {
        'key': {'type': 'string'}, 'value': {}},
        'required': ['key', 'value']},
    'environment.list': {'type': 'object', 'properties': {
        'tracked': {'type': 'object',
                    'additionalProperties': {'type': 'string'}},
        'tokens': {'type': 'array', 'items': {'type': 'string'}},
        'project': {'type': 'array', 'items': {'type': 'string'}},
        'process': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['tracked', 'tokens', 'project', 'process']},
    'playbook.info': {'type': 'object', 'properties': {
        'playbook': {'type': 'string'},
        'plays': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'name': {'type': 'string'},
                           'hosts': {'type': 'array',
                                     'items': {'type': 'string'}},
                           'become': {'type': 'boolean'},
                           'vars': {'type': 'array',
                                    'items': {'type': 'string'}}},
            'required': ['name', 'hosts', 'become']}},
        'roles': {'type': 'array', 'items': {'type': 'string'}},
        'imports': {'type': 'array', 'items': {'type': 'string'}},
        'groups': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['playbook', 'plays', 'roles', 'imports',
                     'groups']},
    'role.info': {'type': 'object', 'properties': {
        'name': {'type': 'string'}, 'path': {'type': 'string'},
        'description': {'type': 'string'}, 'author': {'type': 'string'},
        'license': {'type': 'string'},
        'minAnsible': {'type': 'string'},
        'platforms': {'type': 'array', 'items': {'type': 'string'}},
        'tags': {'type': 'array', 'items': {'type': 'string'}},
        'dependencies': {'type': 'array', 'items': {'type': 'string'}},
        'counts': {'type': 'object', 'properties': {
            'defaults': {'type': 'integer'},
            'variables': {'type': 'integer'},
            'tasks': {'type': 'integer'},
            'handlers': {'type': 'integer'},
            'templates': {'type': 'integer'},
            'files': {'type': 'integer'}},
            'required': ['defaults', 'variables', 'tasks', 'handlers',
                         'templates', 'files']},
        'readme': {'type': 'boolean'}, 'copyright': {'type': 'boolean'},
        'docsUrl': {'type': 'string'}},
        'required': ['name', 'counts']},
    'inventory.spec': {'type': 'object', 'properties': dict(
            [(key, {'type': 'array', 'items': {'type': 'string'}})
             for key in ('written', 'removed', 'skipped', 'kept',
                         'planned', 'planned_removed')]),
        'required': ['written', 'removed', 'skipped', 'kept',
                     'planned', 'planned_removed']},
    'keyring.fetch': {'type': 'object', 'properties': {
        'fingerprint': {'type': 'string'},
        'fingerprintDisplay': {'type': 'string'},
        'userIds': {'type': 'array', 'items': {'type': 'string'}},
        'file': {'type': 'string'}, 'source': {'type': 'string'}},
        'required': ['fingerprint', 'fingerprintDisplay', 'userIds',
                     'file', 'source']},
    'project.pull': {'type': 'object', 'properties': {
        'action': {'type': 'string'},
        'changed': {'type': 'boolean'},
        'commits': {'type': 'array', 'items': {'type': 'string'}},
        'files_changed': {'type': 'integer'},
        'before': {'type': 'string'}, 'after': {'type': 'string'},
        'refreshed': {'type': 'boolean'},
        'refresh_error': {'type': 'string'}},
        'required': ['action']},
    'project.push': {'type': 'object', 'properties': {
        'action': {'type': 'string'}, 'pushed': {'type': 'boolean'},
        'commits': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['action', 'pushed']},
    'ansible.ping': {'type': 'object',
                     'additionalProperties': {'type': 'string'}},
    'check': {'type': 'object', 'properties': {
        'mode': {'type': 'string'},
        'playbooks': {'type': 'array', 'items': {'type': 'string'}},
        'hosts': {'type': 'array', 'items': {'type': 'string'}},
        'exitCode': {'type': 'integer'}, 'status': {'type': 'string'},
        'executionId': {'type': 'integer'}, 'log': {'type': 'string'},
        'recap': _RECAP_SCHEMA},
        'required': ['mode', 'playbooks', 'hosts', 'exitCode',
                     'status', 'executionId']},
    'run': {'type': 'object', 'properties': {
        'mode': {'type': 'string'},
        'playbooks': {'type': 'array', 'items': {'type': 'string'}},
        'hosts': {'type': 'array', 'items': {'type': 'string'}},
        'exitCode': {'type': 'integer'}, 'status': {'type': 'string'},
        'executionId': {'type': 'integer'}, 'log': {'type': 'string'},
        'recap': _RECAP_SCHEMA},
        'required': ['mode', 'playbooks', 'hosts', 'exitCode',
                     'status', 'executionId']},
    'execution.list': {'type': 'array', 'items': {
        'type': 'object', 'properties': {
            'id': {'type': 'integer'}, 'log': {'type': 'string'},
            'mode': {'type': 'string'},
            'playbooks': {'type': 'array', 'items': {'type': 'string'}},
            'hosts': {'type': 'array', 'items': {'type': 'string'}},
            'startedAt': {'type': 'string'},
            'finishedAt': {'type': 'string'},
            'exitCode': {'type': 'integer'},
            'status': {'type': 'string'},
            'logBytes': {'type': 'integer'}},
        'required': ['id', 'log']}},
    'execution.output': {'type': 'object', 'properties': {
        'executionId': {'type': 'integer'}, 'query': {'type': 'string'},
        'matched': {'type': 'integer'}, 'returned': {'type': 'integer'},
        'truncated': {'type': 'boolean'},
        'degraded': {'type': 'boolean'}},
        'required': ['executionId', 'query', 'matched', 'returned',
                     'truncated']},
}


# Behavior hints for client UIs and auto-approval policies (the
# ToolAnnotations schema); only values which differ from the defaults
# are listed. The defaults are: readOnlyHint false, destructiveHint
# true, idempotentHint false, openWorldHint true.
_READ_ONLY = {'readOnlyHint': True, 'openWorldHint': False}

TOOL_ANNOTATIONS = {
    # Pure reads of project or package content
    'project.status': _READ_ONLY,
    'project.files': _READ_ONLY,
    'environment.list': _READ_ONLY,
    'project.read': _READ_ONLY,
    'config.get': _READ_ONLY,
    'inventory.query': _READ_ONLY,
    'playbook.list': _READ_ONLY,
    'playbook.info': _READ_ONLY,
    'collection.roles': _READ_ONLY,
    'collection.playbooks': _READ_ONLY,
    'role.info': _READ_ONLY,
    'role.vars': _READ_ONLY,
    'role.argspec': _READ_ONLY,
    'role.defaults': _READ_ONLY,
    'role.tasks': _READ_ONLY,
    'role.handlers': _READ_ONLY,
    'role.templates': _READ_ONLY,
    'role.files': _READ_ONLY,
    # Host queries reach out over SSH but change nothing
    'ansible.ping': {'readOnlyHint': True},
    'ansible.facts': {'readOnlyHint': True},
    # The run logs are read from the cache; the project is untouched
    'execution.list': _READ_ONLY,
    'execution.output': _READ_ONLY,
    # Plugin documentation is a local read
    'ansible.doc': _READ_ONLY,
    # Secrets are mounted and unmounted; the project may be committed
    'secret.paths': {'destructiveHint': False, 'idempotentHint': True,
                     'openWorldHint': False},
    # Writes change the project (recoverable via the auto-commit)
    'file.write': {'destructiveHint': True, 'idempotentHint': True,
                   'openWorldHint': False},
    # Deleting an absent file is an error, so not idempotent
    'file.delete': {'destructiveHint': True, 'idempotentHint': False,
                    'openWorldHint': False},
    # A specification can remove inventory paths
    'inventory.spec': {'destructiveHint': True, 'idempotentHint': True,
                       'openWorldHint': False},
    # The view is server-global state; listing it changes nothing, and
    # a switch is visible to every connected client
    'view.list': _READ_ONLY,
    'view.switch': {'destructiveHint': False, 'idempotentHint': False,
                    'openWorldHint': False},
    'view.create': {'destructiveHint': False, 'idempotentHint': False,
                    'openWorldHint': False},
    # Fetches over the network and adds a trust anchor
    'keyring.fetch': {'destructiveHint': True, 'idempotentHint': True,
                      'openWorldHint': True},
    # Injects content into role executions on the managed hosts
    'overrides.write': {'destructiveHint': True, 'idempotentHint': True,
                        'openWorldHint': False},
    # Environment writes land in files
    'environment.set': {'destructiveHint': True, 'idempotentHint': True,
                        'openWorldHint': False},
    'environment.unset': {'destructiveHint': True, 'idempotentHint': True,
                          'openWorldHint': False},
    # Collection management fetches code over the network
    'collection.require': {'destructiveHint': False,
                           'idempotentHint': True, 'openWorldHint': True},
    'collection.remove': {'destructiveHint': True, 'idempotentHint': True,
                          'openWorldHint': False},
    'collection.sync': {'destructiveHint': False, 'idempotentHint': True,
                        'openWorldHint': True},
    # A check run changes nothing on the managed hosts, but the local
    # auto-commit may commit the project
    'check': {'destructiveHint': False, 'idempotentHint': True},
    'run': {'destructiveHint': True, 'idempotentHint': True},
    'project.pull': {'destructiveHint': False, 'idempotentHint': True},
    'project.push': {'destructiveHint': True, 'idempotentHint': True},
}


def _wrap_shared_lock(project, policy, handler):
    """Run a read tool under the shared project lock."""
    def locked(arguments, *rest):
        with shared_project(project, policy):
            return handler(arguments, *rest)
    return locked


def _wrap_resource_errors(handler):
    """Turn a pre-start refusal (disk full, a locked project) into a
    tool error: these are expected conditions, not internal faults."""
    def wrapped(arguments, *rest):
        try:
            return handler(arguments, *rest)
        except (ProjectResourceError, ProjectLockedError) as errmsg:
            return _error_result(str(errmsg))
    return wrapped


def _documentation_entries(server, policy, project, roles_path,
                           playbooks_path, collection_index_provider):
    """The documentation tool entries: the bundled DebOps content and
    the collections the index provides.

    With a project its own roles and playbooks resolve first; without
    one (the documentation mode) only the shipped content is served.
    """
    bundled_content = policy is None or policy.bundled_content
    # The roles the document tools resolve; ansible.doc searches the
    # same paths, so the tools agree
    roles_paths = []
    if project is not None:
        roles_paths = project_role_paths(project)
    if bundled_content:
        roles_paths.append(roles_path)
    entries = [
        ('playbook.list',
         'List the playbook targets which "run" and "check" can execute '
         'in this project: the project\'s own playbooks, the bundled '
         'DebOps collection playbooks, and playbook sets from the view '
         'configuration.',
         {'type': 'object',
          'properties': {'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'playbooks whose path contains '
                                    'this string'}}},
         lambda arguments: tool_playbooks(
             project, policy,
             playbooks_path if bundled_content else None, arguments)),
        ('playbook.info',
         'Describe a project or bundled DebOps playbook: its plays, the '
         'inventory groups it targets, the roles it applies, and the '
         'playbooks it imports. Use this to learn what a playbook does '
         'before running it. With "detail: full", also shows the plays\' '
         'variables and task listings.',
         {'type': 'object',
          'properties': {'playbook': {'type': 'string',
                                      'description': 'Playbook target, '
                                      'e.g. "service/nginx" or "site"'},
                         'detail': {'type': 'string',
                                    'enum': ['brief', 'full'],
                                    'description': '"brief" (default) '
                                    'summarizes the plays, "full" adds '
                                    'their variables and task listings'}},
          'required': ['playbook']},
         lambda arguments: tool_playbook_info(
             project, policy,
             playbooks_path if bundled_content else None, arguments,
             index_provider=collection_index_provider)),
        ('collection.playbooks',
         'List the playbooks of Ansible Collections visible to this '
         'project, grouped by collection; with "collection", only that '
         'one. Use this to find playbooks outside of the bundled '
         'DebOps collection.',
         {'type': 'object',
          'properties': {'collection': {'type': 'string',
                                        'description': 'Collection, e.g. '
                                        '"community.sops" (default: all '
                                        'collections)'},
                         'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'playbooks whose target '
                                    '("namespace.collection/path") '
                                    'contains this string'}},
          'required': []},
         lambda arguments: tool_collection_playbooks(
             collection_index_provider, arguments)),
        ('collection.roles',
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
        ('ansible.doc',
         'Look up Ansible plugin documentation with ansible-doc: '
         'modules, lookups, filters, tests and the other plugin types '
         '(the "type" argument), the keywords of the playbook language '
         '("type": "keyword"), and role argument specifications '
         '("type": "role", with "entry_point" when a role declares '
         'several). With "list": true, enumerate the plugins of a '
         'type, narrowed by a namespace or collection name in '
         '"plugin". With "snippet": true, the paste-ready playbook '
         'snippet.' + FORMAT_NOTE,
         {'type': 'object',
          'properties': {
              'plugin': {'type': 'string',
                         'description': 'Plugin name, short or fully '
                         'qualified (namespace.collection.name)'},
              'type': {'type': 'string', 'enum': list(PLUGIN_TYPES),
                       'description': 'The plugin type (default: '
                       '"module")'},
              'list': {'type': 'boolean',
                       'description': 'Enumerate the plugins of the '
                       'type instead of showing one (default: false)'},
              'snippet': {'type': 'boolean',
                          'description': 'The paste-ready playbook '
                          'snippet instead of the documentation '
                          '(inventory, lookup and module plugins)'},
              'entry_point': {'type': 'string',
                              'description': 'The entry point of the '
                              'role (with "type": "role" only)'},
              'format': FORMAT_PROPERTY},
          'required': []},
         lambda arguments: tool_ansible_doc(
             project, policy, arguments, roles_paths=roles_paths)),
    ]

    # Role tools search the project's own roles first; the bundled
    # DebOps collection is included unless the policy disables it. With
    # neither, there are no roles to serve and the tools are not
    # registered at all
    if roles_paths:
        entries.extend([
            ('role.info',
             'Describe a role of this project or the bundled DebOps '
             'collection: purpose, tags, dependencies, and which parts '
             '(defaults, tasks, templates) it provides.',
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'}},
              'required': ['name']},
             lambda arguments: tool_role_info(
                 roles_paths, arguments,
                 index_provider=collection_index_provider,
                 bundled_path=roles_path if bundled_content else None,
                 docs_version=server.version)),
            ('role.vars',
             'List the variables of a role of this project or the '
             'bundled DebOps collection, from its vars/ directory — '
             'higher-precedence, internal state, not meant to be '
             'overridden from the inventory. For the overridable '
             'defaults, use the role.defaults tool.' + FORMAT_NOTE,
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'},
                             'filter': {'type': 'string',
                                        'description': 'Only include '
                                        'variables whose name contains '
                                        'this string'},
                             'detail': {'type': 'string',
                                        'enum': ['names', 'brief', 'full'],
                                        'description': 'Level of detail: '
                                        '"names" lists variable names '
                                        'only, "brief" (default) gives a '
                                        'one-line summary and a truncated '
                                        'default per variable, "full" '
                                        'gives the complete documentation'},
                             'format': FORMAT_PROPERTY},
              'required': ['name']},
             lambda arguments: tool_role_variables(
                               roles_paths, arguments,
                               index_provider=collection_index_provider)),
            ('role.argspec',
             'Render the argument specification of a role '
             '(meta/argument_specs.yml): its options with types, '
             'required flags, choices and defaults. Use this for roles '
             'which document their interface that way.',
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, short '
                                      'or fully qualified '
                                      '(namespace.collection.role)'},
                             'filter': {'type': 'string',
                                        'description': 'Only include '
                                        'options whose name contains '
                                        'this string'},
                             'detail': {'type': 'string',
                                        'enum': ['names', 'brief', 'full'],
                                        'description': 'Level of detail: '
                                        '"names" lists option names '
                                        'only, "brief" (default) gives a '
                                        'one-line summary, "full" the '
                                        'complete specification'}},
              'required': ['name']},
             lambda arguments: tool_role_argspec(
                               roles_paths, arguments,
                               index_provider=collection_index_provider)),
            ('role.defaults',
             'List the default inventory variables of a role of this '
             'project or the bundled DebOps collection, from its '
             'defaults/ directory, with their documentation. Use this to '
             'learn how to configure a role through the Ansible '
             'inventory. Survey a role with the "brief" or "names" '
             'detail level, then use "filter" with the "full" detail '
             'level to read the complete documentation of specific '
             'variables.' + FORMAT_NOTE,
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'},
                             'filter': {'type': 'string',
                                        'description': 'Only include '
                                        'variables whose name contains '
                                        'this string'},
                             'detail': {'type': 'string',
                                        'enum': ['names', 'brief', 'full'],
                                        'description': 'Level of detail: '
                                        '"names" lists variable names '
                                        'only, "brief" (default) gives a '
                                        'one-line summary and a truncated '
                                        'default per variable, "full" '
                                        'gives the complete documentation'},
                             'format': FORMAT_PROPERTY},
              'required': ['name']},
             lambda arguments: tool_role_defaults(
                 roles_paths, arguments,
                 index_provider=collection_index_provider,
                 bundled_path=roles_path if bundled_content else None,
                 docs_version=server.version)),
            ('role.tasks',
             'Inspect the tasks of a role: its task files, and per file '
             'the tasks with their modules, conditions and tags. Use '
             'this to understand what a role does before applying it.'
             + FORMAT_NOTE,
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'},
                             'file': {'type': 'string',
                                      'description': 'Task file to read, '
                                      'e.g. "main" (default: overview of '
                                      'all task files)'},
                             'filter': {'type': 'string',
                                        'description': 'Only include '
                                        'tasks whose name contains this '
                                        'string'},
                             'detail': {'type': 'string',
                                        'enum': ['brief', 'full'],
                                        'description': '"brief" (default) '
                                        'shows name and module, "full" '
                                        'adds when/tags'},
                             'format': FORMAT_PROPERTY},
              'required': ['name']},
             lambda arguments: tool_role_tasks(
                 roles_paths, arguments,
                 index_provider=collection_index_provider)),
            ('role.handlers',
             'Inspect the handlers of a role: its handler files, and '
             'per file the handlers with their modules, listen topics '
             'and conditions.' + FORMAT_NOTE,
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'},
                             'file': {'type': 'string',
                                      'description': 'Handler file to '
                                      'read, e.g. "main" (default: '
                                      'overview of all handler files)'},
                             'filter': {'type': 'string',
                                        'description': 'Only include '
                                        'handlers whose name contains '
                                        'this string'},
                             'detail': {'type': 'string',
                                        'enum': ['brief', 'full'],
                                        'description': '"brief" '
                                        '(default) shows name, module '
                                        'and listen topic, "full" adds '
                                        'when/tags'},
                             'format': FORMAT_PROPERTY},
              'required': ['name']},
             lambda arguments: tool_role_handlers(
                 roles_paths, arguments,
                 index_provider=collection_index_provider)),
            ('role.templates',
             'List the Jinja2 templates of a role, or print one of '
             'them.',
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'},
                             'path': {'type': 'string',
                                      'description': 'Template to print, '
                                      'relative to the role\'s '
                                      'templates/ directory'}},
              'required': ['name']},
             lambda arguments: tool_role_templates(
                 roles_paths, arguments,
                 index_provider=collection_index_provider)),
            ('role.files',
             'List the static files of a role, or print one of them.',
             {'type': 'object',
              'properties': {'name': {'type': 'string',
                                      'description': 'Role name, with or '
                                      'without the "debops." prefix'},
                             'path': {'type': 'string',
                                      'description': 'File to print, '
                                      'relative to the role\'s files/ '
                                      'directory'}},
              'required': ['name']},
             lambda arguments: tool_role_files(
                 roles_paths, arguments,
                 index_provider=collection_index_provider)),
        ])
    return entries


# Hidden aliases per canonical tool, answered quietly and never
# listed: the namespace-pattern guesses ('playbook.run' lands on the
# verb 'run'), and the semantic redirects — a model which asks for the
# 'variables' of a role wants the settable ones, so it lands on
# role.defaults; the vars/ tree answers to role.vars only
TOOL_ALIASES = {
    'run': ['playbook.run'],
    'check': ['playbook.check'],
    'role.defaults': ['role.variables', 'variables'],
    'role.tasks': ['tasks'],
    'role.templates': ['templates'],
}


def _apply_aliases(server):
    """Register the hidden tool aliases on the server."""
    for canonical, aliases in TOOL_ALIASES.items():
        for alias in aliases:
            server.aliases[alias] = canonical


# The tools which cannot work without an active infrastructure view
VIEW_BOUND_TOOLS = frozenset((
    'file.write', 'file.delete', 'inventory.query', 'inventory.spec',
    'secret.paths', 'check', 'run', 'ansible.ping', 'ansible.facts',
    'execution.list', 'execution.output',
))


def _no_view_stub(entry):
    """Replace a view-bound handler with the no-view guidance.

    The surface stays stable — a client which cached tools/list keeps
    the same names — and the error teaches the recovery path.
    """
    return entry[:3] + (
            lambda arguments, context=None: _error_result(
                    NO_ACTIVE_VIEW),) + entry[4:]


def _survival_note():
    """The note about an execution which outlives its client.

    Every execution is detached from the server: the client does not
    get the answer when the server dies, but the run finishes and its
    log records what happened.
    """
    return (' A started execution survives the death of this server: '
            'it is detached from it, and its output lands in the run '
            'log. The answer is lost with the server.')


def _execution_stub_entry(name, policy, execute_mode):
    """A disabled execution tool's entry: the real input schema, and
    the handler answers with the policy path.

    A missing tool makes agents retry-loop over a name they saw
    referenced; a present-but-refused one teaches in one read.
    """
    if execute_mode == 'none':
        reason = 'playbook execution is disabled in this project'
    else:
        reason = ('execution is check-only here ("project.mcp.execute: '
                  'all" allows changing the hosts)')
    description = ('Ansible playbook execution ({}). Currently '
                   'disabled by the project policy.'.format(name))

    def stub(arguments, context=None):
        return _error_result('the "{}" tool is disabled by the project '
                             'policy: {}.'.format(name, reason))

    return (name, description, execution_schema(policy), stub,
            False, True, False)


def _register_tool(target, name, description, schema, handler,
                   output_schema=None, needs_context=False,
                   annotations=None, meta=None, self_confirms=False,
                   task_eligible=False):
    target[name] = Tool(name, description, schema, handler,
                        output_schema, needs_context, annotations,
                        meta, self_confirms, task_eligible)


def _register_entries(target, entries, policy, confirm=(), project=None):
    """Register the entries, honoring the policy and the safety wraps."""
    for entry in entries:
        name, description, schema, handler = entry[:4]
        needs_context = entry[4] if len(entry) > 4 else False
        self_confirms = entry[5] if len(entry) > 5 else False
        task_eligible = entry[6] if len(entry) > 6 else False
        annotations = (entry[7] if len(entry) > 7
                       else TOOL_ANNOTATIONS.get(name))
        handler = _wrap_resource_errors(handler)
        if project is not None and name in READ_LOCKED_TOOLS:
            handler = _wrap_shared_lock(project, policy, handler)
        if policy is None or policy.is_tool_allowed(name):
            meta = None
            if name in confirm:
                description += (' The server asks the user to confirm '
                                'before this runs.')
                meta = {'org.debops/confirm': True}
            _register_tool(target, name, description, schema, handler,
                           output_schema=TOOL_OUTPUT_SCHEMAS.get(name),
                           needs_context=needs_context,
                           annotations=annotations,
                           meta=meta, self_confirms=self_confirms,
                           task_eligible=task_eligible)


def register_project_tools(server, roles_path=COLLECTION_ROLES,
                           playbooks_path=COLLECTION_PLAYBOOKS,
                           collection_index_provider=None,
                           enabled_check=None, registry=None):
    """Register the built-in tools, honoring the server policy.

    With 'registry', the tools are collected in that dictionary instead
    of the server's live registry, so that a reload can build the new
    surface before swapping it in whole.
    """
    project = server.project
    policy = server.policy
    target = registry if registry is not None else server.tools

    if collection_index_provider is None:
        collection_index_provider = make_galaxy_index_provider(project)

    # The content policy: which collection content the tools may serve
    bundled_content = policy is None or policy.bundled_content
    collection_index_provider = restrict_content(
            collection_index_provider, project, policy)

    tools = [
        ('project.status',
         'Report the status of the DebOps project: type, current '
         'infrastructure view, inventory and secrets presence, encryption.',
         {'type': 'object', 'properties': {},
          'additionalProperties': False},
         lambda arguments: tool_project_status(project, policy, arguments)),
        ('project.files',
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
        ('project.read',
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
        ('file.write',
         'Write a file in the current infrastructure view, in allowed '
         'locations only ("resources/" and "playbooks/" by default; the '
         'project.mcp.write_allow policy). One file per call; the '
         'change is committed to git automatically. The change is '
         'complete when this call returns; a dependent call must wait '
         'for it (batched requests run concurrently).',
         {'type': 'object',
          'properties': {'path': {'type': 'string',
                                  'description': 'File to write, '
                                  'relative to the view root, e.g. '
                                  '"playbooks/deploy.yml" or '
                                  '"resources/scripts/backup.sh"'},
                         'content': {'type': 'string',
                                     'description': 'The complete file '
                                     'content (UTF-8 text)'},
                         'mode': {'type': 'string',
                                  'description': 'Octal file mode, e.g. '
                                  '"0755", for scripts which need the '
                                  'executable bit; permission bits '
                                  'only. Omit to keep the mode of an '
                                  'existing file, or 0644 for a new '
                                  'one'}},
          'required': ['path', 'content']},
         lambda arguments: tool_file_write(project, policy, arguments,
                                           server=server)),
        ('file.delete',
         'Delete a file from the current infrastructure view, in '
         'allowed locations only (the project.mcp.write_allow policy). '
         'Regular files only, one per call; the deletion is committed '
         'to git automatically. The change is complete when this call '
         'returns; a dependent call must wait for it (batched requests '
         'run concurrently).',
         {'type': 'object',
          'properties': {'path': {'type': 'string',
                                  'description': 'File to delete, '
                                  'relative to the view root, e.g. '
                                  '"playbooks/deploy.yml"'}},
          'required': ['path']},
         lambda arguments: tool_file_delete(project, policy, arguments,
                                            server=server)),
        ('inventory.spec',
         'Apply an inventory specification to the inventory of the '
         'current view: define groups, hosts and variables the DebOps '
         'way, instead of writing inventory files directly. Existing '
         'files are skipped unless "overwrite" is set; use "dry_run" '
         'to preview the changes. See the debops-inventory-spec skill '
         'for the document format. The change is committed to git '
         'automatically.',
         {'type': 'object',
          'properties': {'spec': {'type': 'string',
                                  'description': 'The inventory '
                                  'specification document (YAML)'},
                         'dry_run': {'type': 'boolean',
                                     'description': 'Report the planned '
                                     'changes without writing them '
                                     '(default: false)'},
                         'overwrite': {'type': 'boolean',
                                       'description': 'Replace existing '
                                       'inventory files (default: '
                                       'false, existing files are '
                                       'skipped)'}},
          'required': ['spec']},
         lambda arguments: tool_inventory_spec(project, policy,
                                               arguments)),
        ('keyring.fetch',
         'Fetch a GPG key into the project keyring (ansible/keyring/), '
         'for debops.keyring to use with APT repositories and signed '
         'data. The server fetches and verifies the key itself; the key '
         'material never enters the conversation. The file is named '
         '0x<FINGERPRINT>.asc. The change is complete when this call '
         'returns; a dependent call must wait for it (batched requests '
         'run concurrently).',
         {'type': 'object',
          'properties': {
              'url': {'type': 'string',
                      'description': 'HTTPS URL of an ASCII-armored key, '
                      'e.g. "https://keybase.io/drybjed/key.asc"'},
              'keyserver': {'type': 'string',
                            'description': 'HKPS keyserver to fetch from '
                            '(default: "keyserver.ubuntu.com"); requires '
                            '"fingerprint"'},
              'fingerprint': {'type': 'string',
                              'description': 'The expected full GPG '
                              'fingerprint of the key (40 hex '
                              'characters); the fetch is refused when '
                              'the server-computed fingerprint differs'}},
          'required': []},
         lambda arguments, context: tool_keyring_fetch(
             project, policy, arguments, context),
         True, True),
        ('config.get',
         'Return the value of a DebOps configuration option, like '
         '"debops config get" does; the key is a dot-separated path in '
         'the configuration tree.',
         {'type': 'object',
          'properties': {'key': {'type': 'string',
                                 'description': 'Configuration key, e.g. '
                                 '"project.default_view"'}},
          'required': ['key']},
         lambda arguments: tool_config_get(project, arguments)),
        ('environment.list',
         'List the environment variables the server\'s tool runs get, '
         'by origin: the project MCP environment file (with values), '
         'the tokens file (names only), the project environment and the '
         'process environment (names only).',
         {'type': 'object',
          'properties': {'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'variables whose name contains '
                                    'this string'}}},
         lambda arguments: tool_environment_list(project, policy,
                                                 arguments)),
        ('environment.set',
         'Set an environment variable for the runs of this server, in '
         'the tracked environment file (.debops/mcp/environment, '
         'committed) or, with "secret": true, in the untracked tokens '
         'file (.debops/mcp/.env, never committed) — for tokens and '
         'API keys. The change is complete when this call returns; a '
         'dependent call must wait for it (batched requests run '
         'concurrently).',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'The variable name, '
                                  'e.g. "REGION"'},
                         'value': {'type': 'string',
                                   'description': 'The value (never '
                                   'shown again for secrets)'},
                         'secret': {'type': 'boolean',
                                    'description': 'Store in the '
                                    'untracked tokens file '
                                    '(default: false)'}},
          'required': ['name', 'value']},
         lambda arguments: tool_environment_set(project, policy,
                                                arguments)),
        ('environment.unset',
         'Remove an environment variable from the MCP environment '
         'files. The change is complete when this call returns; a '
         'dependent call must wait for it (batched requests run '
         'concurrently).',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'The variable name'},
                         'secret': {'type': 'boolean',
                                    'description': 'Remove from the '
                                    'untracked tokens file '
                                    '(default: false)'}},
          'required': ['name']},
         lambda arguments: tool_environment_unset(project, policy,
                                                  arguments)),
        ('inventory.query',
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
        ('secret.paths',
         'Find the paths of secrets in the project view\'s secret/ '
         'directory, without reading their contents. Use the paths to '
         'compose inventory lookups such as lookup("password", '
         'secret + "/<path>"). Secrets are unlocked and relocked as '
         'needed, serialized with playbook executions.',
         {'type': 'object',
          'properties': {'pattern': {'type': 'string',
                                     'description': 'Glob pattern '
                                     'matched against paths relative '
                                     'to the secret/ directory, e.g. '
                                     '"pki/**" or "**/password" '
                                     '(default: all secrets)'}},
          'required': []},
         lambda arguments: tool_secret_paths(project, policy, arguments)),
    ]

    # Playbook execution is gated: 'check' mode by default, changes only
    # with 'project.mcp.execute: all'. A gated-off tool stays registered
    # as a stub which answers with the policy path — a missing tool
    # makes agents retry-loop, a refused one teaches
    execute_mode = policy.execute if policy is not None else 'check'
    execution_stubs = set()
    if execute_mode in ('check', 'all'):
        tools.append((
            'check',
            'Run Ansible playbooks in check mode (no changes are '
            'made), reporting what would change. Facts are gathered '
            'live from the managed hosts. Accepts a playbook target '
            'or a list of targets (see the playbooks tool), run in '
            'order, and optional limit/tags. A check run of a '
            'project-local playbook asks the user first: its tasks '
            'can execute commands even in check mode. The result is '
            'a point-in-time preview: the project or the inventory '
            'can change between the check and the run. The change is '
            'complete when this call returns; a dependent call must '
            'wait for it (batched requests run concurrently).'
            + _survival_note(),
            execution_schema(policy),
            lambda arguments, context: tool_check(
                project, policy, arguments, context,
                enabled_check=enabled_check,
                on_disabled=server.cancel_all),
            True, True, True))
    if execute_mode == 'all':
        tools.append((
            'run',
            'Run Ansible playbooks, changing the managed hosts. Use '
            'the "check" tool first to preview the changes. Accepts a '
            'playbook target or a list of targets (see the playbooks '
            'tool), run in order, and optional limit/tags. The change '
            'is complete when this call returns; a dependent call '
            'must wait for it (batched requests run concurrently).'
            + _survival_note(),
            execution_schema(policy),
            lambda arguments, context: tool_run(
                project, policy, arguments, context,
                enabled_check=enabled_check,
                on_disabled=server.cancel_all),
            True, False, True))
    elif execute_mode == 'check':
        execution_stubs.add('run')
        tools.append(_execution_stub_entry('run', policy, execute_mode))
    else:
        execution_stubs.update(('check', 'run'))
        tools.append(_execution_stub_entry('check', policy,
                                           execute_mode))
        tools.append(_execution_stub_entry('run', policy, execute_mode))

    # The run logs stay queryable whatever the execution gate says: a
    # log written while execution was allowed is read back even after
    # the gate closes
    tools.extend([
        ('execution.list',
         'List the playbook executions of the active project view, '
         'oldest first: id, mode, playbooks, hosts, exit code, log '
         'size, timestamps. Use an id with the execution.output tool '
         'to inspect that run without re-executing anything.',
         {'type': 'object', 'properties': {},
          'additionalProperties': False},
         lambda arguments: tool_execution_list(project, policy,
                                               arguments)),
        ('execution.output',
         'Read the output of one playbook execution from its run log; '
         'the full log is kept on disk, this tool answers questions '
         'about it without re-running anything. Kinds: "changed" (the '
         'tasks which changed something, with their per-host changed '
         'lines), "failed", "unreachable", "recap", "warnings" '
         '(includes the deprecation warnings), "tail" (the default). '
         'Or a free regex in "pattern", with '
         '"context" lines of surroundings — that is how to pull a '
         'diff block. Results are capped ("max_lines", default 200); '
         'a truncation marker says when to narrow the query. Works on '
         'a run which is still going.',
         {'type': 'object', 'properties': {
             'id': {'type': ['integer', 'string'],
                    'description': 'The execution id, from a check/run '
                    'result or execution.list; "last" (the default) '
                    'for the newest run'},
             'kind': {'type': 'string',
                      'enum': ['changed', 'failed', 'unreachable',
                               'recap', 'warnings', 'tail'],
                      'description': 'The canned query (default: '
                      '"tail")'},
             'pattern': {'type': 'string',
                         'description': 'A regular expression matched '
                         'against the log lines (instead of "kind")'},
             'context': {'type': 'integer',
                         'description': 'Lines of context around each '
                         'pattern match (default 3)'},
             'max_lines': {'type': 'integer',
                           'description': 'The most lines to return '
                           '(default 200, at most 1000)'}},
          'additionalProperties': False},
         lambda arguments: tool_execution_output(project, policy,
                                                 arguments))])

    # Host queries change nothing, so they are registered wherever
    # execution is allowed at all
    if execute_mode in ('check', 'all'):
        exec_properties = {
            'hosts': {'type': 'string',
                      'description': 'Host or group pattern to query, '
                      'e.g. "webservers" or "web1.example.com"'},
            'become': {'type': 'boolean',
                       'description': 'Escalate privileges on the '
                       'remote host (sudo to root) — useful because an '
                       'unprivileged account sees a different fact set '
                       'than root. Whether it works is governed by the '
                       'remote sudo configuration, not by this server. '
                       'The project default is set with '
                       '"project.mcp.become"; this argument overrides '
                       'it per call.'},
        }
        tools.extend([
            ('ansible.ping',
             'Check SSH connectivity and Ansible reachability of hosts '
             '(ansible -m ping). Use this to verify a connection before '
             'querying or changing a host.',
             {'type': 'object', 'properties': exec_properties,
              'required': ['hosts']},
             lambda arguments, context: tool_ansible_ping(
                 project, policy, arguments, context,
                 enabled_check=enabled_check,
                 on_disabled=server.cancel_all),
             True),
            ('ansible.facts',
             'Gather Ansible facts from hosts (ansible -m setup), '
             'for debugging and environment knowledge. The output is '
             'large; use "filter" to narrow it. The filter is a glob '
             'over top-level fact names ("ansible_distribution*"); '
             'a dotted path narrows into a branch instead '
             '("ansible_local.postfix*" — the per-role applied state '
             'lives in the ansible_local dict). The answer is the '
             'facts namespace itself, without Ansible\'s result '
             'envelope.' + FORMAT_NOTE,
             {'type': 'object',
              'properties': dict(exec_properties, **{
                  'filter': {'type': 'string',
                             'description': 'Narrow the gathered facts: '
                             'a glob over top-level fact names, or a '
                             'dotted path into a branch '
                             '("ansible_local.postfix*")'},
                  'format': FORMAT_PROPERTY}),
              'required': ['hosts']},
             lambda arguments, context: tool_ansible_facts(
                 project, policy, arguments, context,
                 enabled_check=enabled_check,
                 on_disabled=server.cancel_all),
             True),
        ])

    # The documentation tools: the bundled DebOps content, the
    # collections the index provides, and the project's own roles and
    # playbooks
    tools.extend(_documentation_entries(
            server, policy, project, roles_path, playbooks_path,
            collection_index_provider))

    # View lifecycle: the active infrastructure view is a property of
    # the server itself — the substrate (one git repository, the
    # secrets mounts) is global, so the view is global, switchable
    # mid-session unless the policy locks it
    tools.extend([
        ('view.list',
         'List the infrastructure views of this project: which are '
         'registered, which is active, whether the view is locked.'
         + FORMAT_NOTE,
         {'type': 'object',
          'properties': {'format': FORMAT_PROPERTY},
          'required': []},
         lambda arguments: tool_view_list(project, policy, arguments)),
        ('view.switch',
         'Switch the active infrastructure view of the server to a '
         'registered one. The change is global — all connected clients '
         'then work in the new view, like sharing one checked-out git '
         'branch; every response carries the active view in its '
         '"_meta" stamp. Use view.list to see what exists.',
         {'type': 'object',
          'properties': {'view': {'type': 'string',
                                  'description': 'Name of the '
                                  'registered view to activate'}},
          'required': ['view']},
         lambda arguments: tool_view_switch(project, policy, arguments,
                                            server)),
        ('view.create',
         'Create an infrastructure view (the way "debops project '
         'mkview" does): a registered inventory root of its own, '
         'committed to git. Activate it afterwards with view.switch. '
         'Modern projects only; view names are lowercase letters, '
         'digits and underscores — no dashes (the serve --instance '
         'names split at the last dash).',
         {'type': 'object',
          'properties': {'view': {'type': 'string',
                                  'description': 'Name of the view to '
                                  'create'}},
          'required': ['view']},
         lambda arguments: tool_view_create(project, policy, arguments)),
    ])

    # Git synchronization with the project origin; throttled per
    # direction by 'project.mcp.sync_interval'. The state lives on the
    # server so a policy reload does not reset the throttle
    sync_state = getattr(server, 'sync_state', None)
    if sync_state is None:
        sync_state = server.sync_state = {'pull': 0.0, 'push': 0.0}
    tools.extend([
        ('project.pull',
         'Pull the latest project state from its git origin '
         '(fast-forward only). Use this to pick up changes made '
         'elsewhere before reading or changing the project. The '
         'change is complete when this call returns; a dependent call '
         'must wait for it (batched requests run concurrently).',
         {'type': 'object', 'properties': {},
          'additionalProperties': False},
         make_sync_tool(project, policy, sync_state, 'pull',
                        server=server)),
        ('project.push',
         'Push the project\'s committed state to its git origin. Only '
         'committed history is sent; the working tree must be clean. '
         'The change is complete when this call returns; a dependent '
         'call must wait for it (batched requests run concurrently).',
         {'type': 'object', 'properties': {},
          'additionalProperties': False},
         make_sync_tool(project, policy, sync_state, 'push')),
    ])

    # Ansible Collection management: requirements file plus install
    tools.extend([
        ('collection.require',
         'Require an Ansible Collection in the project: add or update '
         'its entry in ansible/collections/requirements.yml (with '
         '"version", and "source"/"type" for a git repository) and '
         'install the project collections. The change is committed to '
         'git automatically. The change is complete when this call '
         'returns; a dependent call must wait for it (batched requests '
         'run concurrently).',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'The collection, e.g. '
                                  '"community.docker"'},
                         'version': {'type': 'string',
                                     'description': 'Version constraint, '
                                     'e.g. ">=5.0" (a git ref with a '
                                     'git source)'},
                         'source': {'type': 'string',
                                    'description': 'Install from this '
                                    'URL instead of Ansible Galaxy '
                                    '(a git repository)'},
                         'type': {'type': 'string',
                                  'description': 'The source type, e.g. '
                                  '"git"'}},
          'required': ['name']},
         lambda arguments, context: tool_collection_require(
             project, policy, arguments, context,
             enabled_check=enabled_check,
             on_disabled=server.cancel_all),
         True, False, True),
        ('collection.remove',
         'Remove an Ansible Collection from the project: drop its entry '
         'from the requirements and lock files, and delete its '
         'installed content from ansible/collections/ (ansible-galaxy '
         'cannot remove). The change is committed to git '
         'automatically. The change is complete when this call '
         'returns; a dependent call must wait for it (batched requests '
         'run concurrently).',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'The collection, e.g. '
                                  '"community.docker"'}},
          'required': ['name']},
         lambda arguments: tool_collection_remove(project, policy,
                                                  arguments)),
        ('collection.sync',
         'Install the committed collection requirements of the '
         'project, for example after a pull brought new ones. The '
         'change is complete when this call returns; a dependent call '
         'must wait for it (batched requests run concurrently).',
         {'type': 'object', 'properties': {},
          'additionalProperties': False},
         lambda arguments, context: tool_collection_sync(
             project, policy, arguments, context,
             enabled_check=enabled_check,
             on_disabled=server.cancel_all),
         True, False, True),
    ])

    # Custom script tools: code on the controller, off by default,
    # confirmed per call. The names join the confirm set so that the
    # clients show the confirmation marker; the tools elicit
    # themselves, so the generic gate stays out of the way
    custom_names = set()
    if policy is not None and policy.custom_tools:
        for custom_name, (path, custom_description) in \
                discover_custom_tools(project).items():
            custom_names.add(custom_name)
            tools.append((
                custom_name,
                custom_description or 'A custom tool of this project',
                {'type': 'object'},
                make_custom_tool(project, policy, custom_name, path,
                                 enabled_check=enabled_check,
                                 on_disabled=server.cancel_all),
                True, True, False,
                {'destructiveHint': True, 'idempotentHint': False,
                 'openWorldHint': True}))

    # The overrides write tool injects content into read-only roles; it
    # is not registered unless the project enables it
    if policy is not None and policy.overrides:
        tools.append((
            'overrides.write',
            'Write a file into the project\'s overrides '
            '(ansible/overrides/): alternative files, templates and '
            'additional tasks for read-only roles, run on the managed '
            'hosts at the next playbook execution. The change is '
            'committed to git automatically. The change is complete '
            'when this call returns; a dependent call must wait for it '
            '(batched requests run concurrently).',
            {'type': 'object',
             'properties': {'path': {'type': 'string',
                                     'description': 'File to write, '
                                     'relative to the overrides tree: '
                                     '"files/...", "tasks/..." or '
                                     '"templates/..."'},
                            'content': {'type': 'string',
                                        'description': 'The complete '
                                        'file content (UTF-8 text)'},
                            'mode': {'type': 'string',
                                     'description': 'Octal file mode, '
                                     'e.g. "0755", for scripts which '
                                     'need the executable bit; '
                                     'permission bits only. Omit to '
                                     'keep the mode of an existing '
                                     'file, or 0644 for a new one'}},
             'required': ['path', 'content']},
            lambda arguments: tool_overrides_write(project, policy,
                                                   arguments)))

    # With no active view the view-bound tools stay registered but
    # answer with the recovery path, so a client which cached the
    # surface keeps stable tool names
    if (policy is not None and policy.view_root is None
            and project.project_type != 'legacy'):
        tools = [_no_view_stub(entry) if entry[0] in VIEW_BOUND_TOOLS
                 else entry for entry in tools]

    # Tools the policy gates on user confirmation carry the marker and
    # say so in their description; an ungated tool never mentions it.
    # The execution stubs neither gate nor badge — they never ask
    confirm = set(policy.confirm) if policy is not None else set()
    confirm |= custom_names
    confirm -= execution_stubs

    _register_entries(target, tools, policy, confirm=confirm,
                      project=project)
    _apply_aliases(server)


def register_documentation_tools(server, roles_path=COLLECTION_ROLES,
                                 playbooks_path=COLLECTION_PLAYBOOKS,
                                 collection_index_provider=None):
    """Register the documentation tools, without a project directory.

    The documentation mode serves the bundled DebOps content and the
    collections the environment provides, read-only; nothing
    project-bound is registered.
    """
    policy = server.policy
    if collection_index_provider is None:
        collection_index_provider = docs_collection_provider()
    entries = _documentation_entries(server, policy, None, roles_path,
                                     playbooks_path,
                                     collection_index_provider)
    _register_entries(server.tools, entries, policy)
    _apply_aliases(server)
