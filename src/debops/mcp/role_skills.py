# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A generated brief skill per Ansible role, at 'skill://roles/'.

Two hundred roles do not fit a model's context; their one-line briefs
do. The source synthesizes a minimal skill per role from its own
metadata ('meta/main.yml'), always in sync with the tree and never a
file to maintain; the entry's resources are 'dynamic' per the skills
extension.

Authored content wins at the same address: a SKILL.md at the role's
root (the role author's extensive version — inert to Ansible, the way
a README.md is, and shipped with the collection) replaces the brief,
and a skill in the project's tree ('.debops/mcp/skills/roles/<name>/')
shadows both. Role names carry underscores and skill names do not
(Agent Skills allows dashes only), so the corpus maps between them —
dashes never appear in Ansible role names, which keeps the mapping
unambiguous.
"""

import hashlib
import logging
import os
import re
import time

import yaml

from .policy import confine_under
from .skills import _parse_frontmatter_text

logger = logging.getLogger(__name__)

# Ansible role names are lowercase words and digits with underscores
ROLE_NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')

# How long the role scan is cached; the same rhythm as the collection
# index — a changed role lands with the next expiry
ROLE_BRIEF_TTL = 60  # seconds

_BRIEF = '''---
name: {skill_name}
description: {description}
---

# debops.{role}

{paragraph}

A generated brief; the server answers the details: `role.info` for the
overview and the documentation link, `role.defaults` for the inventory
variables, `role.tasks` for the mechanics, `role.argspec` when the role
declares one, `role.templates` and `role.files` for the shipped files.
Playbooks apply the role — find them with `playbook.list`, preview with
`playbook.info`.
'''


class RoleSkillSource(object):
    """The generated per-role skills, merged into a SkillLibrary.

    The roles_paths are the ones the role tools resolve, in the same
    order (the project's own roles shadow the collection's).
    """

    prefixes = {'roles'}

    def __init__(self, roles_paths):
        self.roles_paths = list(roles_paths)
        self._cache = {'at': 0.0, 'roles': None}

    def _roles(self):
        """The visible roles as name → directory (first path wins)."""
        if (self._cache['roles'] is not None
                and time.time() - self._cache['at'] < ROLE_BRIEF_TTL):
            return self._cache['roles']
        roles = {}
        for base in self.roles_paths:
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                directory = os.path.join(base, name)
                if (name not in roles
                        and ROLE_NAME_PATTERN.match(name)
                        and os.path.isdir(directory)):
                    roles[name] = directory
        self._cache['at'] = time.time()
        self._cache['roles'] = roles
        return roles

    def _meta(self, directory):
        """The role's galaxy_info metadata; empty when unreadable."""
        try:
            with open(os.path.join(directory, 'meta', 'main.yml')) as fp:
                meta = yaml.safe_load(fp.read())
        except (OSError, yaml.YAMLError):
            return {}
        if not isinstance(meta, dict):
            return {}
        info = meta.get('galaxy_info') or {}
        return info if isinstance(info, dict) else {}

    def _description(self, role, info):
        description = str(info.get('description') or '').strip()
        if description:
            return description
        return 'The {} role of the DebOps project (no description in ' \
               'the role metadata)'.format(role)

    def _brief(self, role, directory):
        """The generated SKILL.md content of a role."""
        info = self._meta(directory)
        description = self._description(role, info)
        skill_name = role.replace('_', '-')
        return _BRIEF.format(skill_name=skill_name, role=role,
                             description=description,
                             paragraph=description)

    def _entry(self, role, directory):
        skill_name = role.replace('_', '-')
        uri = 'skill://roles/{}/SKILL.md'.format(skill_name)
        authored = os.path.join(directory, 'SKILL.md')
        if os.path.isfile(authored):
            # The role author's own version replaces the brief; the
            # frontmatter is served verbatim, with digests
            with open(authored, 'rb') as fp:
                content = fp.read()
            frontmatter = _parse_frontmatter_text(
                    content.decode('utf-8', 'replace'))
            frontmatter.setdefault('name', skill_name)
            resources = [{
                'uri': uri,
                'digest': 'sha256:' + hashlib.sha256(content)
                          .hexdigest(),
                'size': len(content),
            }]
            # Supporting files of the authored skill live in the
            # role's 'skill/' directory, inert to Ansible
            support_dir = os.path.join(directory, 'skill')
            if os.path.isdir(support_dir):
                for dirpath, _dirnames, filenames in os.walk(
                        support_dir):
                    for filename in sorted(filenames):
                        full = os.path.join(dirpath, filename)
                        rel = os.path.relpath(full, directory)
                        with open(full, 'rb') as fp:
                            payload = fp.read()
                        resources.append({
                            'uri': 'skill://roles/{}/{}'.format(
                                    skill_name, rel.replace(os.sep,
                                                            '/')),
                            'digest': 'sha256:' + hashlib.sha256(
                                    payload).hexdigest(),
                            'size': len(payload),
                        })
            return {'uri': uri, 'frontmatter': frontmatter,
                    'resources': resources}
        info = self._meta(directory)
        frontmatter = {'name': skill_name,
                       'description': self._description(role, info)}
        tags = info.get('galaxy_tags')
        if isinstance(tags, list) and tags:
            frontmatter['metadata'] = {'tags': tags}
        return {'uri': uri, 'frontmatter': frontmatter,
                'resources': 'dynamic'}

    def entries(self):
        return [self._entry(role, directory)
                for role, directory in self._roles().items()]

    def read(self, rest):
        """Read a 'roles/' URI: the authored file, or the brief."""
        segments = rest.split('/')
        if len(segments) < 3 or segments[0] != 'roles':
            return None
        role = segments[1].replace('-', '_')
        relpath = '/'.join(segments[2:])
        directory = self._roles().get(role)
        if directory is None:
            return None
        if relpath == 'SKILL.md':
            authored = os.path.join(directory, 'SKILL.md')
            if os.path.isfile(authored):
                with open(authored) as fp:
                    return fp.read()
            return self._brief(role, directory)
        # Supporting files of an authored skill, from the role's
        # 'skill/' directory
        if relpath.startswith('skill/'):
            try:
                target = confine_under(
                        os.path.join(directory, 'skill'),
                        relpath[len('skill/'):])
            except ValueError:
                return None
            if os.path.isfile(target):
                with open(target) as fp:
                    return fp.read()
        return None
