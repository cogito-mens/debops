---
name: debops-cli
description: Using the DebOps command-line interface inside a project directory — modern project layout, infrastructure views, the project/env/exec/run/check/config sections, playbook resolution and scoping, and the workflow for testing role, playbook, or filter-plugin changes against a live project. Use when working in a DebOps project directory, running or checking DebOps playbooks, verifying DebOps changes on a host, or debugging DebOps roles and plugins.
license: GPL-3.0-or-later
---

# DebOps CLI usage

Procedural knowledge for driving the `debops` CLI against a project directory:
what each section does, how playbook names resolve, how to scope a run so
unrelated roles cannot block you, and how to verify a change on a live host
without reinstalling anything. Does **not** cover authoring DebOps roles
(see `ansible-programmer`, `jinja2-programmer`) or the internal Python API.

> **Important**: This skill runs commands via tool calls. Runs execute against
> real hosts. Prefer `--eval` and `debops check` before a real run, scope with
> `--tags` or `-l`, and never run a destructive playbook without user
> confirmation.

## Design philosophy

`debops` is a thin wrapper. For every section it:

1. loads the project configuration (`.debops/conf.d/*.yml`),
2. exports the project environment (`ANSIBLE_CONFIG`, collection paths, venv
   `PATH`),
3. unlocks the view's encrypted `secret/` directory,
4. resolves playbook names and execs the real Ansible command.

Because the wrapper is thin, you never have to guess what it will do. `--eval`
prints the exact command and environment it would execute. When something
behaves unexpectedly, print the command first; do not reverse-engineer the
wrapper.

## The project directory (modern layout)

```
<project>/
├── ansible/
│   ├── collections/
│   │   ├── ansible_collections/
│   │   └── requirements.yml
│   ├── keyring/
│   ├── overrides/{files,tasks,templates}/
│   └── views/
│       └── system/
│           ├── ansible.cfg          # generated; do not edit by hand
│           ├── inventory/
│           │   ├── hosts
│           │   ├── group_vars/all/
│           │   └── host_vars/
│           ├── playbooks/           # project-local playbooks
│           ├── resources/           # per-view files/templates
│           └── secret/              # encrypted secrets (git-crypt)
└── .debops/
    ├── conf.d/{project,venv,view-*}.yml
    ├── environment                  # extra env vars
    ├── requirements.txt             # pinned Python packages
    └── venv/                        # project virtualenv
```

The **legacy** layout puts `ansible.cfg`, `inventory/`, and `playbooks/`
directly under `ansible/`. Both are supported.

`ansible/views/<view>/ansible.cfg` is generated from configuration. Never edit
it directly; changes are lost on `debops project refresh`. Put settings in
`.debops/conf.d/view-<view>.yml` instead.

## Sections

| Section | Purpose |
|---------|---------|
| `debops project` | manage the project: `init`, `mkview`, `refresh`, `unlock`, `lock`, `commit`, `venv` |
| `debops env` | run an **arbitrary shell command** in the project environment |
| `debops exec` | run an **ad-hoc Ansible module** against the inventory (wraps `ansible`) |
| `debops run` | run one or more playbooks (wraps `ansible-playbook`) |
| `debops check` | same as `run` but adds `--diff --check` |
| `debops config` | inspect configuration: `get`, `list` |

All sections accept `--project-dir <path>`, `-V/--view <view>`, `-E/--bell`,
`--eval`, and `-v`. DebOps options must appear **before the first positional
argument**; after it they are passed to Ansible or ignored — put them first
always. `--` is documented to end DebOps options, but `run`/`check`/`exec`
already forward everything after the playbook or host pattern to Ansible, and
`debops env` does not honour `--` at all.

## Playbook resolution

`debops run <name>` resolves `<name>` in this order:

1. the default Ansible Collection (`debops.debops`), e.g. `service/telegraf`
   → `<collection>/playbooks/service/telegraf.yml`,
2. `<project>/ansible/views/<view>/playbooks/<name>.yml`,
3. an ordinary filesystem path (absolute works).

A "playbook set" defined under `views.<view>.playbook_sets` in
`.debops/conf.d/view-<view>.yml` can expand one name into many.

`service/...` and `scope/...` resolve the same way — they are just
subdirectories of the collection's `playbooks/`.

### Service playbooks vs. scoped playbooks

Two kinds of playbooks target a role, and the difference is **dependency
roles**:

| Playbook | Contains | Use when |
|----------|----------|----------|
| `service/<role>` | the role **plus its dependent roles** (the whole "service stack": APT repo, secrets, `ferm`, `sysctl`, ...) | configuring a host, or any first run |
| `scope/<role>` | **only** the role's main entry point | the host is already configured; iterating on one role |
| `scope/<role>/<entry_point>` | one alternate entry point of the role (e.g. `scope/apt/update`) | running a single role action |
| `scope` | any role via `-e 'role=<role>'` | ad-hoc role application |

`service/elasticsearch` pulls in `extrepo`, `secret`, `etc_services`,
`sysctl`, `ferm`, and `java`; `scope/elasticsearch` imports only
`elasticsearch`. Likewise `service/telegraf` pulls in `keyring` and
`influxdata` (which configure the APT repository); `scope/telegraf` does not.

```shell
debops run service/elasticsearch -l server1.example.com          # full service stack
debops run scope/elasticsearch -l server1.example.com            # just the role
debops run scope/apt/update -l server1.example.com               # one entry point
debops run scope -l server1.example.com -e 'role=apt'            # generic, any role
```

**Scoped playbooks assume the host is already configured.** They are not a
substitute for a first `service/<role>` or `site` run. Some roles expect an
upstream APT repository set up by another role (`debops.extrepo`, `keyring`),
so `scope/<role>` on an unconfigured host can fail to install the package —
that is expected, not a bug in the role. Run `service/<role>` (or `site`)
first, then use `scope/<role>` to focus.

Scoped playbooks also target `[debops_all_services]` in addition to the
service groups, so a host can be added there to receive any scoped role.

### Scope a run with tags and limits

Ansible tags are a second way to focus a run. On a `service/<role>` playbook,
`--tags role::<role>` runs the role plus any dependent roles carrying the same
tag:

```shell
debops run service/telegraf --tags role::telegraf
debops check service/telegraf --tags role::telegraf -l server1.example.com
debops run site --skip-tags role::docker_server   # inverse
```

`--tags role::<name>` runs only tasks tagged for that role; `--skip-tags`
excludes one. Both are passed through to `ansible-playbook`.

Tags and scoped playbooks share the same caveat: only tagged tasks run, so a
dependency **not** tagged for the role is skipped. Which dependencies survive
varies per role — check the service playbook:

```yaml
# service/elasticsearch.yml — extrepo carries role::elasticsearch
- role: extrepo
  tags: [ 'role::extrepo', 'skip::extrepo', 'role::elasticsearch' ]

# service/telegraf.yml — repo roles carry role::influxdata, NOT role::telegraf
- role: keyring
  tags: [ 'role::keyring', 'skip::keyring', 'role::influxdata' ]
- role: influxdata
  tags: [ 'role::influxdata', 'skip::influxdata' ]
```

So `service/elasticsearch --tags role::elasticsearch` still configures the APT
repo, but `service/telegraf --tags role::telegraf` skips `keyring` and
`influxdata` and does **not** — which works only because the repo is already
configured on the host. When in doubt, omit the tags for the first run.

## Prerequisite: is the collection loaded from your checkout?

A project can obtain the `debops.debops` collection from several places, and
only one reflects edits to the DebOps source tree:

| Source of `debops.debops` | How it gets there | Source edits live? |
|---------------------------|-------------------|--------------------|
| DebOps package installed **editable** | `pipx install --editable <monorepo>` | **Yes** — the package's `_data/ansible/collections` is a symlink into the checkout |
| DebOps package installed normally | `pipx install debops` / `pip install debops` | No — detached copy under `site-packages` |
| Ansible Galaxy collection | `ansible/collections/requirements.yml` | No — read-only copy inside the project |
| Project-local collections | `debops project venv enable --collections` (or `--all`) rewrites `collections_path` to **only** the project's `ansible/collections/` | No — the package collection is dropped even if editable |

An **encapsulated venv project** (`project.venv.enabled: true`) does not
automatically use project-local collections; only the `--collections`/`--all`
scope rewrites `collections_path`. Rely on the preflight below, not on the venv
flag.

Before editing DebOps source and expecting a project to pick it up, check which
one is in play. Two cheap steps, then a definitive one:

```shell
# 1. What is configured? (note: no -k — that lists child keys, not values)
debops config get project.venv.enabled
debops config get views.<view>.ansible.defaults.collections_path
# If the DebOps package path (…/debops/_data/ansible/collections) is absent
# from that list, the package copy — editable or not — is not searched.

# 2. Where does Ansible actually load the collection from? Prints "# <dir>"
debops env ansible-galaxy collection list debops.debops

# 3. Does that resolve into a source checkout?
src="$(debops env ansible-galaxy collection list debops.debops 2>/dev/null \
       | sed -n 's/^# //p')"
echo "collection: $src/debops/debops"
git -C "$(readlink -f "$src")" rev-parse --show-toplevel 2>/dev/null \
  && echo "LIVE — source edits are visible to this project" \
  || echo "NOT LIVE — installed or Galaxy copy"
```

- **LIVE** (a git toplevel is printed, e.g. your monorepo): edit the source
  tree and re-run; no reinstall needed. Proceed.
- **NOT LIVE** (a path under `<project>/ansible/collections/` or
  `site-packages`): editing the DebOps source changes nothing this project
  sees. **Stop and ask the user** to enable development mode instead of
  guessing:

  ```shell
  pipx install --editable ~/src/github.com/<owner>/debops
  ```

  For a project that uses its own local collections, the project's
  `ansible/collections/` is authoritative, so the fix is different — ask the
  user whether to remove the `collections_path` rewrite or to add the source
  path back to `collections_path` in `.debops/conf.d/view-<view>.yml`. Do not
  edit the Galaxy copy in place.

### Why an editable install stays live

```
<pkg>/_data/ansible/collections -> <repo>/ansible/collections
<repo>/ansible/collections/ansible_collections/debops/debops/
    {playbooks,plugins,roles} -> ../../../../{playbooks,plugins,roles}
```

Both `_data/ansible/collections` and the collection's `{playbooks,plugins,roles}`
are symlinks tracked in the DebOps repository (git mode `120000`). An editable
install consumes them as-is, so a source edit is visible immediately; a normal
install resolves them into a detached copy. When a project venv is enabled it
supplies ansible-core; otherwise Ansible comes from `$PATH`. Either way, the
collection supplies the DebOps content.

## Verifying a change against a live project

### Verification ladder

Work cheapest-first. Each rung catches a different class of bug.

```shell
# 0. First run on a fresh/unconfigured host: the WHOLE service stack.
#    Scoped runs fail here (no APT repo, no secrets) — that is expected.
debops run service/telegraf -l server1.example.com

# 1. What will actually run? (no execution, reveals resolved paths)
debops run --eval service/telegraf -l server1.example.com

# 2. Which DebOps plugins load in the project environment?
debops env ansible-doc -t filter -l | grep '^debops\.debops\.'

# 3. Dry run — applies nothing, still connects and templates everything
debops check service/telegraf --tags role::telegraf

# 4. Real run, now scoped to the role (host is configured)
debops run service/telegraf --tags role::telegraf

# 5. Idempotency — the second run must report changed=0
debops run service/telegraf --tags role::telegraf
```

Rung 3 surfaces template/filter errors, undefined variables, and type errors
without touching the host. Rung 5 is the only proof that a change is correct
and convergent.

Rungs 3–5 may be scoped (`--tags role::<role>` or `scope/<role>`) **only
after** rung 0 has configured the host; on a fresh host, scoping skips the
dependent roles and the run fails.

Use the **list** form for rung 2 (`ansible-doc -t filter -l`). The per-plugin
form `ansible-doc -t filter debops.debops.to_toml` exits non-zero with
`Missing documentation` because DebOps filter plugins carry no `DOCUMENTATION`
block — the error is expected, not a failure to load. The list form prints the
found plugins and exits 0.

### Throwaway probe playbook

To inspect variable resolution, filter output, or facts in the exact project
context, run a scratch playbook from outside the project. It inherits the
project inventory, `host_vars`/`group_vars`, `ANSIBLE_CONFIG`, and secrets.

```shell
cat > /tmp/opencode/probe.yml <<'EOF'
---
- name: Probe
  hosts: all
  gather_facts: false
  tasks:
    - ansible.builtin.debug:
        msg: "interval={{ telegraf_interval | default('10s') }}"
EOF
debops run /tmp/opencode/probe.yml -l server1.example.com
```

This beats ad-hoc `ansible` for anything involving templating, because it uses
the real play/task machinery.

## Running ad-hoc commands

`debops exec` wraps `ansible` and defaults to the `command` module. It needs a
**host pattern as a positional argument**, exactly like `ansible`:

```shell
debops exec server1.example.com -m ansible.builtin.command -a "uname -r"
debops exec -V deployment webserver -m ansible.builtin.apt -a "upgrade=full" -b
debops exec --eval server1.example.com -m ansible.builtin.ping     # print before running
```

For anything that is not an Ansible invocation (shell tools, `ansible-doc`,
`pip`, `git`), use `debops env`:

```shell
debops env ansible --version
debops env ansible-doc -t filter -l | grep '^debops\.debops\.'
debops env python3 /tmp/opencode/check.py    # a script file, not -c
```

`debops env` re-parses its arguments through a shell, so argument values
containing shell metacharacters (`(`, `)`, `;`, `$`, quotes) are re-interpreted
and usually fail:

```shell
debops env python3 -c "import sys; print(sys.executable)"
# /bin/sh: 1: Syntax error: word unexpected (expecting ")")
```

Put non-trivial commands in a file and run the file. `debops env -- cmd` does
not act as a separator either — `--` is passed to the command. Note that
`debops env python3` resolves to the **project venv** interpreter when one is
enabled (it holds ansible-core but not the `debops` package), and to the system
Python when it is not.

## Infrastructure views

A view is a self-contained inventory plus secrets and custom resources.
`debops` selects it by:

1. `-V/--view <view>` on the command line,
2. the current working directory, if it is under `ansible/views/<view>/`,
3. `project.default_view` in `.debops/conf.d/project.yml`.

```shell
debops config get -k views       # list defined views (child keys)
debops config get project.default_view   # read a value
cd ansible/views/deployment && debops run app/setup   # auto-selects 'deployment'
debops run --project-dir ~/src/projects/x -V deployment app/setup
```

`debops config get <key>` returns the **value** of `<key>`; the `-k`/`--keys`
flag switches it to **list the child keys** at that level. A leaf key with `-k`
prints nothing, which looks like a missing value — a common misread.

## The project virtualenv (optional)

A project virtualenv is **optional and off by default**
(`project.venv.enabled = false`). Many projects have none, and the user may
deliberately keep it that way — do not create or enable one unless asked.

```shell
debops config get project.venv.enabled          # false = no project venv
debops config get binaries.ansible-playbook     # which Ansible will run
```

| Venv | Ansible used | Consequence |
|------|--------------|-------------|
| disabled (`false`) | `ansible-playbook`/`ansible` from `$PATH` — pipx, system package, or user `pip` | `.debops/requirements.txt` pins do **not** apply; the environment is whatever the user installed |
| enabled (`true`) | `<venv>/bin/ansible*` (`binaries.*` are rewritten at runtime; `bin/` is prepended to `PATH`) | Versions are pinned in `.debops/requirements.txt`; reproducible per project |

`debops config get binaries.ansible-playbook` shows the effective binary: a
`<venv>/bin/...` path when the venv is enabled, or the bare `ansible-playbook`
name when it is not. (`DEBOPS_CMD_ANSIBLE*` environment variables in
`.debops/environment` override the lookup.)

When the venv **is** used, manage it with `debops project venv`, never manual
`pip`:

```shell
debops project venv install     # create + install requirements
debops project venv upgrade     # upgrade seed packages + collections
debops project venv freeze      # write installed versions to requirements.txt
debops project venv recreate    # destroy and rebuild
debops project venv --python freeze   # manage only Python packages
```

The seed installs `ansible-core` and the roles' Python dependencies, never the
`debops` package itself — so the `debops` CLI always comes from outside the
project (pipx or a global/user `pip` install), which is why the editable
question above is about *that* install.

## Gotchas

- **`debops exec` runs `ansible`, not arbitrary commands.** `debops exec
  ansible-doc ...` tries to run `ansible ansible-doc ...`. Use `debops env`.
- **Ad-hoc `ansible` needs a host pattern.** `debops env ansible -m cmd -a "x"
  -l host` prints Ansible help — `-l` limits an existing pattern, it is not
  one. Use `debops exec host -m ...` or pass a bare pattern.
- **`get_url`/`uri` tasks run on the target**, not the controller. A local
  `curl` succeeding proves nothing about the host's connectivity.
- **`--check` still connects and gathers facts.** It is not offline, and tasks
  with `check_mode: false` (or `always` tags) do make changes.
- **Secrets are unlocked and locked around each run.** The encrypted
  `secret/` directory lives per view (`ansible/views/<view>/secret/`, or
  `ansible/secret/` in the legacy layout). With `git-crypt`, locking needs a
  clean tree; otherwise run `debops project unlock` first.
- **`--eval` output includes the environment.** The first line is
  `export ANSIBLE_CONFIG=...`; the second is the resolved
  `ansible-playbook` (or `ansible`) invocation with absolute paths.
- **`--project-dir` (and other DebOps options) must precede the positional.**
  `debops run service/telegraf --project-dir <path>` fails with
  `Error: DebOps project directory not found`, and
  `debops config get <key> --project-dir <path>` silently ignores the option
  and reads the current directory. Put DebOps options first.
- **`debops config get` returns values; `-k` lists keys.** `config get
  project.venv.enabled` → `true`; `config get -k project.venv.enabled` → empty
  (a leaf has no children). Use `-k` only to enumerate levels.
- **`debops env` re-parses arguments through a shell.** Metacharacters in
  argument values break the command (`python3 -c "f(x)"` → `/bin/sh: Syntax
  error`). Pass a script file instead. `debops env -- cmd` is not a separator.
- **An encapsulated venv project may use project-local collections.** When
  collections are enabled (`debops project venv enable --collections` or
  `--all`), `collections_path` is rewritten to the project's own
  `ansible/collections/`, so an editable DebOps package is no longer searched.
  Check before assuming.

## Anti-patterns

- **Guessing the generated command**: Run `--eval` instead of inferring what
  DebOps passes to Ansible. It also reveals the resolved collection path.
- **Scoping before the host is configured**: `scope/<role>` or
  `--tags role::<role>` on a fresh host skips the roles that set up the APT
  repository, secrets, and firewall, so package installation fails. Run
  `service/<role>` (or `site`) first.
- **Assuming a dependency is tagged for the role**: check the service
  playbook. `extrepo` carries `role::elasticsearch`, but `keyring`/`influxdata`
  carry `role::influxdata`, so `--tags role::telegraf` skips them.
- **Editing `ansible/views/<view>/ansible.cfg`**: It is generated. Edit
  `.debops/conf.d/view-<view>.yml` and run `debops project refresh`.
- **Assuming source edits are live**: Only an editable DebOps install, in a
  project whose `collections_path` still includes it, picks up source edits.
  Run the preflight (`ansible-galaxy collection list debops.debops`) first.
- **Editing the collection copy in place**: A Galaxy collection under
  `<project>/ansible/collections/` is a read-only, versioned artifact. Fix the
  DebOps source and reinstall/rebuild instead.
- **Testing with the wrong Python**: With a project venv enabled, only its
  interpreter has the pinned ansible-core; without one, `debops` uses the
  system/user Ansible from `$PATH`. Test with `debops env` or `debops run`,
  not a bare local `python`.
- **Skipping the idempotency re-run**: A single successful run cannot
  distinguish "converged" from "changes every time".
- **Using `pip install` directly in the project venv**: Bypasses
  `requirements.txt`; `debops project venv upgrade` will revert it.

## When to load this skill

- Running, checking, or evaluating a DebOps playbook in a project directory
- Working in a project **without** its own virtualenv, where the system or
  user-installed Ansible is in use
- Checking whether DebOps source edits are actually live in a project
  (editable install or Galaxy copy) before modifying anything
- Verifying a role, template, or filter-plugin change on a live host
- Debugging why a DebOps run failed, stalled, or is not idempotent
- Inspecting variable resolution or filter output in project context
- Choosing between `debops run`, `check`, `exec`, and `env`
- Choosing between `service/<role>` (full stack) and `scope/<role>` (one role)
- Selecting an infrastructure view or a playbook from a non-default collection
- Pair with `ansible-programmer` and `jinja2-programmer` when the fix is in a
  role

## The MCP server alternative

When the DebOps MCP server (`debops mcp serve`) is connected, prefer its tools
for inspection (`project_status`, `project_read`, `inventory_query`,
`role_info`, `role_defaults`, `role_tasks`, `role_handlers`,
`role_templates`, `role_files`, `playbooks`, `playbook_info`, `secret_paths`),
sync the project with `project_pull` and share your changes with
`project_push`, and use `check` before any `run`. The CLI knowledge in this
skill still applies — the tools wrap the same semantics.
