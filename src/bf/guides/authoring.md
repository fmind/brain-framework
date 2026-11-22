# Authoring

Humans write plain Markdown in any folder listed under `notes:` in `bf.yaml` (defaulting to `concepts/`, `projects/`, and `actions/`). Both flat files (`<dir>/<slug>.md`) and universal bundle directories (`<dir>/<slug>/index.md`, with all sibling files and subdirectories left untouched) compile uniformly via `bf build` into `memories/<dir>.jsonl` (`kind: "note"`), validated unconditionally against the bundled `record` schema plus required `schemas/<dir>.json`.

## YAML frontmatter and required schemas

Start every note with a YAML 1.2 frontmatter block (anchors and aliases are rejected; `on`, `off`, and `no` remain strings):

```markdown
---
type: architecture
status: stable
updated: 2026-11-15
stale_after: 90d
tags: [agents, mcp]
links:
  - bf:///concepts/welcome.md#conventions
summary: Open standards governing agent protocols and tool integrations.
---

# Agentic AI Foundation
```

- `updated` (`YYYY-MM-DD`) and `stale_after` (humanized duration like `14d` or `90d`, integer days, or ISO date) drive read-only `bf review` freshness checks (`newer_evidence` and `due`).
- Every custom frontmatter field passes straight through to the compiled JSONL record in `memories/<dir>.jsonl` (`ref`, `source`, `kind`, `text`, `headings`, `tasks`, and `sha256` are computed by `bf build`).
- Every configured `notes:` folder and every `memories/` sensor source requires `schemas/<source>.json`; every `usages/` source validates against required `schemas/usages.json` (plus optional `schemas/<source>.json` tightening). Bundled `record` is always enforced unconditionally (an optional `schemas/record.json` can only tighten, never weaken it).

## Heading anchors, tasks, and link metadata

Use Python-Markdown `attr_list` syntax where it adds real signal—heading `#id` anchors and typed links—alongside standard checklist tasks:

<!-- dprint-ignore-start -->
```markdown
## Model Context Protocol {#mcp creator=Anthropic}

Standard wire protocol for tools and context. See [AAIF](aaif.md#mcp){rel=part-of}.

- [ ] Audit server permissions.
```
<!-- dprint-ignore-end -->

- **Headings (`headings[]`)**: `## MCP {#mcp creator=Anthropic}` populates `headings` (`{id, title, level, meta}`) and gives `bf read path#mcp` a stable section anchor across title edits. Separate attributes with spaces (`{#mcp?creator=Anthropic}` is rejected). Headings inside blockquotes are ignored so quoted excerpts never shift anchors.
- **Tasks (`tasks[]`)**: Markdown checklist items (`- [ ] ...`, `- [x] ...`) populate `tasks` (`{text, done, section, meta}`) and drive `total_tasks_open` in `bf review`.
- **Links (`links[]`)**: attach edge metadata via `{rel=part-of}` right after `)` in Markdown prose, or `{to, meta}` objects in frontmatter/sensor JSON.

## Connect notes and records {#connect-notes-and-records}

Authored notes and ingested JSONL records share one normalized `links` list (`note`, `record`, `brain`, `url`) addressed by universal human-readable `ref`:

- **Markdown prose**: link to local notes (`../concepts/aaif.md#mcp`, `#mcp`), records by `source:id` (`[Issue 7](github-issues:7){rel=cites}`), declared sibling brains (`bf://team-brain/concepts/aaif.md#mcp`, `bf://team-brain/jira:WEB-1`), or external URLs (`https://`, `http://`, `mailto:`, `urn:`).
- **Note frontmatter & JSON (`links`)**: accept brain-root note paths, `source:id`, `bf://` URIs, external URLs, or `{to, meta}` objects. Cross-brain `bf://<host>/...` targets must declare `<host>` under `brains:` in `bf.yaml`. Record IDs cannot contain `#` or whitespace, and schemes containing `://` other than `https`/`http`/`bf` are rejected rather than misclassified as record refs.
- **Validation**: `bf check` is strictly read-only—run `bf build` after editing Markdown, then `bf check` to verify compiled-note sync, schemas, `memories/` & `usages/`, enabled `loops:`, links, `#anchor` headings, and declared cross-brain targets. Missing targets in snapshot-ingested sources or `.forgotten` emit warnings (`ok: true` unless `--strict`).

## Flat notes and `index.md` bundles

Any subdirectory inside a configured `notes:` folder that contains `index.md` is treated as a bundle note: `bf build` compiles only `index.md` and ignores every sibling file or subdirectory (such as `inputs/`, `outputs/`, attachments, or scripts). Use dated bundles like `actions/YYYY-MM-DD_<slug>/index.md` for workspaces, or flat `<dir>/<slug>.md` files for standalone notes.
