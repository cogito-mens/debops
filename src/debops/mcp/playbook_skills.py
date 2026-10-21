# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generated brief skills per collection playbook, at
'skill://playbooks/'.

The DebOps playbook semantics make most of the corpus derivable: a
'service/<name>' playbook applies the whole stack which configures a
service (the role plus its dependencies), and a 'scope/<name>' one
runs a single role on the scoped groups — 'scope/<name>/<entry>' the
role's one alternative action ('scope/apt/update' executes 'apt
update'). A target whose name maps to a visible role gets a brief;
the variant family ('cryptsetup-plain') maps by stripping at the
first dash. Everything else — the layers, the top-level playbooks,
'ldap/', 'tools/' — is authored content: a filesystem skill at the
same path shadows the generated one, and the project tree shadows
both.

The brief is a Jinja2 template rendered per read, so a playbook edit
lands with the next expiry. Which tools the text points at depends
on the mode: the project mode has the execution tools, the
documentation mode does not, and the agent should never meet a tool
which does not exist there.
"""

import logging
import os
import time

import jinja2

from .skills import _parse_frontmatter_text
from .tools.playbooks import list_playbooks, parse_playbook, target_form

logger = logging.getLogger(__name__)

# How long the target scan is cached; the role briefs' rhythm
PLAYBOOK_BRIEF_TTL = 60  # seconds

# The playbook sections the generator knows
SECTIONS = ('service', 'scope')

_BRIEF = '''\
---
name: {{ skill_name }}
description: {{ description }}
---

# {{ target }}

{{ summary }}

{% if section == 'service' -%}
Configures the service end to end: the stack which makes it, not only
the role — {{ stack }}.
{%- if variants %} Variants of it: {{ variants }}.{% endif %}
{%- else -%}
Runs only the '{{ role }}' role{% if action %} — its '{{ action }}'
action{% endif %} — on the scoped groups ({{ hosts }}). The host is
expected to carry the configured service already: the 'common' playbook
is applied initially everywhere, then 'service/{{ role }}' for the
stack; without them the run may fail or leave a partial configuration
(missing APT repositories, for example).
{%- endif %}
{% if variant %}
A variant of the {{ variant }} playbook; the parent's brief covers the
shared parts.
{% endif %}
{% if execution -%}
Verify with 'check' ('syntax_check' or 'list_tasks' for a playbook you
wrote), change with 'run'; inspect the outcome with 'execution.output'.
{%- else -%}
Inspect it with 'playbook.info'; the role's details answer through the
'role.*' tools and 'ansible.doc'.
{%- endif %}

The role's brief is at 'skill://roles/{{ role_dashed }}/'.
'''


class PlaybookSkillSource(object):
    """The generated per-playbook skills, merged into a SkillLibrary.

    'playbooks_path' is the collection's playbooks root; 'roles' is
    the RoleSkillSource the names resolve against (the role must be
    visible for a brief to exist). 'execution' says whether the
    project-mode tools exist; the documentation mode renders without
    them.
    """

    # No reservation: authored content lives in the package tree under
    # the same prefix ('_data/skills/playbooks/') and shadows the
    # generated briefs, and the project tree shadows both
    prefixes = set()

    def __init__(self, playbooks_path, roles, execution=True):
        self.playbooks_path = playbooks_path
        self._roles = roles
        self.execution = execution
        self._environment = jinja2.sandbox.SandboxedEnvironment(
                keep_trailing_newline=True)
        self._template = self._environment.from_string(_BRIEF)
        self._cache = {'at': 0.0, 'targets': None}

    def _targets(self):
        """The runnable targets of the collection: (target, path)."""
        if (self._cache['targets'] is not None
                and time.time() - self._cache['at'] < PLAYBOOK_BRIEF_TTL):
            return self._cache['targets']
        targets = []
        if self.playbooks_path is not None \
                and os.path.isdir(self.playbooks_path):
            for relpath in list_playbooks(self.playbooks_path):
                targets.append((target_form(relpath),
                                os.path.join(self.playbooks_path,
                                             relpath)))
        self._cache['at'] = time.time()
        self._cache['targets'] = targets
        return targets

    def _target_of(self, skill_path):
        """The playbook target of a skill path, or None.

        Skill path segments are dashed (the Agent Skills convention);
        playbook file names carry underscores and dashes both. An
        exact file wins over the dash-to-underscore fold.
        """
        segments = skill_path.split('/')
        exact = os.path.join(self.playbooks_path, *segments) + '.yml'
        if os.path.isfile(exact):
            return skill_path
        folded = [segment.replace('-', '_') for segment in segments]
        candidate = os.path.join(self.playbooks_path, *folded) + '.yml'
        if os.path.isfile(candidate):
            return '/'.join(folded)
        return None

    def _map(self, target):
        """Map a target to (section, role, action, variant); None when
        the target names no visible role.

        'service/<name>' and 'scope/<name>' map to the role '<name>';
        'scope/<name>/<entry>' names the role's alternative action. A
        dashed name which matches nothing exact strips once at the
        first dash — the variant family ('cryptsetup-plain' →
        'cryptsetup').
        """
        segments = target.split('/')
        if len(segments) < 2 or segments[0] not in SECTIONS:
            return None
        section = segments[0]
        if len(segments) == 2:
            name, action = segments[1], None
        elif len(segments) == 3:
            name, action = segments[1], segments[2]
        else:
            return None
        variant = None
        role = name
        if self._roles.role_directory(role) is None:
            stripped = name.split('-')[0]
            if self._roles.role_directory(stripped) is None:
                return None
            role, variant = stripped, '{}/{}'.format(section, stripped)
        return section, role, action, variant

    def _fields(self, target, path):
        """The template fields of a target, from its own text."""
        try:
            with open(path) as fp:
                plays, roles, imports, groups = parse_playbook(
                        fp.read())
        except (OSError, ValueError):
            plays, roles, imports, groups = [], [], [], []
        play_name = plays[0]['name'] if plays and plays[0]['name'] \
            else None
        return play_name, roles, imports, groups

    def _render(self, target):
        """The brief of one target, or None when it maps to nothing."""
        mapped = self._map(target)
        if mapped is None:
            return None
        section, role, action, variant = mapped
        path = os.path.join(self.playbooks_path,
                            *target.split('/')) + '.yml'
        play_name, roles, imports, groups = self._fields(target, path)

        description = play_name or self._roles.role_description(role) \
            or 'The {} playbook of the DebOps collection.'.format(target)
        # The Agent Skills convention bounds the description
        description = description.replace('\n', ' ').strip()[:1024]

        stack = [name.split('.', 2)[-1] for name in roles]
        stack_text = (', '.join(stack[:-1]) + ' and ' + stack[-1]
                      if len(stack) > 1 else (stack[0] if stack
                                              else "the '{}' role"
                                              .format(role)))
        # The variants of a parent: the sibling targets which map to
        # the same role by the strip rule or a nested action
        variants = []
        if variant is None and action is None:
            for other, _other_path in self._targets():
                if other == target or not (
                        other.startswith(target + '-')
                        or other.startswith(target + '/')):
                    continue
                other_mapped = self._map(other)
                if (other_mapped is not None
                        and other_mapped[0] == section
                        and other_mapped[1] == role):
                    variants.append("'{}'".format(other[len(target):]
                                                  .lstrip('/-')))

        sentences = [description if description.endswith('.')
                     else description + '.']
        if groups:
            sentences.append('Runs on {}.'.format(
                    ', '.join("'{}'".format(group) for group in groups)))
        if imports:
            sentences.append('Imports {}.'.format(
                    ', '.join("'{}'".format(item) for item in imports)))
        summary = ' '.join(sentences)
        return self._template.render(
                skill_name=target.replace('/', '-').replace('_', '-'),
                description=description,
                target=target,
                summary=summary,
                section=section,
                role=role,
                role_dashed=role.replace('_', '-'),
                action=action,
                variant=variant,
                stack=stack_text,
                variants=', '.join(variants),
                hosts=', '.join("'{}'".format(group) for group in groups),
                execution=self.execution)

    def uris_for(self, role):
        """The generated brief URIs covering a role: every service and
        scope playbook which maps to it, the canonical service playbook
        first."""
        uris = []
        for target, _path in self._targets():
            mapped = self._map(target)
            if mapped is None or mapped[1] != role:
                continue
            uris.append('skill://playbooks/{}/SKILL.md'.format(
                    target.replace('_', '-')))
        # The canonical service playbook first, then the scope one,
        # then the variants and the nested actions
        uris.sort(key=lambda uri: ('/service/' not in uri,
                                   uri.count('/'), uri))
        return uris

    def entries(self):
        entries = []
        for target, _path in self._targets():
            if self._map(target) is None:
                continue
            # The skill path is dashed (the Agent Skills convention);
            # the playbook target keeps its underscores
            skill_path = target.replace('_', '-')
            skill_name = skill_path.replace('/', '-')
            # The frontmatter's description is the brief's first line;
            # render once, reuse
            content = self._render(target)
            if content is None:
                continue
            frontmatter = _parse_frontmatter_text(content)
            frontmatter.setdefault('name', skill_name)
            entries.append({
                'uri': 'skill://playbooks/{}/SKILL.md'.format(
                        skill_path),
                'frontmatter': frontmatter,
                'resources': 'dynamic'})
        return entries

    def read(self, rest):
        """Read a 'playbooks/' URI: the generated brief."""
        segments = rest.split('/')
        if len(segments) < 2 or segments[0] != 'playbooks':
            return None
        if segments[-1] != 'SKILL.md':
            return None
        target = self._target_of('/'.join(segments[1:-1]))
        if target is None:
            return None
        return self._render(target)
