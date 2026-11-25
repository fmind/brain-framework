---
type: guide
status: stable
created: 2026-11-15
updated: 2026-11-15
tags: [getting-started]
description: Starter concept showing YAML frontmatter, heading anchors, unified links, and JSONL memories.
---

# Welcome

YAML configures the brain (`bf.yaml`), Markdown stays readable for humans (`concepts/`, `projects/`, `actions/`), loops run shell or Python scripts (`sensors/`, `routines/`, `agents/`), and `bf build` / `bf ingest` store flat JSONL records under `memories/` (indexed into disposable `.bf/cache/index.sqlite3` for BM25 `bf query` and directly queryable with `rg`, `jq`, and `duckdb`).

## Conventions {#conventions scope=brain}

Attach `#id` anchors on headings and `{rel=...}` or `?rel=...` metadata on links, link uniformly across prose and frontmatter using relative paths like [Conventions](#conventions){rel=self}, `<source>:<id>`, or `bf://` URIs (`bf:///concepts/welcome.md`), and run `bf build && bf check` after edits.
