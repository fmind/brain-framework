# Authoring

Humans write plain Markdown in `projects/` (volatile TODOs and active initiatives), `concepts/` (durable knowledge), and `actions/YYYY-MM-DD_topic/ACTION.md` (dated task workspaces). `bf build` parses every note with [Python-Markdown](https://python-markdown.github.io/) (`extra`, `toc`, `admonition`, `sane_lists`) and writes individual JSON files to `memories/{concepts,projects,actions}/<slug>.json`, validated by `schemas/record.json`, `schemas/concepts.json`, and optional `schemas/<source>.json`.

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

Authored notes follow **two link conventions** so Markdown links work in editors/GitHub while frontmatter links stay canonical URIs:

- **Markdown prose (relative paths + cross-brain/external URLs)**: link to local notes (`../concepts/aaif.md#mcp`, `aaif.md`, `#mcp`) and sensor memories (`../memories/github-issues/7902699be42c8a8e46fbbb4501726517e86b22c56a189f7625a6da49081b2451.json#.title`) with relative paths, and use `bf://<brain>/...` or external URLs when crossing brains or linking outside the brain.
- **Note frontmatter (`links`, `sources`, `relations`)**: requires `bf://` or external URLs — same-brain `bf:///concepts/aaif.md?rel=project-of#mcp` and `bf:///memories/<source>/<sha256>.json`, or cross-brain `bf://team-brain/concepts/aaif.md#mcp` (resolved via `brains:` in `bf.yaml`, `../team-brain`, or `~/team-brain`). Sensor JSON `links` accept `bf://` URLs, external URLs, brain-relative paths, or `source:id` refs.
- **`jq` fragment pointers**: `<bf:///memories/concepts/welcome.json#select(.status=="stable")>` resolves the target JSON file and applies the `#<jq_filter>` fragment on `bf read` (syntax-checked by `bf validate`).
- **External URIs**: `https://`, `http://`, `mailto:`, and `urn:` keep `kind: "url"`. Legacy `source:id` (`github-issues:7`) and 2-slash `bf://concepts/...` note links are rejected in authored notes (`bf read "github-issues:7"` still looks up records on the CLI).

## Tracked actions and user schemas

Create or reuse a dated workspace with `bf action "Retention review"` (`actions/YYYY-MM-DD_retention-review/ACTION.md`). Mark finished actions `status: stable` to clear them from `bf review`'s active inbox. Customize `schemas/concepts.json`, `schemas/record.json`, or any `schemas/<source>.json` file to enforce your brain's ontology during `bf build`, `bf ingest`, and `bf validate`.
