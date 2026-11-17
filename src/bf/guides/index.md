# Overview

Brain Framework (`bf`) keeps a local-first knowledge brain built around three rules: **YAML for config**, **JSON for records**, and **Markdown for humans**. `bf` compiles Markdown notes and sensor payloads into individual Draft 2020-12 schema-validated JSON files (`memories/**/*.json`) that humans and AI agents query offline with `rg` and `jq` (`bf query`, `bf read`, or `bf mcp`).

![Brain Framework loop](https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-loop.svg)

## Three-format architecture

1. **YAML for config.** Configure timezone, environment, jobs, channels, services, and watch settings in `bf.yaml` (`version: 13`, humanized durations like `15m` or `1d`, cron expressions, optional gitignored `.env`), deterministic checks in `evals/*.yaml`, and note frontmatter in YAML 1.2.
2. **Markdown for humans.** Track volatile TODOs in `projects/`, durable knowledge in `concepts/`, dated workspaces in `actions/YYYY-MM-DD_topic/ACTION.md`, and input prompts in `prompts/`. Attach structured metadata directly to headings (`## MCP {#mcp license=mit}`), links (`[AAIF](../concepts/aaif.md#mcp){rel=depends-on}` in prose, `bf:///concepts/aaif.md` in frontmatter), tasks, and blocks (`{: #decision-1 confidence=high}`) using Python-Markdown's `attr_list` extension.
3. **JSON for records.** `bf build` compiles Markdown notes into `memories/{concepts,projects,actions}/<slug>.json` and `bf ingest SOURCE` writes sensor records into `memories/<source>/<sha256(id)>.json` — validated by user-owned JSON Schemas in `schemas/` — while runtime orchestration keeps state in `.bf/` and writes `structlog` JSON lines to `logs/<name>.log` and `logs/events.log`.

## Guides

- [Getting started](getting-started.md): create a brain, ingest evidence, author a project note with `attr_list` metadata, and query `memories/` with `bf query` and `bf read`.
- [Authoring](authoring.md): write OKF Markdown with Python-Markdown extensions (`attr_list`, `footnotes`, `tables`, `def_list`, `admonition`), relative prose links, `bf://` frontmatter URLs, and `schemas/*.json`.
- [Retrieval](retrieval.md): orient with `bf status` and `bf review`, resolve `bf://` URLs with `bf read`, query JSON records via `rg` and `jq`, and run `bf mcp` or `bf eval`.
- [Operations](operations.md): schedule `jobs`, wire `channels`, supervise `services` with `bf watch`, validate Git hooks, and preserve security boundaries.
- [Reference](reference.md): exact CLI flags, exit codes, flat folder layout, `bf.yaml` (`version: 13`), and published JSON Schemas.
