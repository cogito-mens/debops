# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe roles and playbooks of external Collections.

External collections are enumerated with 'ansible-galaxy collection
list' executed in the project environment, which resolves the generated
ansible.cfg, the project virtual environment's implicit site-packages
path and '~' expansion.
"""

import json
import logging
import os
import re
import subprocess
import time

from ...constants import DEBOPS_PACKAGE_DATA
from ..policy import confine_under
from .common import error_result, mcp_environ, text_result
from .playbooks import (list_playbooks, parse_playbook, playbook_summary,
                        render_playbook_info, target_form)

logger = logging.getLogger(__name__)

# A fully qualified role name: 'namespace.collection.role'
ROLE_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$')

# A fully qualified collection name: 'namespace.collection'
COLLECTION_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+$')

# How long the collection index is cached in the server process;
# collections change only when the user installs or removes them
COLLECTION_INDEX_TTL = 60  # seconds

# The collections root of the bundled DebOps collection; the
# documentation mode defaults to it when the environment does not
# provide one
BUNDLED_COLLECTIONS_ROOT = os.path.join(
        DEBOPS_PACKAGE_DATA, 'ansible', 'collections')


def effective_collections_path(environ):
    """The ANSIBLE_COLLECTIONS_PATH of the documentation mode.

    The environment's value when set, the bundled DebOps collection
    otherwise — the mode is useful with zero configuration, and the
    ambient account defaults ('~/.ansible/collections', ...) never leak
    into it.
    """
    return environ.get('ANSIBLE_COLLECTIONS_PATH') or BUNDLED_COLLECTIONS_ROOT


def galaxy_collection_list(project=None, environ=None):
    """Run 'ansible-galaxy collection list' in the project environment.

    The environment computed by DebOps points PATH at the project's
    virtual environment when one is enabled, which is what makes the
    collections installed there visible. No hooks run and the inventory
    stays locked: listing collections needs neither. Without a project
    (the documentation mode) the ambient environment is used as given.
    """
    if environ is None:
        env = mcp_environ(project) if project is not None \
            else dict(os.environ)
    else:
        env = environ
    try:
        result = subprocess.run(
                ['ansible-galaxy', 'collection', 'list', '--format', 'json'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError('the ansible-galaxy command was not found')
    except subprocess.TimeoutExpired:
        raise RuntimeError('ansible-galaxy collection list timed out')
    if result.returncode != 0:
        raise RuntimeError('ansible-galaxy collection list failed: '
                           + result.stderr.strip())
    try:
        return json.loads(result.stdout)
    except ValueError:
        # A broken or warning-chatty ansible-galaxy writes anything but
        # JSON; that is a tool-logic failure, not an internal error
        raise RuntimeError('ansible-galaxy collection list did not '
                           'return JSON: {}'.format(
                                   result.stdout.strip()[:200]))


def index_from_galaxy(data):
    """Turn 'ansible-galaxy collection list' JSON into {fqcn: entry}.

    When the same collection is installed in multiple paths, the first
    reported location wins.
    """
    index = {}
    for path, collections in data.items():
        for fqcn, info in collections.items():
            index.setdefault(fqcn, {
                'path': os.path.join(path, *fqcn.split('.')),
                'version': str(info.get('version', '')),
            })
    return index


def make_galaxy_index_provider(project=None, environ=None):
    """A collection index provider backed by ansible-galaxy, cached.

    The index is rebuilt at most once per COLLECTION_INDEX_TTL;
    collections change only when the user installs or removes them.
    """
    cache = {'timestamp': 0.0, 'index': None}

    def collection_index_provider():
        if (cache['index'] is None
                or time.time() - cache['timestamp']
                > COLLECTION_INDEX_TTL):
            cache['index'] = index_from_galaxy(
                    galaxy_collection_list(project, environ=environ))
            cache['timestamp'] = time.time()
        return cache['index']

    return collection_index_provider


def filter_collections_under(provider, roots):
    """Keep only the collections installed under one of the roots."""
    real_roots = [os.path.realpath(root) for root in roots]

    def filtered_provider():
        index = {}
        for fqcn, entry in provider().items():
            path = os.path.realpath(entry['path'])
            if any(os.path.commonpath((root, path)) == root
                   for root in real_roots):
                index[fqcn] = entry
        return index

    return filtered_provider


def docs_collection_provider(environ=None):
    """The collection index of the documentation mode.

    Only the collections under the effective ANSIBLE_COLLECTIONS_PATH
    are served: the variable when the environment sets it, the bundled
    DebOps collection otherwise. The variable is set for the galaxy
    subprocess as well, so that the listing and the filter agree; the
    ambient account defaults never leak into a server running outside
    of a project.
    """
    environ = dict(os.environ if environ is None else environ)
    effective = effective_collections_path(environ)
    environ['ANSIBLE_COLLECTIONS_PATH'] = effective
    roots = [entry for entry in effective.split(os.pathsep) if entry]
    provider = make_galaxy_index_provider(environ=environ)
    return filter_collections_under(provider, roots)


def restrict_content(provider, project, policy):
    """Wrap a collection index provider with the content policy.

    With 'bundled_content: false' the DebOps collection is dropped even
    when it lives inside the project; with 'external_content: false',
    anything which is neither DebOps nor project-local is dropped. The
    project's own collections are always visible.
    """
    bundled = policy.bundled_content if policy is not None else True
    external = policy.external_content if policy is not None else True
    if bundled and external:
        return provider
    project_root = os.path.realpath(os.path.join(
            project.path, 'ansible', 'collections',
            'ansible_collections'))

    def restricted():
        index = provider()
        visible = {}
        for fqcn, entry in index.items():
            if fqcn == 'debops.debops':
                if bundled:
                    visible[fqcn] = entry
                continue
            path = os.path.realpath(entry['path'])
            if (os.path.commonpath((project_root, path))
                    == project_root):
                visible[fqcn] = entry
                continue
            if external:
                visible[fqcn] = entry
        dropped = len(index) - len(visible)
        if dropped:
            logger.debug('Content policy dropped %d collection(s)',
                         dropped)
        return visible

    return restricted


def list_collection_roles(collection_path):
    """Return the role names of a collection, sorted."""
    roles_dir = os.path.join(collection_path, 'roles')
    roles = []
    if os.path.isdir(roles_dir):
        for entry in sorted(os.listdir(roles_dir)):
            if (os.path.isdir(os.path.join(roles_dir, entry))
                    and not entry.startswith('.')):
                roles.append(entry)
    return roles


def resolve_collection(index, fqcn):
    """Resolve 'namespace.collection' to its index entry, or None."""
    if not COLLECTION_FQCN_PATTERN.match(str(fqcn or '')):
        return None
    return index.get(fqcn)


def tool_collection_roles(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))

    name_filter = str(arguments.get('filter') or '').lower()
    lines = []
    total = 0
    structured = []
    for fqcn in sorted(index):
        if name_filter and name_filter not in fqcn.lower():
            continue
        roles = list_collection_roles(index[fqcn]['path'])
        if not roles:
            continue
        total += len(roles)
        version = index[fqcn]['version'] or ''
        structured.append({'collection': fqcn, 'version': version,
                           'roles': roles})
        lines.append('## {}{}'.format(
                fqcn, ' ({})'.format(version) if version else ''))
        lines.extend('- {}'.format(role) for role in roles)
        lines.append('')
    if not lines:
        return text_result('no collections with roles found',
                           structured=[])
    header = '# Collections with roles visible to Ansible ({})'.format(
            total)
    return text_result('\n'.join([header, ''] + lines),
                       structured=structured)


def _playbook_entries(fqcn, entry, name_filter, match_qualified):
    """The playbook entries of one collection, as (target, summary).

    With 'match_qualified' (the all-collections listing) the filter
    matches the full 'namespace.collection/path' target form; in the
    single-collection listing it matches the path alone.
    """
    playbooks_dir = os.path.join(entry['path'], 'playbooks')
    entries = []
    if not os.path.isdir(playbooks_dir):
        return entries
    for relpath in list_playbooks(playbooks_dir):
        target = '{}/{}'.format(fqcn, target_form(relpath))
        matched = target if match_qualified else relpath
        if name_filter and name_filter not in matched.lower():
            continue
        entries.append((target, playbook_summary(
                os.path.join(playbooks_dir, relpath))))
    return entries


def tool_collection_playbooks(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))

    name_filter = str(arguments.get('filter') or '').lower()
    collection = str(arguments.get('collection') or '')
    if collection:
        entry = resolve_collection(index, collection)
        if entry is None:
            return error_result('collection not found or invalid: {}'
                                .format(collection))
        entries = _playbook_entries(collection, entry, name_filter,
                                    match_qualified=False)
        if not entries:
            return text_result('collection {} provides no playbooks'
                               .format(collection), structured=[])
        structured = [{'collection': collection, 'target': target,
                       'summary': summary}
                      for target, summary in entries]
        header = '# Playbooks in {} ({})'.format(
                collection, entry['version'] or 'unknown version')
        lines = []
        for target, summary in entries:
            line = '- `{}`'.format(target)
            if summary:
                line += ' — {}'.format(summary)
            lines.append(line)
        return text_result('\n'.join([header, ''] + lines),
                           structured=structured)

    lines = []
    total = 0
    structured = []
    for fqcn in sorted(index):
        entries = _playbook_entries(fqcn, index[fqcn], name_filter,
                                    match_qualified=True)
        if not entries:
            continue
        version = index[fqcn]['version']
        lines.append('## {}{}'.format(
                fqcn, ' ({})'.format(version) if version else ''))
        for target, summary in entries:
            line = '- `{}`'.format(target)
            if summary:
                line += ' — {}'.format(summary)
            lines.append(line)
            structured.append({'collection': fqcn, 'target': target,
                               'summary': summary})
        lines.append('')
        total += len(entries)
    if not lines:
        return text_result('no collections with playbooks found',
                           structured=[])
    header = '# Collections with playbooks visible to Ansible ' \
        '({})'.format(total)
    return text_result('\n'.join([header, ''] + lines),
                       structured=structured)
