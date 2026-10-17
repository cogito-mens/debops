---
name: jinja2-programmer
description: Jinja2 template style — falsy-safe existence checks, macro conventions, indentation, whitespace control, comment rendering, serialization bridges (loads/to_nice_json), and DebOps-specific template structure. Use when writing or reviewing .j2 templates for Ansible roles.
license: GPL-3.0-or-later
---

# Jinja2 coding style

A Jinja2 template style guide for AI-assisted programming. Covers conventions
and patterns used across the [DebOps](https://debops.org/) project for Ansible
role templates, local fact scripts, and configuration file generation.

> **Important**: Jinja2 templates are evaluated by Ansible on target hosts
> (servers, containers, CI runners), not in the OpenCode execution environment.
> The templates themselves are source files — always consider what variables
> are available at render time, what their types are, and whether fallback
> defaults (`| d()`) are needed.

## Human readability first

Prefer clarity over brevity. Templates are read more often than they are
rendered. Choose the form that makes intent obvious, even if more verbose.

## Copyright / License header

Every template gets an SPDX-format header, followed by `{{ ansible_managed }}`.

### Plain Jinja2 template (.j2)

```jinja2
{# Copyright (C) 2015-2018 Maciej Delmanowski <drybjed@gmail.com>
 # Copyright (C) 2015-2018 DebOps <https://debops.org/>
 # SPDX-License-Identifier: GPL-3.0-only
 #}
# {{ ansible_managed }}
```

### Python fact script (.fact.j2)

```jinja2
#!{{ ansible_python['executable'] }}
# -*- coding: utf-8 -*-

# Copyright (C) 2015-2018 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2015-2018 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-only

# {{ ansible_managed }}
```

### Config file with native comment syntax (PHP, Erlang, etc.)

```jinja2
# {{ ansible_managed }}

// {{ ansible_managed }}

%% {{ ansible_managed }}
```

### Rules

- Use `GPL-3.0-only` or `GPL-3.0-or-later` as appropriate.
- Keep the header brief — 2-4 lines maximum, plus `ansible_managed`.
- `ansible_managed` goes on its own line after the copyright block, using the
  config file's native comment character.
- For Python fact scripts: shebang on line 1, encoding on line 2, blank line,
  copyright, then `ansible_managed`.

## Template indentation

Align `{%` and `{{` at **column 0** — both are tag markers, not content.
Indent the keyword inside `{% %}` by `2N+1` spaces and the expression
inside `{{ }}` by `2N+3` spaces at block level N. The expression sits 2
columns deeper than the keyword (`2N+5` vs `2N+3`), visually showing that
the output belongs *inside* the block without introducing leading whitespace
in the rendered file.

```jinja2
{% macro parse_value(value) %}
{%   if value | bool and value is not iterable %}
{{     '1' -}}
{%   elif not value | bool and value is not iterable %}
{%     if value is not none %}
{%       if value | int or value | string == '0' %}
{{         value | string -}}
{%       else %}
{{         '0' -}}
{%       endif %}
{%     endif %}
{%   elif value is string %}
{{     value -}}
{%   endif %}
{% endmacro %}
```

### Rules

- `{%` and `{{` are always at column 0.
- Inside `{% %}`, keyword indent is `2N+1` spaces at block level N:
  `{% if` (N=0, keyword col 3), `{%   if` (N=1, col 5),
  `{%     if` (N=2, col 7).
- Inside `{{ }}`, expression indent is `2N+3` spaces at block level N:
  `{{   expr }}` (N=0, expr col 5), `{{     expr }}` (N=1, col 7),
  `{{       expr }}` (N=2, col 9).
- The expression is always 2 columns deeper than the `{% %}` keyword at the
  same nesting level.
- `{# #}` comments at the same indent as the block they document.
- Matching `if/elif/else/endif` share the same indent level.

### Rationale

- Nesting output 2 columns deeper than the control keyword visually
  distinguishes the rendered value from the block it belongs to.
- `{{` at column 0 avoids leading whitespace in rendered config files — no
  `-%}` stripping needed.
- Jinja2 ignores extra whitespace inside `{{ }}`, so the `2N+3` padding
  before the expression is purely cosmetic.
- Left-trim (`{%-`) is never used.

### Exception: macro calls inside `{{ }}`

Macro calls that produce output use `{{ name(...) -}}` with whitespace trim:

```jinja2
{{ parse_value(element.value) -}}
```

The `-}}` suppresses the trailing newline so the calling template controls
line breaks.

## Template file conventions

### Naming

Templates live under `templates/` in each Ansible role. The path after
`templates/` mirrors the destination path on the target host:

```
templates/etc/sysctl.d/parameters.conf.j2  →  /etc/sysctl.d/parameters.conf
templates/etc/ansible/facts.d/sysctl.fact.j2  →  /etc/ansible/facts.d/sysctl.fact
```

### No template inheritance

DebOps does **not** use `{% extends %}`, `{% block %}`, or `{% include %}`.
Template reuse is achieved through:
- **Lookup plugins**: `lookup('template', ...)` for dynamic rendering
- **Heredoc pattern** (in generated shell scripts): `readarray -t var << EOF`
- **Inline macros** defined at the top of each template (see Macro conventions)

## Variable access patterns

### Safe defaults with `| d()`

The `| d()` filter is the most-used filter in DebOps templates (3000+ uses).
Always use it for optional keys — never assume a variable is defined:

```jinja2
{{ element.option | d(element.name) }}
{{ element.state | d('present') }}
{{ ansible_local.pki.known_realms | d() }}                    # default to empty
{{ ansible_local.apache.version | d("2.4.0") }}
```

### Boolean coercion with `| bool`

Always coerce to boolean with `| bool` for conditional tests. Never rely on
a string like `"True"` or arbitrary truthiness:

```jinja2
{% if unattended_upgrades__enabled | bool %}
{% if (item.start | d(True)) | bool %}
{% set filter_undef = kwargs.filter_undef | d(True) | bool %}
```

### Combined default chaining

The standard pattern for boolean-option variables:

```jinja2
{% if item.softfail | d(False) | bool %}
{% if item.separator | d(True) | bool %}
```

Read right-to-left: default → coerce.

### Ternary for conditional values

```jinja2
{{ ntp__servers_as_pool | ternary('pool', 'server') }} {{ address }} iburst
{{ apache__status_extended_enabled | bool | ternary("On", "Off") }}
"rule_state": ((item.network.node_reachable | d(True) | bool) | ternary("present", "absent"))
```

### String/numeric coercion

```jinja2
{{ value | string }}
{{ value | int }}
```

Chained for safe conversion:
```jinja2
{% if value | int or value | string == '0' %}
```

## Falsy value handling

This is the most common pitfall in Jinja2 templates. See the dedicated
reference at `~/.config/opencode/memory/patterns/jinja2-falsy-values.md` for
full details.

### The problem

`{% if element.value | d() %}` treats `0`, `False`, and `""` the same as
"undefined" — the `| d()` filter returns the value when defined, but
`0 | bool` is `False`, so the `if` block is skipped.

### Pattern A: `is defined` (gold standard)

```jinja2
{% if element.value is defined %}
  {# value exists, even if 0, False, or "" #}
{% else %}
  {# key is truly absent #}
{% endif %}
```

Use this when `0` / `False` / `""` are valid values.

### Pattern B: `defined and | bool` (combined check)

```jinja2
{% if element.separator is defined and element.separator | bool %}
```

Use when you need to verify "key exists AND is truthy".

### Pattern C: `| d(False) | bool` (boolean flags only)

```jinja2
{% if element.softfail | d(False) | bool %}
```

Safe when absent is semantically equivalent to `False`. Use for boolean-option
variables where there's no meaningful distinction between "absent" and "False".

### Pattern to avoid

```jinja2
{% if element.value | d() %}       {# BROKEN: treats 0, False, "" as absent #}
```

Only safe when the value is guaranteed to never be `0`, `False`, or `""`.

## Whitespace control

Use `-%}` for right-trim sparingly. Never use `{%-` for left-trim — it is not
used anywhere in DebOps templates.

### When to right-trim

**Macro definitions** — suppress the trailing newline so the caller controls
line breaks:

```jinja2
{% macro get_apache_version() -%}
{{ ansible_local.apache.version | d("2.4.0") -}}
{% endmacro %}
```

**End of `for` blocks** — prevent trailing blank lines in the rendered output:

```jinja2
{% for key, value in items -%}
{{ key }}="{{ value }}"
{% endfor -%}
```

**Comment blocks with whitespace trim** — suppress the newline after the
comment content so the next output appears on the same logical line:

```jinja2
{{ element.comment | regex_replace('\n$','') | comment(prefix='', postfix='') -}}
```

### When NOT to trim

- Inside config formatting macros where newlines between entries are desired.
- Around `{% if %}` / `{% endif %}` blocks that conditionally include entire
  sections — the natural newlines produce clean output.
- `{# #}` comments do not produce whitespace in the output — no trimming
  needed.

## Comment rendering

### Standard pattern

Use the `comment` filter with explicit `prefix=''` and `postfix=''` to avoid
extra blank lines:

```jinja2
{{ item.comment | regex_replace('\n$','') | comment(prefix='', postfix='') -}}
```

The `regex_replace('\n$','')` strips trailing newlines from the comment text
before the filter processes it, preventing double-newline artifacts.

### Multi-line YAML text blocks

Comments are stored as YAML text blocks (`|`) in the role defaults and passed
to the template via `item.comment`. The `comment` filter handles multi-line
strings correctly — each line gets the decoration character prepended.

### Different decoration styles

| Comment style | Template | Rendered |
|---|---|---|
| `#` (shell, Python, config) | `\| comment(prefix='', postfix='')` | `# text` |
| `# ` with space decoration | `\| comment(prefix='', decoration='# ', postfix='')` | `# text` |
| `%%` (Erlang) | `\| comment(decoration='    %% ', prefix='', postfix='')` | `    %% text` |
| `//` (PHP, C) | `\| comment(prefix='', decoration='// ', postfix='')` | `// text` |
| Multi-line header | `\| comment(prefix='#', postfix='#', decoration='# ')` | `# text #` |

### Conditional rendering

Only render comments when they exist:

```jinja2
{% if item.comment | d() %}
{{ item.comment | regex_replace('\n$','') | comment(prefix='', postfix='') }}
{% endif %}
```

## Macro conventions

Macros are the primary code-reuse mechanism in DebOps templates
(178+ macros across ~60 files). Each role typically defines one or more
role-specific macros.

### The `parse_value` pattern (canonical)

The most common macro: safely coerce a Python/Jinja2 value to a string
appropriate for the config file format. Example from the sysctl role:

```jinja2
{% macro parse_value(value) %}
{%   if value | bool and value is not iterable %}
{{     '1' -}}
{%   elif not value | bool and value is not iterable %}
{%     if value is not none %}
{%       if value | int or value | string == '0' %}
{{         value | string -}}
{%       else %}
{{         '0' -}}
{%       endif %}
{%     endif %}
{%   elif value is string %}
{{     value -}}
{%   endif %}
{% endmacro %}
```

This handles the truth table for sysctl values: `True` → `"1"`, `False` → `"0"`,
integer → its string, string → as-is, `None` → empty.

### Config formatting macros

Most roles define a `print_config(config)` or similar macro that iterates over
a structured list and renders each entry:

```jinja2
{% macro print_config(config) %}
{%   for element in config %}
{{     element.option }} = {{ element.value }}
{%   endfor %}
{% endmacro %}
```

Usage:
```jinja2
{{ print_config(php__tpl_config) }}
```

### Vim fold markers

Macros can use `{# [[[ #}` and `{# ]]] #}` markers for folding in editors
that support it:

```jinja2
{% macro parse_value(value) %}{# [[[ #}}
{%   if value | bool and value is not iterable %}
{{     '1' -}}
{%   endif %}
{% endmacro %}{# ]]] #}
```

### Rules

- Each macro has a single purpose.
- Keep macros short (5-15 lines). If a macro grows beyond 20 lines, consider
  breaking it into smaller helpers.
- Use descriptive names: `print_config`, `parse_value`, `merge_dict`.
- Always provide a default for the return value when the macro might produce
  nothing: `{{ result | d('') }}`.

## Template structure pattern

Most DebOps config templates follow the same structure. Example template
layout (`parameters.conf.j2`):

```jinja2
{# Copyright ... #}
# {{ ansible_managed }}

{% macro parse_value(value) %}
  ...
{% endmacro %}

{# File-level variables and state trackers #}
{% set param_tracker = [] %}
{% set tracker = {'seen_comment': False} %}

{# Iterate over the options list #}
{% for element in item.options %}
{%   if element.state | d('present') not in ['absent', 'ignore', 'init'] %}

{#     Optional per-element comment #}
{%     if element.comment | d() %}
{{       element.comment | regex_replace('\n$','') | comment(prefix='', postfix='') -}}
{%     endif %}

{#     Check writability #}
{%     if not is_writable %}
# Read-only parameter.
{%     endif %}

{#     Render the parameter #}
{%     if element.value is defined %}
{{       '{}{} = {}'.format(commented_entry, param_name, parse_value(element.value)) }}
{%     else %}
{{       '{}{}'.format(commented_entry, param_name) }}
{%     endif %}
{%   endif %}
{% endfor %}
```

### The three-phase pattern

1. **Setup phase**: Macros, file-level variables, state trackers (`{% set %}`).
2. **Configuration phase**: Default/mandatory entries, dependent configuration
   (often via `lookup('flattened', ...)` or `combine()`).
3. **Render phase**: Single `{% for element in item.options %}` loop that
   handles: state filtering → writability check → value rendering.

### Role-level defaults structure

Role `defaults/main.yml` defines structured YAML lists that templates
consume. Each entry typically has:

```yaml
sysctl__default_parameters:
  - name: 'net.ipv4.ip_forward'
    value: False
    comment: |
      Enable IP forwarding for hosts that act as routers...
  - name: 'net.ipv4.ping_group_range'
    value: '0 {{ ansible_local.sysctl.ping_group_max | d("2147483647") }}'
    softfail: True
    comment: |
      Allow unprivileged ping from containers...
```

Template-side variable resolution follows the DebOps role variable
hierarchy: `role__combined_var` → `role__group_var` → `role__host_var` →
`role__dependent_var` → `instance` + `default` variables.

## Serialization bridge

The `| to_nice_json` / `| to_nice_yaml` filters are used to serialize
complex data structures in templates, especially for embedding structured
data inside generated scripts.

### Direct output in config files

```jinja2
# YAML config file — serialize a dict
{{ sysfs__tpl_dependent_attributes | to_nice_json }}

# With indent
{{ q("flattened", item.config) | to_nice_yaml(indent=2) }}
```

### Python fact scripts — the `loads()` pattern

The canonical way to embed Jinja2-structured data into a Python fact script.
The template renders a JSON string inside triple-quotes, then Python's
`json.loads()` deserializes it at runtime:

```jinja2
output = loads("""{{ {'configured': True,
                      'enabled': sysctl__enabled | bool,
                      'writable': []} | to_nice_json }}""")
```

For complex pre-built variables:

```jinja2
output = loads('''{{ core__tpl_facts | to_nice_json }}''')
```

### PHP `json_decode()`

Same pattern in PHP config templates:

```php
$CONFIG = json_decode('{{ owncloud__tpl_active_config_dict | to_nice_json }}', true);
```

### Python `json.loads()` in Python config files

```python
LOGGING = json.loads('''{{ netbox__config_logging | to_nice_json }}''')
```

### Workaround for Jinja2 in Python scripts

When a fact script needs Jinja2 control flow to build its data, use a
triple-quoted string as a workaround:

```jinja2
# Workaround to allow Jinja template inside of a Python script
"""
{% set core__tpl_facts = {} %}
{% if not core__reset_facts | d(False) %}
...
"""
output = loads('''{{ core__tpl_facts | to_nice_json }}''')
```

### Re-parsing with `from_yaml`

In macros that need to manipulate serialized data, use `from_yaml` to parse
YAML strings back into Jinja2 data structures:

```jinja2
{% for domain in (get_yaml_list_for_elem(domains) | from_yaml) %}
```

This round-trips through serialization to create a deep copy, avoiding
reference-sharing bugs with nested dicts.

## Error handling in fact scripts

Fact scripts are Python code generated from Jinja2 templates. Error handling
uses **Python** `try/except` — there are no `{% try %}` / `{% except %}`
Jinja2 extensions in DebOps.

### Graceful degradation with `pass`

```jinja2
try:
    with open('/proc/self/gid_map', 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) == 3 and parts[0] == '0':
                output['ping_group_max'] = int(parts[2]) - 1
                break
except Exception:
    pass
```

### Broad exception handling

Fact scripts run at the start of every playbook — they must never crash, even
on unusual systems. Use broad `except` clauses:

```jinja2
try:
    ...
except (FileNotFoundError, PermissionError):
    ...
    try:
        ...
    except (FileNotFoundError, PermissionError):
        ...
```

### Rules

- **Fact scripts**: `except Exception: pass` is acceptable — the script must
  produce output even on partial failure.
- **Config templates**: No error handling needed — use `| d()` for safe
  defaults instead.

## Lookup plugins in templates

`lookup()` calls inside `.j2` templates fetch external data at render time.

### List flattening (most common)

```jinja2
{% set ferm__tpl_dependent_rules = lookup('flattened', ferm__dependent_rules) %}
```

The `q("flattened", ...)` shorthand is equivalent:

```jinja2
{{ q("flattened", item.config) | to_nice_yaml(indent=2) }}
```

### Password/secret generation

```jinja2
MINIO_ROOT_USER="{{ item.root_user | d(lookup('password', secret + '/minio/' + item.name + '/root_user length=20 chars=ascii_letters,digits')) }}"
```

Always prefix secret paths with the role's `secret` variable
(`{{ secret + '/role/...' }}`).

### Reading existing files

```jinja2
{% set existing = lookup('file', secret + '/postfix/' + inventory_hostname + '/maincf.json', on_missing='skip') %}
```

Use `on_missing='skip'` to avoid errors when the file doesn't exist yet.

### Shell commands

```jinja2
"hash": "{{ lookup('pipe', 'printf "%s" "' + lookup('password', ...) + '" | sha512sum | cut -f1 -d" "') }}",
```

Use sparingly — prefer Ansible modules over shell lookups.

## Security

### Server-Side Template Injection (SSTI)

Jinja2 evaluates `{{ ... }}`, `{% ... %}`, and `{# ... #}` syntax in template
variables. If any value passed to `render()` comes from user input or
environment variables, an attacker can inject template directives that read
files, access object attributes, or execute code.

- Never pass untrusted user input as template variables.
- Be especially careful with `env=os.environ` — environment variables set by
  external tools, CI systems, or user profiles may contain template syntax.
- In Ansible, the `template` module is safe because Ansible controls the
  variable sources (role defaults, host facts, inventory). The risk exists in
  custom Python code that calls `jinja2.Template.render()` directly.

### The `safe` filter

Use `| safe` only when you are certain the content is trusted and should not
be HTML-escaped. In config file templates (not HTML), escaping is usually
undesirable, so `| safe` is the default behavior — the filter is rarely
needed.

### Secret exposure

- Variables named `secret*` contain paths to the Ansible secrets directory.
  Never render the raw secret value to config files — use
  `lookup('password', ...)` which generates the value server-side.
- Log messages generated during template rendering can leak variable values.
  Keep sensitive variables out of debug output.

## Self-verification

After writing or modifying a Jinja2 template, verify it:

### 1. Render the template

```bash
ansible all -m template -a "src=path/to/template.j2 dest=/dev/null"
```

Or for a specific host:

```bash
ansible hostname -m template \
  -a "src=ansible/roles/role/templates/path/to/template.j2 dest=/tmp/verify.out"
```

### 2. Check syntax

For generated Python fact scripts:

```bash
python3 -m py_compile path/to/rendered_script
```

For generated shell scripts:

```bash
shellcheck path/to/rendered_script
```

For .j2 templates themselves: use `file` to detect the generated type, then
validate accordingly.

### 3. Inspect rendered output

```bash
diff -u expected_output rendered_output
```

Pay attention to:
- Trailing whitespace (config files are often parsed strictly)
- Blank lines between entries (controlled by comments and state tracking)
- Correct comment rendering (multi-line YAML text blocks)

### 4. Common errors to check

| Error | Symptom | Fix |
|---|---|---|
| Falsy value swallowed | `0` or `False` parameter missing | Use `is defined` instead of `\| d()` |
| Double newline before comment | Extra blank line above comment | Strip trailing `\n` before `\| comment()` |
| Leading whitespace in output | Config file has spaces at line start | Check `{{` is at column 0, not indented |
| Template syntax error | Ansible fails with Jinja2 parse error | Check unmatched `{% %}` or `{{ }}` |
| Missing trailing newline | Config file ends without `\n` | Remove `-%}` from last output line |
| Fact script crash | `json.loads()` fails on invalid JSON | Verify the `to_nice_json` output is valid JSON |
| Variable not defined | Ansible `UndefinedError` | Add `\| d()` to all optional variable references |

### 5. Verify with a no-op playbook

Create a minimal playbook that templates the file and diffs against the
current state:

```bash
ansible-playbook --check --diff -l hostname path/to/playbook.yml
```

The `--diff` output shows what changed. A clean run means the template
produces the expected output.
