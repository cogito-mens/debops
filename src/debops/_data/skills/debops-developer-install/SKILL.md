---
name: debops-developer-install
description: Install DebOps in developer mode - cloning the monorepo and an editable install so role and playbook edits go live immediately, the test suite, and the workflow for verifying changes against a real project. For managing infrastructure with a released DebOps, see the debops-user-install skill instead; when unsure which one the user needs, ask whether they want to modify DebOps or just use it. Use when editing roles, playbooks, plugins, or the CLI source.
license: GPL-3.0-or-later
---

# DebOps in development mode

How to set up a development environment for working on DebOps itself — roles,
playbooks, plugins, or the CLI. For normal usage of a released DebOps, see the
`debops-user-install` skill.

## Get the source and an editable install

```shell
git clone https://github.com/debops/debops ~/src/github.com/debops/debops
pipx install --editable ~/src/github.com/debops/debops
```

(Any git source works the same; the GitHub URL is the canonical one.)

An editable install matters because of how the package ships its content:
`src/debops/_data/ansible/collections` in the installed package is a symlink
into the checkout, and the collection's `playbooks`/`plugins`/`roles` are
symlinks back into `ansible/`. So with an editable install, editing a role in
the monorepo is immediately visible to every project that uses the package —
no reinstall, no rebuild. With a normal install you would be editing a
detached copy.

Verify the install picks up the checkout, and note that a *project* may still
shadow it with its own collections — the `debops-cli` skill has the full
preflight (LIVE / NOT LIVE check).

## The monorepo layout

```
ansible/roles/          # all DebOps roles
ansible/playbooks/      # site.yml, service/<role>.yml, scope/<role>.yml
ansible/plugins/        # filter/lookup/... plugins
src/debops/             # the debops CLI (Python package)
src/tests/              # pytest suite (unit/ and integration/)
docs/                   # Sphinx documentation
```

## The change-verify loop

Create or reuse a project to exercise changes against a real host:

```shell
debops project init --type modern ~/src/projects/sandbox
debops check service/<role> -l testhost    # dry run, catches template errors
debops run service/<role> -l testhost      # apply
debops run service/<role> -l testhost      # again: idempotency proof
```

On a fresh host, run the full `service/<role>` stack first; scoped runs
(`scope/<role>`) assume an already-configured host. See `debops-cli` for the
verification ladder.

## Run the test suite

From the monorepo root:

```shell
make test-unit              # the pytest unit suite
make test-integration       # CLI-driven integration tests
make test-yaml              # yamllint
make test-spdx              # license headers (REUSE)
```

Run a focused subset while iterating:
`python3 -m pytest src/tests/unit/test_<name>.py -q`.

## Conventions that will be checked

- Commit messages follow Conventional Commits, GPG-signed, with a
  `Generated-By: LLM` trailer when applicable; see the AI contributions policy
  in the documentation (`debops_policy__ai_contributions`).
- Role style lives in the `ansible-programmer` skill; template style in
  `jinja2-programmer`.
- REUSE compliance: every file needs copyright/license information.
