---
name: skill-author
description: Author a project-local skill for the DebOps MCP server — the .debops/mcp/skills/ layout, SKILL.md frontmatter rules, shadowing packaged skills, write-time validation, and the write_allow opt-in which lets an agent create skills at all. Use when writing a new project skill or updating one.
license: GPL-3.0-or-later
---

# Authoring project-local skills

A project can carry its own skills, served by the MCP server next to the
packaged corpus. This skill covers writing one.

## Layout

A skill is a directory with a `SKILL.md` file, plus optional supporting files:

```
.debops/mcp/skills/<name>/SKILL.md
.debops/mcp/skills/<name>/references/notes.md
```

The directory name is the skill name: lowercase words separated by dashes,
`^[a-z0-9-]+$`. The frontmatter `name` must match the directory name, and
`description` is required — it is the only thing an agent sees when choosing
a skill, so write it as a trigger: what the skill does and when to use it.

```markdown
---
name: my-skill
description: What it does and when to use it. Use when <trigger>.
license: GPL-3.0-or-later
---

# My skill

Procedural knowledge, in Markdown.
```

## Rules of the server

- The project skill **shadows** a packaged skill of the same name (the way
  project roles shadow collection roles).
- Skills are validated at write time; an invalid `SKILL.md` is refused.
- A write, a delete, or a `project_pull` which changes the skills announces
  it to subscribed clients (`resources/list_changed`, and
  `resources/updated` for the skill's URI).
- Serving is gated by `project.mcp.project_content` (on by default).

## Writing skills with the write tools

Skills live outside the infrastructure view, so `file_write` must be allowed
to reach them — by default it is not. The project administrator opts in:

```toml
# .debops/conf.d/mcp.toml
[project.mcp]
write_allow = ["resources/**", "playbooks/**",
               ".debops/mcp/skills/**"]
```

Then, per skill file:

```
file_write path=.debops/mcp/skills/my-skill/SKILL.md content="..."
```

Paths under `.debops/` are anchored at the project directory, in legacy and
modern projects alike.

## Verifying

- `resources/list` shows the skill; `resources/read` with
  `skill://my-skill` returns it.
- `debops mcp inspect <socket> --resources` lists it for a human.
- Rename the directory or drop the description and re-write: the refusal
  tells you what the server expects.
