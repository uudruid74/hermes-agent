---
name: hermes-agent-skill-authoring
description: "Author in-repo SKILL.md: frontmatter, validator, structure, and writing-quality principles."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skills, authoring, hermes-agent, conventions, skill-md]
    related_skills: [plan, requesting-code-review]
---
# Authoring Hermes-Agent Skills (in-repo)

## Skill Locations

| Location | Scope | Create via | Edit via |
|---|---|---|---|
| `~/.hermes/skills/<cat>/<name>/SKILL.md` | User-local | `skill_manage(action='create')` | `skill_manage(action='patch'/'edit')` |
| `skills/<cat>/<name>/SKILL.md` | In-repo (ships) | `write_file` + `git add` | `patch` / `write_file` |

## When to Use

- Asked to add a skill "in this branch / repo / commit"
- Committing a reusable workflow that should ship with hermes-agent
- Editing an existing skill under `skills/` — `patch` for small fixes, `write_file` for rewrites

## Required Frontmatter

Validator source: `tools/skill_manager_tool.py::_validate_frontmatter`.

| Rule | Requirement |
|---|---|
| Opens at byte 0 | `---` (no leading blank line, no BOM) |
| Closes before body | `\n---\n` |
| Parses as YAML | Valid YAML mapping |
| `name` | Present, lowercase+hyphens, ≤64 chars |
| `description` | Present, ≤1024 chars, starts "Use when ..." |
| Body | Non-empty after closing `---` |

```yaml
---
name: my-skill-name
description: Use when <trigger>. <one-line behavior>.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [short, tags]
    related_skills: [other-skill]
---
```

`version`/`author`/`license`/`metadata` not validator-enforced but every peer has them.

## Size Limits

| Field | Max | Enforced |
|---|---|---|
| description | 1024 chars | ✅ |
| Total SKILL.md | 100,000 chars (~36k tokens) | ✅ |
| Target range | 8-14k chars | Peer norm |

≥20k → split into `references/*.md`.

## Writing Quality Principles

1. **Optimize for process predictability.** What behavior changes when this skill loads? Cut lines that don't change it.
2. **Choose the right context load.** Description paid every turn — keep trigger-focused. Details in body or linked references.
3. **Use an information hierarchy.** Always-needed in SKILL.md; branch material in `references/`, `templates/`, `scripts/`.
4. **End steps with completion criteria.** Checkable, exhaustive: "every modified file accounted for" beats "summarize changes."
5. **Co-locate rules with the concept they govern.** No scattering one idea across the file.
6. **Use strong leading words.** "tight loop," "tracer bullet," "root cause" — save tokens, anchor behavior.
7. **Prune duplication and no-ops.** Every sentence changes behavior vs default. If not, delete.
8. **Watch for premature completion.** Sharpen criteria before splitting steps.
9. **Density target: reference-grade.** Half the tokens. Tables for parameters. Man-page compactness (not caveman ambiguity). Full sentences only for edge cases and warnings. This skill is written in this style.

## Required Sections

```
## Overview           — what and why (1-2 paragraphs or table)
## When to Use        — triggers + "Don't use for:" counter-triggers
## [Topic sections]   — tables, exact commands, recipes
## Common Pitfalls    — mistakes + fixes
## Verification       — checkbox list of post-action checks
```

`Overview` + `When to Use` + actionable body + pitfalls = minimum for a peer-quality skill.

## Directory Placement

```
skills/<category>/<name>/SKILL.md
```

Categories (confirm with `ls skills/`): autonomous-ai-agents, creative, data-science, devops, dogfood, email, gaming, github, leisure, mcp, media, mlops/*, note-taking, productivity, red-teaming, research, smart-home, social-media, software-development.

## Workflow

1. **Survey peers** — `ls skills/<category>/`; read 2-3 SKILL.md for tone/structure
2. **Check validator** — `tools/skill_manager_tool.py` if unsure
3. **Draft** — `write_file` to `skills/<category>/<name>/SKILL.md`
4. **Validate:**
   ```python
   import yaml, re, pathlib
   c = pathlib.Path("skills/<cat>/<name>/SKILL.md").read_text()
   ```
5. **Git add + commit** on active branch
6. **Cache note:** Current session won't see it — the loader initializes at session start. Verify via `skill_view` with exact path, or start a new session.

## Cross-Referencing

- `related_skills` unions both trees (in-repo + `~/.hermes/skills/`) at load time
- User-local refs won't resolve for other clones → prefer in-repo links
- Frequently-referenced user-local skill → consider promoting to repo

## Editing In-Repo Skills

| Change | Tool |
|---|---|
| Typo/pitfall/trigger tweak | `skill_manage(action='patch', ...)` |
| Major rewrite | `write_file` full SKILL.md |
| Supporting files | `write_file` to `references/*.md`, `templates/*`, `scripts/*` |
| Commit | Always — in-repo skills are source, not runtime state |
