---
icon: lucide/file-pen-line
description: Write YAML-frontmatter Markdown with CommonMark attr_list metadata, connect notes and brains with universal refs and bf:// URLs, and add optional JSON Schemas in schemas/.
---

# Authoring

Humans write plain CommonMark Markdown in any folder listed under `notes:` in `bf.yaml` (defaulting to `concepts/`, `projects/`, and `actions/`). Both flat files (`<dir>/<slug>.md`) and universal bundle directories (`<dir>/<slug>/index.md`, with all sibling files and subdirectories left untouched) compile incrementally via `bf build` into `memories/_notes.jsonl` (`kind: "note"`) and sync `.bf/cache/index.sqlite3`, validated unconditionally against the bundled `record` schema plus optional `schemas/<dir>.json`.

## YAML frontmatter and optional domain schemas

Start every note with a YAML 1.2 frontmatter block (anchors and aliases are rejected; `on`, `off`, and `no` remain strings):

```markdown
---
type: architecture
status: stable
created: 2026-11-10
updated: 2026-11-15
stale_after: 90d
tags: [agents, mcp]
links:
  - bf:///concepts/welcome.md?rel=follows#conventions
description: Open standards governing agent protocols and tool integrations.
---

# Agentic AI Foundation
```

- `created` and `updated` (both accepting valid calendar `YYYY-MM-DD` dates or ISO 8601 timestamps with timezone), `description`, and `stale_after` (explicit duration like `14d` or `90d`, or ISO date) unify metadata across notes and ingested records; `updated` (falling back to `created`) and `stale_after` drive read-only `bf review` freshness checks (`newer_evidence` and `due`).
- Every custom frontmatter field passes straight through to the compiled JSONL record in `memories/_notes.jsonl`. Computed and reserved keys (`id`, `ref`, `source`, `kind`, `text`, `headings`, `tasks`, `sha256`, `ingested_at`) are rejected in frontmatter.
- Bundled `record` is enforced unconditionally across all notes and memories (`schemas/record.json` is reserved and rejected). Per-source `schemas/<source>.json` files are optional: `bf init` scaffolds starter note schemas (`concepts`: `draft|stable|deprecated`; `projects`: `active|paused|done|archived`; `actions`: `open|done|cancelled` with required `created`), and memory sources only need `schemas/<source>.json` (or explicit `ingest.schema:` / `--schema`) when you want extra domain constraints.

## Heading anchors, tasks, and link metadata

Use CommonMark `attr_list` syntax where it adds real signal (heading `#id` anchors and typed links) alongside standard checklist tasks:

<!-- dprint-ignore-start -->
```markdown
## Model Context Protocol {#mcp creator=Anthropic}

Standard wire protocol for tools and context. See [AAIF](aaif.md#mcp){rel=part-of}.

- [ ] Audit server permissions.
```
<!-- dprint-ignore-end -->

- **Headings (`headings[]`)**: `## MCP {#mcp creator=Anthropic}` populates `headings` (`{id, title, level, meta}`) and gives `bf read path#mcp` a stable section anchor across title edits (both ATX and Setext headings use one exact CommonMark AST line map). Separate attributes with spaces (`{#mcp?creator=Anthropic}` is rejected). Headings inside blockquotes are ignored so quoted excerpts never shift anchors.
- **Tasks (`tasks[]`)**: Markdown checklist items (`- [ ] ...`, `- [x] ...`, supporting both tight and loose `{: key=val}` attributes, never triggered by inline code spans) populate `tasks` (`{text, done, section, meta}`) and drive `total_tasks_open` in `bf review`.
- **Links (`links[]`)**: attach edge metadata via `{rel=part-of}` right after `)` in Markdown prose, `?rel=related-to` query parameters on note / `bf://` / non-URL `source:id` links (ideal for YAML frontmatter), or `{to, meta}` objects in frontmatter/sensor JSON. All three normalize into the same `{to, kind, anchor, meta}` record shape.

## Connect notes and records {#connect-notes-and-records}

Authored notes and ingested JSONL records share one normalized `links` list (`note`, `record`, `brain`, `url`) addressed by universal human-readable `ref`:

- **Markdown prose**: link to local notes (`../concepts/aaif.md#mcp`, `#mcp`), tracked non-private repo files in the same brain (`../README.md`, `../AGENTS.md`, `bf:///bf.yaml`), records by `source:id` (`[Issue 7](github-issues:7){rel=cites}`), declared sibling brains (`bf://team-brain/concepts/aaif.md#mcp`, `bf://team-brain/jira:WEB-1`), or external URLs (`https://`, `http://`, `mailto:`, `urn:`).
- **Note frontmatter & JSON (`links`)**: accept brain-root note paths, `source:id`, `bf://` URIs, external URLs, or `{to, meta}` objects. Cross-brain `bf://<host>/...` targets must declare `<host>` under `brains:` in `bf.yaml`. Same-brain authored notes may reference tracked non-private brain files outside `logs/`, `memories/`, and dotfiles, while `bf read`, ingested memory records, and cross-brain `bf://` links stay confined to configured `notes:` folders. Record IDs cannot contain `#`, non-URL `?`, or whitespace, and schemes containing `://` other than `https`/`http`/`bf` are rejected rather than misclassified as record refs.
- **Validation**: `bf check` is strictly read-only. Run `bf build` after editing Markdown (unchanged notes are reused automatically), then `bf check` to verify compiled-note sync, optional schemas, `memories/`, enabled `loops:` (including explicit `run:` script paths), links, `#anchor` headings, and referenced cross-brain targets. Missing targets in snapshot-ingested sources, `.forgotten`, or unreferenced sibling `brains:` paths emit warnings (`ok: true` unless `--strict`).

## Flat notes and `index.md` bundles

Any subdirectory inside a configured `notes:` folder that contains `index.md` is treated as a bundle note: `bf build` compiles only `index.md` and ignores every sibling file or subdirectory (such as `inputs/`, `outputs/`, attachments, or scripts). Use clean semantic slugs (`actions/<slug>/index.md` or `<dir>/<slug>.md`) and keep creation dates in frontmatter (`created: YYYY-MM-DD`) so links stay stable if an action is rescheduled (append a cadence suffix like `retention-review-2026-q4` only when recurring slugs collide).
