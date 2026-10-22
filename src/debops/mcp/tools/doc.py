# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The ansible.doc tool: ansible-doc over the visible content.

Plugin documentation (modules, lookups, filters, the playbook
keywords, role argument specifications, ...) rendered from the
collections the server may serve, in both the project and the
documentation mode. The lookup is a bounded read-only subprocess, the
same discipline as the collection index: no hooks, no cancellation
plumbing, a timeout and clean errors.
"""

import glob
import json
import logging
import os
import re
import shutil
import subprocess

from ...constants import DEBOPS_PACKAGE_DATA
from .collections import BUNDLED_COLLECTIONS_ROOT
from .common import (document_result, error_result, mcp_environ,
                     parse_output_format, text_result)

logger = logging.getLogger(__name__)

# The ansible-doc plugin types (ansible-doc --type ...)
PLUGIN_TYPES = ('become', 'cache', 'callback', 'cliconf', 'connection',
                'filter', 'httpapi', 'inventory', 'keyword', 'lookup',
                'module', 'netconf', 'role', 'shell', 'strategy',
                'test', 'vars')

# Snippets exist only for these types (ansible-doc --snippet)
SNIPPET_TYPES = ('inventory', 'lookup', 'module')

# A plugin name: letters, digits, underscores and dots (the FQCN form)
PLUGIN_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_.]+$')

DOC_TIMEOUT = 60  # seconds; a full plugin scan can take a while

# Where the account's own ansible-doc lives, in preference order: the
# pipx or pip --user install, the administrator's /usr/local install,
# the distribution package. The harness which spawns a server may
# carry a PATH without the user locations
ACCOUNT_ANSIBLE_DOC = ('~/.local/bin/ansible-doc',
                       '/usr/local/bin/ansible-doc',
                       '/usr/bin/ansible-doc')

# The collection roots ansible-doc searches by default. With no
# operator-chosen ANSIBLE_COLLECTIONS_PATH the documentation mode
# serves them next to the bundled collection
ACCOUNT_COLLECTION_ROOTS = ('~/.ansible/collections',
                            '/usr/share/ansible/collections')


def account_ansible_doc():
    """The ansible-doc of the account's own environment, or None.

    The user locations in preference order, then the inherited PATH;
    None when nothing is found, so the caller decides how to report
    the absence.
    """
    for candidate in ACCOUNT_ANSIBLE_DOC:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.debug('Resolved ansible-doc: %s', path)
            return path
    found = shutil.which('ansible-doc')
    logger.debug('Resolved ansible-doc: %s', found or '(not found)')
    return found


def _doc_environ(project, policy):
    """The environment for the ansible-doc subprocess.

    The project environment in a project. In the documentation mode
    the account's own environment is consulted: an operator-chosen
    ANSIBLE_COLLECTIONS_PATH is inherited as is, and without one the
    account's default collection roots are served next to the bundled
    collection, so the system-wide or pipx-installed ansible-doc
    answers for the content the account actually has. A stray
    ansible.cfg from the harness' launch directory must not smuggle
    ambient plugin paths in either way. When the content policy
    restricts the visible collections, the variable is computed from
    the allowed roots instead of inherited.
    """
    if project is not None:
        env = mcp_environ(project)
    else:
        env = dict(os.environ)
        # Hermetic by construction: the packaged empty configuration
        # (and home) means a stray ansible.cfg — from the directory
        # the harness was launched in, or the account's — never
        # smuggles ambient plugin paths into a projectless server
        neutral = os.path.join(DEBOPS_PACKAGE_DATA, 'mcp')
        env['ANSIBLE_CONFIG'] = os.path.join(neutral, 'ansible.cfg')
        env['ANSIBLE_HOME'] = neutral
        if 'ANSIBLE_COLLECTIONS_PATH' not in os.environ:
            roots = [BUNDLED_COLLECTIONS_ROOT]
            roots.extend(os.path.expanduser(root)
                         for root in ACCOUNT_COLLECTION_ROOTS)
            env['ANSIBLE_COLLECTIONS_PATH'] = os.pathsep.join(roots)
    if policy is not None and not (policy.bundled_content
                                   and policy.external_content):
        roots = []
        if policy.bundled_content:
            roots.append(BUNDLED_COLLECTIONS_ROOT)
        if project is not None:
            # The project's own content is always allowed
            roots.append(os.path.join(project.path, 'ansible',
                                      'collections'))
            venv = os.path.join(project.path, '.debops', 'venv')
            roots.extend(glob.glob(os.path.join(
                    venv, 'lib', 'python*', 'site-packages',
                    'ansible_collections')))
        env['ANSIBLE_COLLECTIONS_PATH'] = os.pathsep.join(roots)
    return env


def tool_ansible_doc(project, policy, arguments, roles_paths=None):
    plugin = str(arguments.get('plugin') or '')
    plugin_type = str(arguments.get('type') or 'module')
    list_mode = bool(arguments.get('list'))
    snippet = bool(arguments.get('snippet'))
    entry_point = str(arguments.get('entry_point') or '')
    try:
        output_format = parse_output_format(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    if plugin_type not in PLUGIN_TYPES:
        return error_result('invalid plugin type: {!r} (choose from '
                            '{})'.format(plugin_type,
                                         ', '.join(PLUGIN_TYPES)))
    if plugin and not PLUGIN_NAME_PATTERN.match(plugin):
        return error_result('invalid plugin name: {!r}'.format(plugin))
    if entry_point and plugin_type != 'role':
        return error_result('an entry point only makes sense with '
                            '"type": "role"')
    if snippet and plugin_type not in SNIPPET_TYPES:
        return error_result('snippets exist only for the {} plugin '
                            'types'.format(', '.join(SNIPPET_TYPES)))
    if snippet and list_mode:
        return error_result('"snippet" and "list" exclude each other')
    if snippet and output_format != 'text':
        return error_result('snippets are text-only')
    if not list_mode and not plugin:
        return error_result('the "plugin" argument is required '
                            '(or pass "list": true)')

    command = ['ansible-doc']
    if project is None:
        # The documentation mode consults the account's own
        # ansible-doc: the harness which spawned the server may carry
        # a PATH without the user installs. The plain name as the last
        # resort, so the subprocess reports it missing in its own
        # words
        command = [account_ansible_doc() or 'ansible-doc']
    if list_mode:
        command.append('--list')
    if snippet:
        command.append('--snippet')
    if plugin_type != 'module':
        command += ['--type', plugin_type]
    if entry_point:
        command += ['--entry-point', entry_point]
    if output_format != 'text':
        command.append('--json')
    for path in roles_paths or []:
        command += ['--roles-path', path]
    if plugin:
        command.append(plugin)

    env = _doc_environ(project, policy)
    try:
        result = subprocess.run(
                command, env=env, cwd=(project.path if project
                                       is not None else None),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=DOC_TIMEOUT)
    except FileNotFoundError:
        return error_result('the ansible-doc command was not found')
    except subprocess.TimeoutExpired:
        return error_result('ansible-doc timed out')
    if result.returncode != 0:
        return error_result('ansible-doc failed: '
                            + result.stderr.strip()[-500:])
    if not result.stdout.strip():
        # ansible-doc exits 0 for a missing plugin, with the
        # '[WARNING]: ... was not found' on stderr; the listing of an
        # unmatched filter is empty the same way but is a valid answer
        if list_mode:
            return text_result('no matching plugins\n')
        return error_result('no documentation for {}: {}'.format(
                plugin,
                result.stderr.strip()[-300:] or 'not found'))

    if output_format != 'text':
        try:
            document = json.loads(result.stdout)
        except ValueError:
            return error_result('ansible-doc did not return JSON: '
                                + result.stdout.strip()[:200])
        return document_result(document, output_format)
    return text_result(result.stdout)
