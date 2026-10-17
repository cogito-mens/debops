---
name: ansible-programmer
description: Ansible role and playbook development style — DebOps conventions, variable naming, task flow, Jinja2 templates, idempotent patterns, and local facts. Use when creating or modifying Ansible roles, playbooks, or DebOps infrastructure.
license: GPL-3.0-or-later
---

# Ansible role and playbook development style

An Ansible style guide for LLM-assisted programming that blends general Ansible
best practices with conventions used in the [DebOps](https://debops.org/)
project (200+ roles, 700+ playbooks). Covers role structure, variables, tasks,
Jinja templates, playbooks, and verification.

> **Important**: Ansible roles are designed to run on target hosts, not on the
> LLM execution environment. Never run generated playbooks or roles
> directly on the LLM host unless explicitly asked. Always use
> `--syntax-check` and `--check` before applying changes.

## Design philosophy

### Roles and playbooks are read-only

Roles are never modified directly. All customization happens through inventory
variables. This enables clean upgrades via `git pull` and allows multiple
projects to share the same role codebase.

- **Default variables** in `defaults/main.yml` define every tunable parameter.
- **Inventory variables** override defaults at global, group, or host scope.
- **Never** modify a role's tasks, templates, or defaults to suit a specific
  deployment. If you need different behavior, add a new variable.

### Inventory is the source of truth

Roles never modify the Ansible inventory. The inventory expresses user intent;
roles implement it. Dynamic inventories, static YAML files, or any other source
should all work unchanged.

### Single-purpose, composable roles

Each role focuses on exactly one service or concern. Roles are composed in
playbooks to build complete system configurations. If a role needs firewall
rules, it declares a dependent variable — it does not manage firewalls itself.

### Soft dependencies over hard dependencies

Role dependencies are always declared in *playbooks* (soft), never in
`meta/main.yml` (hard). This gives the playbook author full control over which
roles run and in what order. The `secret` role is the only accepted exception.

### State is shared via local facts

One role discovers another role's state through Ansible local facts
(`ansible_local.*`), never by reading its variables directly. This keeps roles
decoupled and independently testable.

## Project structure

### Role directory layout

```
roles/<name>/
├── defaults/
│   └── main.yml            # All default variables (NO vars/ directory)
├── files/                  # Static files, mirrors target filesystem
│   └── etc/nginx/sites-available/
├── handlers/
│   └── main.yml            # Role-specific handlers (optional)
├── meta/
│   └── main.yml            # Galaxy metadata + collections
├── tasks/
│   ├── main.yml            # Main task entry point
│   ├── main_<scope>.yml    # Alternative/scoped task entry point
│   └── <role>/             # Extension hook dirs (pre_main.yml, post_main.yml)
├── templates/              # Jinja2 templates, mirrors target filesystem + .j2
│   └── etc/nginx/sites-available/default.j2
└── COPYRIGHT               # SPDX license/copyright (REUSE compliant)
```

Key rules:
- **No `vars/` directory** — everything is a default, everything is overridable.
- **`files/` and `templates/` mirror the target filesystem path** so the
  destination is obvious from the source tree.
- **Extension hooks** live in `tasks/<role>/pre_main.yml` and
  `tasks/<role>/post_main.yml` (empty files, overridden by playbook authors
  via lookup plugins).

### Playbook directory layout

```
playbooks/
├── site.yml                    # Main entry — imports layers in order
├── layer/
│   ├── net.yml                 # 1. Network layer
│   ├── sys.yml                 # 2. System plumbing
│   ├── common.yml              # 3. Common services
│   ├── env.yml                 # 4. Language environments
│   ├── srv.yml                 # 5. User services
│   ├── app.yml                 # 6. Applications
│   ├── virt.yml                # 7. Virtualization
│   ├── hw.yml                  # 8. Hardware
│   └── agent.yml               # 9. Service agents
├── scope/                      # One playbook per role, scoped
│   ├── <role>/<entry>.yml      # One playbook per role, alt entry point
│   └── <role>.yml
└── service/                    # One playbook per role
    └── <role>.yml
```

### File naming

- YAML files use `.yml` (not `.yaml`).
- Jinja templates use `.j2`.
- Ansible local facts use `/<role>.fact.j2` → rendered to
  `/etc/ansible/facts.d/<role>.fact`.

### Collection metadata

```yaml
# ansible/galaxy.yml
namespace: 'debops'
name: 'debops'
version: '...'

# ansible/roles/<role>/meta/main.yml
dependencies: []                      # Hard deps — keep empty
collections: [ 'debops.debops' ]      # Always declare

galaxy_info:
  author: '...'
  description: '...'
  company: 'DebOps <https://debops.org/>'
  license: 'GPL-3.0-only'
  min_ansible_version: '2.10.0'
  platforms:
    - name: 'Debian'
      versions: [ 'bullseye', 'bookworm' ]
    - name: 'Ubuntu'
      versions: [ 'focal', 'jammy' ]
  galaxy_tags: [ 'debian', 'system' ]
```

## Formatting

### YAML

- **2-space indentation**. No tabs.
- **Spaces inside Jinja delimiters**: `{{ var }}`, not `{{var}}`.
- **80-character line width** preferred (not enforced by lint).
- **`true`/`false`, `yes`/`no`**: use `True`/`False` for boolean values in
  YAML (Ansible convention).
- Files end with a newline. No trailing whitespace.

```yaml
# GOOD
- name: Create directory
  ansible.builtin.file:
    path: '/etc/nginx'
    state: 'directory'
    mode: '0755'

# BAD — tabs or inconsistent indentation
- name: Create directory
  ansible.builtin.file:
    path: '/etc/nginx'
	state: 'directory'  # tab character
```

### Jinja2

- Indent Jinja block tags to match the indentation level of the surrounding
  context.
- Use `{% %}` for control flow and `{{ }}` for expressions.
- Use `{# #}` for comments.

```jinja2
# GOOD — properly indented
{% for server in nginx__combined_servers %}
{%   if server.state | d('present') != 'absent' %}
server {
    listen {{ server.port }};
}
{%   endif %}
{% endfor %}

# BAD — inconsistent indentation
{% for server in nginx__combined_servers %}
{% if server.state | d('present') != 'absent' %}
server {
    listen {{ server.port }};
}
{% endif %}
{% endfor %}
```

## Variable naming

### Double-underscore namespace

All role variables use the role name followed by two underscores. This prevents
collisions and makes variable provenance immediately visible.

```yaml
# GOOD — double-underscore namespace
nginx__user: 'www-data'
nginx__worker_processes: 'auto'

# BAD — single underscore (legacy, deprecated)
nginx_user: 'www-data'
```

### Variable categories

| Pattern | Purpose | Example |
|---------|---------|---------|
| `role__var` | Configuration, overridable | `nginx__port: 443` |
| `role__default_var` | Role-provided safe default list/dict | `nginx__default_servers: []` |
| `role__group_var` | Per-inventory-group scope | `nginx__group_servers: []` |
| `role__host_var` | Per-host scope | `nginx__host_servers: []` |
| `role__combined_var` | Merged result (default + bare + group + host) | `nginx__combined_servers: '{{ ... }}'` |
| `role__dependent_var` | Consumed by another role | `nginx__ferm__dependent_rules: []` |
| `role__register_var` | Task register result | `nginx__register_packages` |
| `role__fact_var` | Runtime set_fact result | `nginx__fact_deploy_state` |
| `role__tpl_var` | Local-only, within a Jinja template | `nginx__tpl_ssl_path` |

### Inventory-level scoped variables

For each configuration property that users may want to set at different
inventory scopes, provide three variables following this pattern:

```yaml
# defaults/main.yml
nginx__servers: []           # All hosts in inventory
nginx__group_servers: []     # Per-group override
nginx__host_servers: []      # Per-host override

# Combined — merge all scopes
nginx__combined_servers: '{{
  nginx__default_servers +
  nginx__servers +
  nginx__group_servers +
  nginx__host_servers }}'
```

Users then set in their inventory:

```yaml
# inventory/group_vars/all/nginx.yml
nginx__servers:
  - name: 'global'

# inventory/group_vars/webservers/nginx.yml
nginx__group_servers:
  - name: 'group-specific'

# inventory/host_vars/nginx01/nginx.yml
nginx__host_servers:
  - name: 'host-specific'
```

### Combined variable pattern

Always merge in this order so that more specific scopes override less specific
ones:

```yaml
role__combined_thing: '{{
  role__default_thing +
  role__dependent_thing +
  role__thing +
  role__group_thing +
  role__host_thing }}'
```

## Default variables

### Structure and documentation

Default variables live in **exactly one file**: `defaults/main.yml`. Each
variable is preceded by a reStructuredText comment block that the `yaml2rst`
tool renders into documentation.

```yaml
# .. envvar:: nginx__port [[[
#
# The TCP port the nginx service should listen on.
# Set to ``443`` for HTTPS, ``80`` for HTTP.
nginx__port: '443'
                                                                     # ]]]
```

### Section grouping with fold markers

Use `# .. envvar::` anchors for individual variables and `# ]]]` closers.
Group related variables with section titles and fold markers:

```yaml
# APT packages [[[
# ----------------

# .. envvar:: nginx__base_packages [[[
nginx__base_packages:
  - [ 'nginx' ]
  - '{{ ["nginx-extras"] if (nginx__extras | bool) else [] }}'

                                                                     # ]]]
# .. envvar:: nginx__packages [[[
nginx__packages: []
                                                                     # ]]]
                                                                     # ]]]
# Network configuration [[[
# -------------------------

# .. envvar:: nginx__port [[[
nginx__port: '443'
                                                                     # ]]]
                                                                     # ]]]
```

### Dynamic defaults

Defaults are Jinja expressions, not static values. They adapt to the target
host environment:

```yaml
nginx__worker_processes: '{{ ansible_processor_cores | default(1) }}'
nginx__server_names_hash_bucket_size: '{{ 64 if ansible_architecture == "x86_64" else 32 }}'
```

### Safe undefined access

Always use `| d()` (default filter) when accessing variables that might not
be defined:

```yaml
nginx__enabled: '{{ ansible_local.nginx.enabled | d("False") | bool }}'
nginx__custom_packages: '{{ ansible_local.nginx.custom_packages | d([]) }}'
```

## Dependent variables (cross-role communication)

Dependent variables are the primary mechanism for roles to request
configuration from other roles without hard coupling.

### Provider role defines a dependent variable

```yaml
# roles/apt_preferences/defaults/main.yml
apt_preferences__dependent_list: []
```

### Consumer role populates it

```yaml
# roles/nginx/defaults/main.yml
nginx__apt_preferences__dependent_list:
  - package: 'nginx nginx-*'
    backports: [ 'bullseye', 'bookworm' ]
    by_role: 'debops.nginx'
```

### Playbook passes the value

```yaml
# playbooks/service/nginx.yml
- name: Manage nginx webserver
  hosts: [ 'debops_all_hosts', 'debops_service_nginx' ]
  become: True

  roles:

    - role: debops.apt_preferences
      apt_preferences__dependent_list:
        - '{{ nginx__apt_preferences__dependent_list }}'

    - role: debops.nginx
```

### Dependent variable contract

Each item in a dependent variable list SHOULD support:

- `state: present|absent` — whether to add or remove the configuration.
- `by_role: 'namespace.role'` — which role requested it (for debugging).

The provider role MUST handle multiple consumers without interference.

## Canonical task flow

Every role follows this structural flow in `tasks/main.yml`:

```yaml
---
# 1. Import DebOps global handlers
- name: Import DebOps global handlers
  ansible.builtin.import_role:
    name: 'global_handlers'
  tags: [ 'role::nginx', 'skip::nginx' ]

# 2. Import secret role (if secrets are needed)
- name: Import DebOps secret role
  ansible.builtin.import_role:
    name: 'secret'
  tags: [ 'role::nginx', 'skip::nginx' ]

# 3. Pre-tasks hook (allows playbook injection)
- name: DebOps pre_tasks hook
  ansible.builtin.include_tasks: "{{ lookup('debops.debops.task_src', 'nginx/pre_main.yml') }}"
  tags: [ 'role::nginx', 'skip::nginx' ]

# 4. Validate environment
- name: Check required tools
  ansible.builtin.command: 'which openssl'
  changed_when: false
  check_mode: false

# 5. Install packages
- name: Install nginx packages
  ansible.builtin.apt:
    name: '{{ q("flattened", (nginx__base_packages + nginx__packages)) }}'
  when: nginx__deploy_state in ['present']
  register: nginx__register_packages
  until: nginx__register_packages is succeeded
  tags: [ 'role::nginx', 'skip::nginx' ]

# 6. Create directories
- name: Create configuration directories
  ansible.builtin.file:
    path: '/etc/nginx'
    state: 'directory'
    mode: '0755'
  tags: [ 'role::nginx', 'skip::nginx' ]

# 7. Copy static files
- name: Deploy static files
  ansible.builtin.copy:
    src: 'etc/nginx/'
    dest: '/etc/nginx/'
  tags: [ 'role::nginx', 'skip::nginx' ]

# 8. Template configuration
- name: Generate nginx configuration
  ansible.builtin.template:
    src: 'etc/nginx/nginx.conf.j2'
    dest: '/etc/nginx/nginx.conf'
  notify: [ 'Reload nginx' ]
  tags: [ 'role::nginx', 'skip::nginx' ]

# 9. Manage service
- name: Ensure nginx is running
  ansible.builtin.systemd:
    name: 'nginx'
    state: 'started'
    enabled: true
  tags: [ 'role::nginx', 'skip::nginx' ]

# 10. Save local facts
- name: Save nginx local facts
  ansible.builtin.template:
    src: 'etc/ansible/facts.d/nginx.fact.j2'
    dest: '/etc/ansible/facts.d/nginx.fact'
    mode: '0755'
  tags: [ 'role::nginx', 'skip::nginx' ]

# 11. Flush handlers
- name: Flush handlers
  ansible.builtin.meta: 'flush_handlers'
  tags: [ 'role::nginx', 'skip::nginx' ]

# 12. Post-tasks hook
- name: DebOps post_tasks hook
  ansible.builtin.include_tasks: "{{ lookup('debops.debops.task_src', 'nginx/post_main.yml') }}"
  tags: [ 'role::nginx', 'skip::nginx' ]
```

## Task writing

### YAML-native syntax

Always use native YAML syntax for task definitions. Never use the inline
`key=value` format.

```yaml
# GOOD
- name: Create plugin path
  ansible.builtin.file:
    path: '/etc/nginx/plugins'
    state: 'directory'

# BAD — key=value format
- name: Create plugin path
  file: path=/etc/nginx/plugins state=directory
```

### Meaningful names

Every task and every `include_tasks` MUST have a `name:` that describes what
it does and why.

```yaml
- name: Check if the opasswd database exists
  ansible.builtin.stat:
    path: '/etc/security/opasswd'
  register: nginx__register_opasswd

- name: Configure ACME support
  ansible.builtin.include_tasks: 'acme.yml'
  when: nginx__acme | bool
```

### Conditions via default variables

Complex conditions go in `defaults/main.yml`, not inline. This lets users
override the decision.

```yaml
# In defaults/main.yml:
nginx__use_pki: '{{ nginx__pki_realm in ansible_local.pki.known_realms | d([]) }}'

# In tasks/main.yml:
- name: Configure SSL certificates
  ansible.builtin.include_tasks: 'ssl.yml'
  when: nginx__use_pki | bool
```

### Tags

Every task in a role carries two base tags:
- `role::<name>` — to run all tasks for a role.
- `skip::<name>` — to skip all tasks for a role (when combined with the
  enabled guard).

Fact deployment tasks use an additional tag:
- `meta::facts` — allows targeting or skipping fact collection independently.

```yaml
- name: Install nginx packages
  ansible.builtin.apt:
    name: '{{ nginx__base_packages | flatten }}'
  when: nginx__deploy_state in ['present']
  tags: [ 'role::nginx', 'skip::nginx' ]

- name: Save nginx local facts
  ansible.builtin.template:
    src: 'etc/ansible/facts.d/nginx.fact.j2'
    dest: '/etc/ansible/facts.d/nginx.fact'
    mode: '0755'
  notify: ['Refresh host facts']
  tags: [ 'meta::facts' ]
```

### Idempotence

Every task must be idempotent. Use module features to ensure this:

```yaml
# Use creates: on commands
- name: Generate Diffie-Hellman parameters
  ansible.builtin.command:
    cmd: 'openssl dhparam -out {{ nginx__dhparam_path }} {{ nginx__dhparam_bits }}'
    creates: '{{ nginx__dhparam_path }}'

# Use force: no on copy tasks
- name: Deploy static configuration
  ansible.builtin.copy:
    src: 'nginx.conf'
    dest: '/etc/nginx/nginx.conf'
    force: no

# Use changed_when: for idempotent shell operations
- name: Run hook scripts
  ansible.builtin.command: 'run-parts {{ nginx__hook_path }}'
  register: nginx__register_hooks
  changed_when: nginx__register_hooks.changed | bool
```

### No debug or ignore_errors in production

Never use `debug` or `ignore_errors` unconditionally in production code. These
modules are meant for debugging only, and might cause unintended consequences
like exposing secrets or destroying infrastructure. Leaving them in production
code means that somebody did not pay enough attention.

## Package installation

### Package list with conditional inclusion

Packages lists use nested lists with conditional elements. Each conditionally
included package is wrapped in a single-element list (or empty list):

```yaml
nginx__base_packages:
  - [ 'nginx' ]
  - '{{ ["nginx-extras"] if (nginx__extras | bool) else [] }}'
  - '{{ ["libnginx-mod-http-geoip"] if (nginx__geoip | bool) else [] }}'
```

### Install with register + until

```yaml
- name: Install nginx packages
  ansible.builtin.apt:
    name: '{{ q("flattened", (nginx__base_packages + nginx__packages)) }}'
  register: nginx__register_packages
  until: nginx__register_packages is succeeded
  when: nginx__deploy_state in ['present']
```

Both `q("flattened", ...)` (the `lookup` alias) and `| flatten` are acceptable.
`q("flattened")` handles deeply nested lists more reliably; `| flatten` is more
readable for simple cases. Use whichever fits the context.

## Jinja templates

### Template path convention

Template paths mirror the target filesystem path and end in `.j2`:

```
templates/etc/nginx/nginx.conf.j2         → /etc/nginx/nginx.conf
templates/etc/ansible/facts.d/nginx.fact.j2 → /etc/ansible/facts.d/nginx.fact
```

### Template header

Every template starts with a SPDX copyright header and `ansible_managed`:

```jinja2
{# Copyright (C) 2024 Maciej Delmanowski <drybjed@gmail.com>
 # Copyright (C) 2024 DebOps <https://debops.org/>
 # SPDX-License-Identifier: GPL-3.0-only
 #}
# {{ ansible_managed }}
```

### Configuration file template

Iterate over structured data with proper indentation:

```jinja2
{% for server in nginx__combined_servers %}
{%   if server.state | d('present') not in ['absent', 'ignore'] %}
server {
    listen {{ server.port | d(443) }} ssl;
    server_name {{ server.name }};
    {{ server.extra_config | d('') }}
}
{%   endif %}
{% endfor %}
```

### Local facts template (Python)

All local fact scripts use the Python executable format. Pure JSON fact files
(`/etc/ansible/facts.d/*.fact` without a shebang) are obsolete — Python scripts
support runtime introspection and consistent error handling.

Every fact script follows this structure:

```jinja2
#!{{ ansible_python['executable'] }}
# -*- coding: utf-8 -*-

# {{ ansible_managed }}

from __future__ import print_function
from json import dumps
import os


def cmd_exists(cmd):
    return any(
        os.access(os.path.join(path, cmd), os.X_OK)
        for path in os.environ["PATH"].split(os.pathsep)
    )


output = {'installed': cmd_exists('service_binary')}

print(dumps(output, sort_keys=True, indent=4))
```

The `cmd_exists()` helper is the standard way to detect whether a service's
binary is installed on the target host.

### Complex fact script with Jinja data injection

For facts that need Jinja-expanded variable values combined with runtime
introspection, use Jinja to generate the Python data structure and let the
Python part handle runtime queries:

```jinja2
#!{{ ansible_python['executable'] }}
# -*- coding: utf-8 -*-

# {{ ansible_managed }}

from __future__ import print_function
from json import loads, dumps
import subprocess
import os


def cmd_exists(cmd):
    return any(
        os.access(os.path.join(path, cmd), os.X_OK)
        for path in os.environ["PATH"].split(os.pathsep)
    )


output = loads('''{{ {
    "enabled": (nginx__deploy_state == "present"),
    "known_modules": nginx__tpl_known_modules,
} | to_nice_json }}''')

result = subprocess.run(
    ['dpkg-query', '-W', '-f', '${Version}', 'nginx'],
    capture_output=True, text=True)
output['installed_version'] = result.stdout.strip()

print(dumps(output, sort_keys=True, indent=4))
```

Key technique: Jinja bakes Ansible variable values into the Python script at
template-render time, and the Python code handles runtime queries on the
target host. This keeps fact templates simple while supporting rich
introspection and linting.

### Protecting non-Jinja syntax in fact scripts

When a fact script must contain template syntax from another language
(e.g. Go-style `{{ .Names }}` in Docker commands), wrap it in
`{% raw %}...{% endraw %}` to prevent Jinja interpretation:

```jinja2
ps_output = subprocess.check_output(
    ['docker', 'ps', '-a',
     '--format', '{% raw %}{{.Names}}\t{{.Image}}\t{{.Status}}{% endraw %}'],
    stderr=subprocess.DEVNULL
).decode('utf-8')
```

Without `{% raw %}`, Jinja would try to evaluate `{{ .Names }}` as an Ansible
variable, causing a template error.

### Built-in Jinja filters to know

| Filter | Use case |
|--------|----------|
| `| d(value)` | Default for undefined variables |
| `| bool` | Normalize truthy values |
| `| bool \| lower` | Output `true`/`false` strings (Ansible convention) |
| `| to_nice_json` | Serialize complex structures |
| `| string` | Safe concatenation |
| `| flatten` | Flatten nested lists |
| `| unique` | Deduplicate lists |
| `| sort` | Sort lists |
| `| join(',')` | Join list to string |
| `| ipaddr('address')` | Extract IP from CIDR (requires `ipaddr` filter plugin) |

## Directory tree deployment

### with_community.general.filetree

For roles that manage entire configuration directory trees (not just individual
files), use the `filetree` lookup to walk a source directory and mirror its
structure on the target host:

```yaml
- name: Create subdirectories from config tree
  ansible.builtin.file:
    path: '{{ dest + "/" + item.path }}'
    state: 'directory'
    mode: '{{ item.mode | d("0755") }}'
  with_community.general.filetree:
    - '{{ source_path }}'
  when: item.state == 'directory'

- name: Generate files from config tree
  ansible.builtin.template:
    src: '{{ item.src }}'
    dest: '{{ dest + "/" + item.path }}'
    mode: '{{ item.mode | d("0644") }}'
  with_community.general.filetree:
    - '{{ source_path }}'
  when: item.state == 'file'
  register: config_tree_changes
```

Key details:
- `item.state` is `'directory'`, `'file'`, or `'link'` — filter on it to
  handle each type separately.
- `item.src` points to the source file inside the filetree (for `template`).
- `item.path` is the relative path within the tree.
- `item.mode` carries the source file's permissions.
- Track changes via `register` to restart services when config changes.

This pattern is used by `docker_service` for deploying per-service config
directories and by `nixos` for deploying custom NixOS configuration trees.

## Local facts (state sharing)

### Facts as API

Local facts are the public API of a role. Other roles read them via
`ansible_local.<role>.<key>`.

```yaml
# Consumer role checks provider state:
- name: Check if nginx is enabled
  ansible.builtin.set_fact:
    nginx_available: '{{ ansible_local.nginx.enabled | d("False") | bool }}'
```

### Always set a fact

Every role should save a local fact file so downstream roles can discover its
state:

```yaml
- name: Save nginx local facts
  ansible.builtin.template:
    src: 'etc/ansible/facts.d/nginx.fact.j2'
    dest: '/etc/ansible/facts.d/nginx.fact'
    mode: '0755'
```

### Reading facts safely

Always use `| d(default)` when accessing another role's facts — the role may
not have run yet:

```yaml
nginx__use_pki: '{{ ansible_local.pki.known_realms | d([]) }}'
firewall_enabled: '{{ ansible_local.ferm.enabled | d("unknown") }}'
```

## Handlers

### Two-tier handler system

1. **Per-role handlers** — for service-specific notifications (restart, reload).
2. **Global handlers** — centralized handlers shared across roles, imported via
   the `global_handlers` role.

```yaml
# handlers/main.yml (per-role)
- name: Reload nginx
  ansible.builtin.systemd:
    name: 'nginx'
    state: 'reloaded'
```

### Notify by descriptive name

```yaml
- name: Generate nginx configuration
  ansible.builtin.template:
    src: 'nginx.conf.j2'
    dest: '/etc/nginx/nginx.conf'
  notify: [ 'Reload nginx' ]
```

### Flush handlers after facts

After saving local facts, flush handlers so dependent roles see fresh data:

```yaml
- name: Save nginx local facts
  ansible.builtin.template:
    src: 'nginx.fact.j2'
    dest: '/etc/ansible/facts.d/nginx.fact'

- name: Flush handlers to pick up facts changes
  ansible.builtin.meta: 'flush_handlers'
```

## Playbooks

### Host group target

Playbooks target a combination of the universal group and the service-specific
group:

```yaml
- name: Manage nginx webserver
  hosts: [ 'debops_all_hosts', 'debops_service_nginx' ]
```

The `debops_all_hosts` group ensures the playbook runs on managed hosts even
before the service group is assigned. The service group filters to only the
relevant hosts.

### Environment variable merging

Merge inventory-defined environment variables at three tiers:

```yaml
  environment: '{{ inventory__environment | d({})
                   | combine(inventory__group_environment | d({}))
                   | combine(inventory__host_environment  | d({})) }}'
```

### Tags on every role

```yaml
  roles:
    - role: debops.apt_preferences
      tags: [ 'role::apt_preferences', 'skip::apt_preferences' ]
      apt_preferences__dependent_list:
        - '{{ nginx__apt_preferences__dependent_list }}'

    - role: debops.nginx
      tags: [ 'role::nginx', 'skip::nginx' ]
```

The `skip::<name>` tag allows skipping a role entirely without removing it
from the playbook:

```bash
ansible-playbook site.yml --skip-tags 'skip::nginx'
```

### Soft dependency injection

Place dependent roles before the consumer role in the playbook, passing their
dependent variables:

```yaml
- name: Manage nginx webserver
  hosts: [ 'debops_all_hosts', 'debops_service_nginx' ]
  become: True

  environment: '{{ inventory__environment | d({})
                   | combine(inventory__group_environment | d({}))
                   | combine(inventory__host_environment  | d({})) }}'

  roles:

    - role: debops.ferm
      tags: [ 'role::ferm', 'skip::ferm' ]
      ferm__dependent_rules:
        - '{{ nginx__ferm__dependent_rules }}'

    - role: debops.apt_preferences
      tags: [ 'role::apt_preferences', 'skip::apt_preferences' ]
      apt_preferences__dependent_list:
        - '{{ nginx__apt_preferences__dependent_list }}'

    - role: debops.nginx
      tags: [ 'role::nginx', 'skip::nginx' ]
```

### Pre-tasks for environment setup

Use `pre_tasks` when a role needs secret directories or environment
preparation before the role runs:

```yaml
  pre_tasks:
    - name: Prepare nginx environment
      ansible.builtin.import_role:
        name: 'nginx'
        tasks_from: 'main_env'
      tags: [ 'role::nginx' ]

  roles:
    - role: debops.secret
      tags: [ 'role::secret' ]
      secret_directories:
        - '{{ nginx__env_secret_directories }}'

    - role: debops.nginx
      tags: [ 'role::nginx', 'skip::nginx' ]
```

## Role dependencies

### Soft dependencies (playbook) PREFERRED

Declare dependencies in the playbook's `roles:` list, not in `meta/main.yml`.
This gives playbook authors control over ordering and conditional execution.

### Hard dependencies (meta/main.yml) AVOIDED

Hard dependencies force installation and execution of another role regardless
of context. The only accepted exception is the `secret` role, which defines a
path used by the `lookup('password')` plugin and must be available everywhere.

```yaml
# meta/main.yml — keep dependencies empty
dependencies: []
```

### Transitive dependencies

When role A depends on role B and B depends on C, list all three in the
playbook:

```yaml
  roles:
    - role: debops.ferm       # needed by nginx
    - role: debops.apt_preferences  # needed by nginx
    - role: debops.nginx
```

## Security

- **No secrets in variables**. Use the `secret` role to resolve a secure path
  and `lookup('password')` for generating credentials.
- **Safe default filter**: always use `| d()` when accessing variables that
  may be undefined — prevents crashes and avoids information leaks through
  errors.
- **No ignore_errors in production**. If a task can fail without breaking
  idempotency, use `failed_when:` with an explicit condition instead.
- **No hardcoded passwords or API keys** anywhere in roles, playbooks, or
  templates.
- **Parameterized no_log**: use `no_log: '{{ debops__no_log | d(true) }}'` for
  tasks handling secrets, not hardcoded `no_log: true`, so users can control
  logging verbosity via inventory.
- **SUID/SGID never set** on generated scripts.
- **Idempotent operations only** — never run commands that modify state without
  `creates:`, `force: no`, or `changed_when:` guards.

```yaml
# GOOD — idempotent, no secrets in code
- name: Set database password
  ansible.builtin.set_fact:
    db_password: '{{ lookup("password", secret + "/db/password chars=hex") }}'

# BAD — hardcoded secret
- name: Set database password
  ansible.builtin.set_fact:
    db_password: 'hunter2'
```

## Python compatibility (facts scripts)

Local fact scripts use Python 3 only. Python 2 is EOL since 2020 and is not
supported.

```python
#!{{ ansible_python['executable'] }}
# Always use print() with brackets
print("output")

# Use list(dict) not dict.keys() for Jinja compatibility
keys = list(output)
```

## Verification workflow

Before committing any Ansible role or playbook changes, run:

```bash
# 1. Lint the role
ansible-lint roles/<name>/

# 2. Validate YAML syntax
yamllint roles/<name>/

# 3. Syntax-check playbooks
ansible-playbook --syntax-check playbooks/service/<name>.yml

# 4. Pre-commit (if configured)
pre-commit run --all-files
```

### ansible-lint configuration

```yaml
# .ansible-lint
skip_list:
  - 'fqcn[keyword]'        # Needed for collection-based task_src lookups
  - 'run-once[task]'       # False positive with free strategy
warn_list: []
enable_list:
  - 'fqcn-builtins'        # Require FQCN for built-in modules
  - 'no-log-password'      # Flag password exposure
  - 'no-same-owner'        # Flag ownership issues
```

### yamllint configuration

```yaml
# .yamllint
extends: default
rules:
  indentation:
    spaces: 2
    indent-sequences: true
  line-length: disable
  truthy: disable
  comments-indentation: false
```

## Summary checklist for new roles

Before publishing a new role, verify:

- [ ] `defaults/main.yml` uses `role__variable` double-underscore naming.
- [ ] All configurable values are in `defaults/main.yml` (no `vars/`).
- [ ] Inventory-level scoped variables follow the `role__thing` / `role__group_thing` / `role__host_thing` pattern.
- [ ] Combined variable merges all four tiers (default + bare + group + host).
- [ ] Dependent variables declared for cross-role features (firewall, APT prefs, etc.).
- [ ] Every task has a meaningful `name:` and carries `role::<name>` / `skip::<name>` tags.
- [ ] Native YAML syntax used (not key=value).
- [ ] All tasks are idempotent (uses `creates:`, `force: no`, or `changed_when:`).
- [ ] No `debug` or `ignore_errors` in production tasks.
- [ ] Package installation uses register + until pattern.
- [ ] Local facts saved to `/etc/ansible/facts.d/`.
- [ ] Handlers flushed after fact updates.
- [ ] Template paths mirror target filesystem + `.j2`.
- [ ] Templates include SPDX header and `ansible_managed`.
- [ ] `collections: ['debops.debops']` set in `meta/main.yml`.
- [ ] `dependencies: []` in `meta/main.yml` (empty — use soft deps via playbook).
- [ ] ansible-lint passes with configured skip-list.
- [ ] yamllint passes.
- [ ] `ansible-playbook --syntax-check` passes.
