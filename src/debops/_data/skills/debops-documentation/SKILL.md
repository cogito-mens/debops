---
name: debops-documentation
description: Navigating the DebOps online documentation — the docs.debops.org URL scheme and version slugs, the verified entry points (getting started, project directories with inventory and infrastructure views, playbooks, CLI reference, role index, upgrade notes), and how to cite role documentation. Use when you need an authoritative DebOps documentation link, when answering questions about DebOps concepts, or before guessing a documentation URL.
license: GPL-3.0-or-later
---

# DebOps documentation map

The DebOps documentation lives at <https://docs.debops.org/>, versioned by
URL slug. This skill is the map: cite these entry points instead of guessing
URLs, and fetch a page to follow its links when you need something deeper.

## Version slugs

The URL scheme is `https://docs.debops.org/en/<slug>/...` where the slug is:

- `master` — the development branch (this repository's working tree)
- `stable-X.Y` — a release line, e.g. `stable-3.3`

There is no plain `stable` slug; it does not resolve. Match the slug to the
DebOps version in use — the `role_info` tool already produces documentation
links with the right slug for the project's version.

## Entry points

All paths below are relative to `https://docs.debops.org/en/<slug>/` and are
known to exist:

- `introduction/quick-start.html` — try DebOps in Docker or Vagrant, no
  installation
- `introduction/getting-started.html` — installation and the first project
- `user-guide/project-directories.html` — the project layout; the Ansible
  inventory and infrastructure views are sections of this page
  (`#the-ansible-inventory`, `#how-to-use-infrastructure-views`)
- `user-guide/inventory-spec.html` — the inventory specification which
  generates the inventory from project data
- `user-guide/playbooks.html` — playbook composition and import order
- `user-guide/scripts/debops.html` — the `debops` CLI reference
- `ansible/role-index.html` — every bundled role, by category
- `news/upgrades.html` — upgrade notes between releases; check before an
  upgrade
- `developer-guide/contributing.html` — development setup and contribution
  workflow

## Role documentation

The URL pattern for a bundled role is:

```
https://docs.debops.org/en/<slug>/ansible/roles/<role_name>/index.html#debops-<role-name>
```

with underscores kept in the path and turned into hyphens in the anchor
(`debops.nginx` → `ansible/roles/nginx/index.html#debops-nginx`). Each role
page documents its variables, dependency usage and examples; the "Getting
started" section of a role page is the fastest way to a working service.

## Everything else

- The monorepo: <https://github.com/debops/debops> (roles, playbooks, the CLI,
  these docs under `docs/`)
- The Ansible Collection: `debops.debops` on Ansible Galaxy
- Support channels and community links are listed on the documentation index
  page

## Rules of engagement

- **Fetch, don't guess.** If a needed page is not in this map, fetch the
  nearest entry point and follow its links rather than inventing a URL.
- **Match the version.** An answer about `stable-3.2` behavior may not hold
  on `master`; say which slug you read.
- **Cite the page.** When documentation informed the answer, name the URL so
  the user can verify it.
