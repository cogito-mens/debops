---
name: prompt-author
description: Author a project-local prompt for the DebOps MCP server — the .debops/mcp/prompts/<name>.md layout, frontmatter arguments, Jinja2 rendering on prompts/get, and the write_allow opt-in which lets an agent create prompts at all. Use when writing a new project prompt or updating one.
license: GPL-3.0-or-later
---

# Authoring project-local prompts

A prompt is a user-invoked workflow template: the operator picks it, the model
gets the rendered text as a user message. This skill covers writing one.

## Layout

One file per prompt, flat in the prompts directory:

```
.debops/mcp/prompts/<name>.md
```

The file name minus `.md` is the prompt name: lowercase words separated by
dashes, `^[a-z0-9-]+$`. Frontmatter carries the description (required — the
only thing an agent sees when choosing) and the arguments:

```markdown
---
description: Deploy a service with review
arguments:
  - name: service
    description: The service playbook to run
    required: true
  - name: environment
    description: Which environment to target
---

Deploy {{ service }}: check it first, then run it with confirmation.
```

The body is rendered with Jinja2 on `prompts/get`: `{{ argument }}`
placeholders, and conditionals like `{% if extra %}...{% endif %}` for
optional arguments. A call which misses a required argument is rejected; extra
arguments are ignored.

## Rules of the server

- A project prompt **shadows** a packaged prompt of the same name.
- Prompts are validated at write time; a file without a frontmatter
  description is refused.
- A write, a delete, or a `project_pull` which changes the prompts announces
  it to subscribed clients (`prompts/list_changed`).
- Serving is gated by `project.mcp.project_content` (on by default) and the
  `prompts` capability appears only when valid prompts exist.

## Writing prompts with the write tools

Prompts live outside the infrastructure view, so `view_write` must be allowed
to reach them — by default it is not. The project administrator opts in:

```toml
# .debops/conf.d/mcp.toml
[project.mcp]
write_allow = ["resources/**", "playbooks/**",
               ".debops/mcp/prompts/**"]
```

Then:

```
view_write path=.debops/mcp/prompts/my-prompt.md content="..."
```

Paths under `.debops/` are anchored at the project directory, in legacy and
modern projects alike.

## Verifying

- `prompts/list` shows the prompt with its arguments; `prompts/get` with
  arguments returns the rendered text.
- `debops mcp inspect <socket> --prompts` lists it for a human.
- Fetch it with a missing required argument: the error names what is missing.
