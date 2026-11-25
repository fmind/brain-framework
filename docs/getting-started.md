---
icon: lucide/rocket
description: Create a brain, ingest a record, write a Markdown decision with heading anchors and typed links, and search with BM25 bf query, bf read, and bf review.
---

# Getting started

Create a brain, ingest memory records, author a Markdown decision, compile it with `bf build`, and search ranked BM25 results offline with `bf`.

## Prerequisites

- Linux or macOS
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python 3.12+ tool installation)
- Optional UNIX tools for direct JSONL inspection and SQL analytics: [`ripgrep` (`rg`)](https://github.com/BurntSushi/ripgrep), [`jq`](https://jqlang.org/), and [DuckDB](https://duckdb.org/)

## Install and initialize a brain

```bash
uv tool install brain-framework
bf init ~/brain
cd ~/brain
bf check --format text
bf status --format text
```

`bf check` prints `[OK]` and `bf status` prints `[HEALTHY]` with `notes=1`. (If `BF_BRAIN` is already exported in your shell, unset it or pass `--brain ~/brain`.)

`bf init` scaffolds the [3x3 brain layout](index.md#the-3x3-layout), starter note schemas, a smoke test in `evals/smoke.py`, compiled `memories/_notes.jsonl`, and the disposable SQLite FTS5 cache. Core `bf` and `record` schemas ship inside `bf` and print anytime with `bf schema`.

## Ingest records and author a Markdown note

Every record is validated automatically against the bundled `record` schema (`bf schema record`):

```bash
printf '%s\n' '[{"id":"brief","title":"Website brief","type":"doc","text":"Visitors need one clear product page before signup.","created":"2026-09-20T09:00:00Z"}]' | bf ingest workspace

# Track token/FinOps telemetry in the same unified memories/ store
printf '%s\n' '{"id":"sess-1","title":"Kickoff","type":"session","harness":"claude","model":"sonnet","input_tokens":1200,"output_tokens":340,"cost_usd":0.01}' | bf ingest claude
```

Add an optional `schemas/<source>.json` file (or pass `--schema NAME`) only when you want extra domain validation beyond the core `record` schema.

Write `projects/new-website.md` with heading anchors and typed links, compile with `bf build`, and verify read-only with `bf check`:

```bash
cat > projects/new-website.md <<'EOF'
---
type: project
status: active
created: 2026-11-15
updated: 2026-11-15
tags: [launch, web]
description: Launch a single-page product site.
---

# New website

## Decision {#decision owner=product}

Start with one product page so visitors understand who the product helps before signing up. Evidence: [Workspace brief](workspace:brief){rel=cites} and [Conventions](../concepts/welcome.md#conventions){rel=follows}.

## Next actions

- [ ] Draft the hero copy.
EOF
bf build
bf check --format text
```

`bf build` prints `{"ok":true,...,"written":1,...}` and `bf check` reports `[OK]` with `notes=2` and `memories=2`.

## Search, read, and review (`bf query`, `bf read`, `bf review`)

```bash
# Ranked BM25 full-text search (authored notes boosted above raw memories)
bf query "product page" --format text

# Backlinks: find every note or record linking to a target
bf query --links-to workspace:brief --format text

# Structured filtering over notes or telemetry records
bf query --kind note --tag launch --format jsonl
bf query --type session --scope claude

# Read compiled record, exact #anchor section, or raw Markdown (pipe JSON to jq optionally)
bf read projects/new-website.md
bf read "bf:///projects/new-website.md#decision"
bf read projects/new-website.md --format raw
bf read "workspace:brief"

# Check stale notes, newer evidence, and open tasks (read-only)
bf review --format text
```

`bf query "product page"` ranks `projects/new-website.md` ahead of `workspace:brief`, `bf query --links-to workspace:brief` returns `projects/new-website.md`, and `bf review` reports `[FRESH]` with `open_tasks=1`.

## Connect your coding harness

`bf` uses zero global config and resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `~/brain`:

- **Inside a brain**: Coding harnesses read the repo's `AGENTS.md`, orient with `bf status` and `bf review`, and edit Markdown notes directly.
- **Outside a brain**: Set `export BF_BRAIN="$HOME/my-brain"` (or use `~/brain`) and copy [`skills/bf/SKILL.md`](https://github.com/fmind/brain-framework/blob/main/skills/bf/SKILL.md) into your harness's skills folder so `/bf` routes commands to your brain.

## Upgrade and pinned runtimes {#upgrade-and-pinned-runtimes}

Upgrade the installed CLI with `uv tool upgrade brain-framework`. Before crossing a major format version, back up your brain and follow `Breaking changes` in the [changelog](changelog.md). Then preview cleanup with `bf state prune --dry-run` before running `bf state prune && bf build && bf check`.

## Next steps

- [Authoring](authoring.md): Write folder bundles (`<slug>/index.md`), cross-brain `bf://` links, and optional domain schemas.
- [Retrieval](retrieval.md): Combine `bf query` backlinks with direct `rg`, `jq`, and DuckDB queries over `memories/*.jsonl`.
- [Operations](operations.md): Automate sensors, routines, and agents with explicit `loops:` and headless `bf watch`.
