# Overview

Brain Framework (`bf`) is a UNIX-native, local-first knowledge brain built on three formats (**YAML for config**, **Markdown for humans**, **JSONL for records**) and a **3x3 folder convention**, with zero database, zero external indexer, and zero built-in LLMs. Query flat JSONL directly with `rg`, `jq`, DuckDB, or the `bf` CLI.

![Brain Framework loop](https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-loop.svg)

## 3x3 Convention Architecture

1. **Notes (OKF YAML frontmatter + Markdown):** `concepts/` (durable knowledge), `projects/` (ongoing initiatives), and `actions/` (ad-hoc requests, supporting flat `<slug>.md` and universal `<slug>/index.md` bundles). Attach `#id` anchors (`## MCP {#mcp owner=alice}`) and `{rel=...}` link metadata (`[Ticket](jira:WEB-1){rel=cites}`, `bf:///concepts/welcome.md`).
2. **Loops (shell `.sh`, Python `.py`, or any executable):** `sensors/` (deterministic collectors auto-ingesting stdout JSONL), `routines/` (maintenance and reports), and `agents/` (coding-harness/LLM tasks). Every loop receives `BF_*` env + `stdin` JSON context, writes data/events as JSONL on `stdout`, and writes structured JSONL logs on `stderr` into `logs/<loop>.jsonl`.
3. **Records (schema-validated JSONL):** `memories/<source>.jsonl` (compiled notes + sensor memories), `usages/<source>.jsonl` (token, FinOps, and prompt telemetry), and `logs/<loop>.jsonl` + `logs/events.jsonl` (private execution logs and event bus). Core `bf` and `record` schemas ship inside `bf`; `schemas/<source>.json` and `schemas/usages.json` enforce domain contracts. Optional `clients/` and `prompts/` folders hold ad-hoc API scripts and LLM prompts by convention.

## Guides

- [Getting started](getting-started.md): initialize a brain, ingest memories and usages, author a note, compile with `bf build`, and query with `bf query`, `bf read`, `bf review`, and `bf status --format text`.
- [Authoring](authoring.md): organize Markdown across `notes:` folders (flat notes and `<slug>/index.md` bundles), heading `#id` anchors, universal `ref` & `bf://` links, and required JSON Schemas.
- [Retrieval](retrieval.md): orient with read-only `bf status` and `bf review`, resolve refs with `bf read`, query `memories/*.jsonl` and `usages/*.jsonl` via `rg`, `jq`, and DuckDB (`bf query`), and verify with `bf check`.
- [Operations](operations.md): compose `loops:` with `sensors/`, `routines/`, and `agents/` conventions, `stdout`/`stderr` JSONL streams, `logs/events.jsonl`, `bf watch`, and security boundaries.
- [Reference](reference.md): exact CLI commands (strict read/write split), exit codes, 3x3 layout, `bf.yaml` (`version: 18`), and published JSON Schemas (`bf schema`).
