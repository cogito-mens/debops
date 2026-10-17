---
name: debops-user-install
description: Use DebOps in production — installing the Python package from PyPI with pipx, creating a project directory, adding hosts, and the first playbook runs. For working on DebOps itself (roles, playbooks, the CLI), see the debops-developer-install skill instead; when unsure which one the user needs, ask whether they want to manage infrastructure or modify DebOps. Use when installing DebOps, creating a project, or preparing a host for its first run.
license: GPL-3.0-or-later
---

# Using DebOps in production: install and first project

How to install DebOps as a normal Python package and set up a project that
manages hosts. For deeper coverage see the official documentation at
<https://docs.debops.org/> (Getting Started guide, then the User Guide chapter
on project directories).

> **Important**: runs execute against real hosts. Always `debops check` before
> `debops run` on a host you have not configured yet, and prefer `--limit` to
> touch one host at a time.

## Install the CLI

A Debian or Ubuntu control host with Python 3, `git`, and SSH access to the
managed hosts is assumed. The `debops` PyPI package carries the CLI plus the
`debops.debops` Ansible Collection (all roles and playbooks):

```shell
pipx install debops            # isolated install, `debops` on PATH
debops --version
```

`pip install debops` into a user site also works; pipx keeps it isolated.

Upgrade later with `pipx upgrade debops`.

## Create a project

```shell
debops project init --type modern ~/src/projects/example
cd ~/src/projects/example
```

The modern layout keeps each infrastructure view self-contained:
`ansible/views/<view>/{inventory,secret,playbooks,resources}` plus a generated
`ansible.cfg` per view. Project configuration lives in `.debops/conf.d/*.yml`.
The generated `ansible.cfg` is rewritten by `debops project refresh` — never
edit it by hand; change the configuration in `.debops/conf.d/` instead.

Add hosts to `ansible/views/system/inventory/hosts`:

```ini
[webservers]
web1 ansible_host=web1.example.org   # or IP address

[debops_all_hosts:children]
webservers
```

Hosts in `[debops_all_hosts]` receive the common playbook (firewall, SSH,
user accounts, ...). A host only in a service group such as `[debops_service_nginx]`
is managed by `service/nginx` runs.

## First run

```shell
debops check site -l web1        # dry run: connects, changes nothing
debops run site -l web1          # full configuration
debops run service/nginx -l web1 # one service stack later on
```

The first run on a fresh host should be `site` or `common` (or at least the
full `service/<role>` stack) so that repositories, secrets, and firewall rules
exist before service roles rely on them.

Secrets are generated per view in `ansible/views/<view>/secret/`; with
git-crypt or encfs configured, `debops project unlock` and
`debops project lock` manage access. Commit the project to git early:
`debops project commit`.

## Optional: a project virtualenv

Off by default. `debops project venv enable` pins ansible-core and the roles'
Python dependencies per project under `.debops/venv/`; manage it only through
`debops project venv` subcommands, never manual `pip`.

## Next steps

- The `debops-cli` skill covers day-to-day CLI usage in a project.
- The `debops-developer-install` skill covers working on DebOps itself
  (editable install from the monorepo, live testing of role changes).
- Documentation: <https://docs.debops.org/en/master/introduction/getting-started.html>
