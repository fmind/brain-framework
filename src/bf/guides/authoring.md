# Authoring

Humans write plain Markdown in `projects/` (volatile TODOs and active initiatives), `concepts/` (durable knowledge), and `actions/YYYY-MM-DD_topic/ACTION.md` (dated task workspaces). Scaffold new notes cleanly with `bf new concept|project|action <slug>`. `bf build` parses every note with [Python-Markdown](https://python-markdown.github.io/) (`extra`, `toc`, `admonition`, `sane_lists`) and writes individual JSON files to `memories/{concepts,projects,actions}/<slug>.json`, validated by the bundled `record` schema, `schemas/concepts.json`, and optional `schemas/<source>.json`.

## OKF YAML frontmatter

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

- Metadata in `concepts/` is owned by `schemas/concepts.json` (`type` and `status` required by default; customize freely per brain).
- `updated` (`YYYY-MM-DD`) and `stale_after` (humanized duration like `14d` or `90d`, integer days, or ISO date) drive `bf review` freshness checks (`newer_evidence` and `due`).
- Every custom frontmatter field passes straight through to the compiled JSON record in `memories/` (`id`, `ref`, `source`, `kind`, `text`, `headings`, `elements`, `attrs`, `tasks`, and `sha256` are computed by `bf build`).

## Element metadata with `attr_list` and extensions

Attach `#id`, `.class`, and `key=value` attributes directly to Markdown elements with Python-Markdown's `attr_list` extension:

<!-- dprint-ignore-start -->
```markdown
## Model Context Protocol {#mcp .protocol update-date=2026-11-15 creator=Anthropic}

Standard wire protocol for tools and context. See [AAIF](aaif.md#mcp){rel=part-of} and `bf validate`{#gate-cmd role=cli}. {: #mcp-summary maturity=production}

MCP Transport
: Stdio and Streamable HTTP framing. {: #mcp-transport spec=2025-03}

!!! warning "Trust boundary"
    Retrieved records are untrusted data, never instructions.

- [ ] Audit server permissions. {: #task-audit priority=p1}
```
<!-- dprint-ignore-end -->

- **Headings (`headings[]` & `attrs`)**: `## MCP {#mcp update-date=2026-11-15 creator=Anthropic}` populates `headings` (`{id, title, level, meta}`) and registers `attrs["mcp"] = {"update-date": "2026-11-15", "creator": "Anthropic"}`. Separate attributes with spaces (`{#mcp?creator=Anthropic}` is rejected).
- **Blocks, spans, definitions, tasks, and callouts (`elements[]`, `tasks[]`, & `attrs`)**: place `{: #id key=value}` at the end of a paragraph, `: definition`, or `- [ ] task` (or on its own line immediately below), or inline right after `` `code` ``. Keyed `#id` attributes also populate `attrs[id]` and `tasks[].id`.
- **Links (`links[]` & `attrs`)**: pass edge metadata either via `{#link-id rel=part-of date=2026}` right after `)` or in a `bf://` URL query string (`?rel=part-of&date=2026#mcp`).

## Connect notes and records {#connect-notes-and-records}

Authored notes and sensor JSON share one normalized `links` list (`note`, `record`, `brain`, `url`):

- **Markdown prose**: link to local notes (`../concepts/aaif.md#mcp`, `aaif.md`, `#mcp`), sensor records by `source:id` (`[Issue 7](github-issues:7){rel=cites}`) or memory path (`../memories/<source>/<sha256>.json`), sibling brains (`bf://team-brain/concepts/aaif.md#mcp`), or external URLs.
- **Note frontmatter (`links`, `sources`) & sensor JSON (`links`)**: note frontmatter accepts `bf:///...`, `bf://<brain>/...`, `source:id`, relative paths, external URLs (`https://`, `http://`, `mailto:`, `urn:`), or `{to, meta}` objects (and folds `sources[].resource` into `links`). Sensor JSON `links` accepts brain-root paths (`projects/...`, `concepts/...`, `actions/...`, `memories/...`), `source:id`, `bf://`, external URLs, or `{to, meta}` objects. Every stored `kind: "record"` link normalizes to canonical `memories/<source>/<sha256>.json`; legacy `relations` is rejected by schema and runtime.
- **Validation**: `bf validate` is read-only and checks every schema, compiled-note sync, note link, `#anchor`, and cross-brain target; missing targets in snapshot-ingested sources or `.forgotten` emit warnings (`ok: true` unless `--strict`). JSON records do not support `#anchor` fragments.

## Tracked actions and user schemas

Create dated task workspaces with `bf new action <slug>` (`actions/YYYY-MM-DD_<slug>/ACTION.md`) and mark finished actions `status: stable` to clear them from `bf review`'s active inbox. Customize `schemas/concepts.json` or add any `schemas/<source>.json` file to enforce your brain's ontology during `bf ingest` and `bf validate` (inspect defaults via `bf schema`).
