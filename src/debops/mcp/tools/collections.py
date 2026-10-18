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

from ..policy import confine_under
from .common import (error_result, text_result)
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


def galaxy_collection_list(project):
    """Run 'ansible-galaxy collection list' in the project environment.

    The environment computed by DebOps points PATH at the project's
    virtual environment when one is enabled, which is what makes the
    collections installed there visible. No hooks run and the inventory
    stays locked: listing collections needs neither.
    """
    env = os.environ.copy()
    env.update(project.config._env_vars)
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
    return json.loads(result.stdout)


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
    for fqcn in sorted(index):
        if name_filter and name_filter not in fqcn.lower():
            continue
        roles = list_collection_roles(index[fqcn]['path'])
        if not roles:
            continue
        total += len(roles)
        version = index[fqcn]['version']
        lines.append('## {}{}'.format(
                fqcn, ' ({})'.format(version) if version else ''))
        lines.extend('- {}'.format(role) for role in roles)
        lines.append('')
    if not lines:
        return text_result('no collections with roles found')
    header = '# Collections with roles visible to Ansible ({})'.format(
            total)
    return text_result('\n'.join([header, ''] + lines))


def tool_collection_playbooks(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))
    collection = str(arguments.get('collection') or '')
    entry = resolve_collection(index, collection)
    if entry is None:
        return error_result('collection not found or invalid: {}'
                            .format(collection))

    playbooks_dir = os.path.join(entry['path'], 'playbooks')
    if not os.path.isdir(playbooks_dir):
        return text_result('collection {} provides no playbooks'
                           .format(collection))

    name_filter = str(arguments.get('filter') or '').lower()
    lines = ['# Playbooks in {} ({})'.format(
            collection, entry['version'] or 'unknown version'), '']
    total = 0
    for relpath in list_playbooks(playbooks_dir):
        if name_filter and name_filter not in relpath.lower():
            continue
        line = '- `{}/{}`'.format(collection, target_form(relpath))
        summary = playbook_summary(os.path.join(playbooks_dir, relpath))
        if summary:
            line += ' — {}'.format(summary)
        lines.append(line)
        total += 1
    if not total:
        return text_result('collection {} provides no playbooks'
                           .format(collection))
    return text_result('\n'.join(lines))
