# Getting started

Create a brain, ingest memory and usage records, author a Markdown decision, compile it with `bf build`, and query flat JSONL records offline with `bf`, `rg`, `jq`, and DuckDB.

## Prerequisites

- Linux or macOS
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (for Python 3.14 tool installation)
- Standard UNIX tools: [`ripgrep` (`rg`)](https://github.com/BurntSushi/ripgrep) and [`jq`](https://jqlang.org/) (plus optional [DuckDB](https://duckdb.org/) for SQL analytics over `memories/*.jsonl`, `usages/*.jsonl`, and `logs/*.jsonl`)

## Install and initialize a brain

```bash
uv tool install --python 3.14 brain-framework
bf init ~/brain
cd ~/brain
bf check
bf status --format text
```

`bf init` scaffolds the 3x3 layout (`concepts/`, `projects/`, `actions/`, `sensors/`, `routines/`, `agents/`, `memories/`, `usages/`, `logs/`, `schemas/`, `evals/`) with starter schemas (`schemas/{concepts,projects,actions,usages}.json`), a runnable Python eval (`evals/smoke.py`), and compiled `memories/concepts.jsonl`. Core schemas (`bf`, `record`) ship inside `bf` and print anytime with `bf schema`.

## Ingest records and author a Markdown note

Every memory source requires `schemas/<source>.json` (fail closed); every usage source under `usages/` validates against `schemas/usages.json` (plus optional `schemas/<source>.json`):

```bash
printf '{"type":"object"}\n' > schemas/workspace.json
printf '%s\n' '[{"id":"brief","title":"Website brief","text":"Visitors need one clear product page before signup.","time":"2026-09-20T09:00:00Z"}]' | bf ingest workspace

# Track token/FinOps usage separately under usages/
printf '%s\n' '{"id":"sess-1","title":"Kickoff","harness":"agy","model":"gemini","input_tokens":1200,"output_tokens":340,"cost_usd":0.01}' | bf ingest --to usages agy
```

Write `projects/new-website.md` with heading anchors and typed links, compile with `bf build`, and verify read-only with `bf check`:

```bash
cat > projects/new-website.md <<'EOF'
---
type: project
status: draft
updated: 2026-09-25
tags: [launch, web]
summary: Launch a single-page product site.
---

# New website

## Decision {#decision owner=product}

Start with one product page so visitors understand who the product helps before signing up. Evidence: [Workspace brief](workspace:brief){rel=cites} and [Conventions](../concepts/welcome.md#conventions){rel=follows}.

## Next actions

- [ ] Draft the hero copy.
EOF
bf build
bf check
```

## Query JSONL records (`bf query`, `bf read`, `bf review`)

```bash
# Lexical search across memories/*.jsonl (rg)
bf query "product page"

# Filter heading metadata with jq
bf query --jq 'select(.kind == "note" and any(.headings[]?; .meta.owner == "product")) | {ref, title, headings}'

# Inspect usage telemetry separately from semantic memories
bf query --kind usage --scope usages --jq '{source, harness, input_tokens, cost_usd}'

# Read compiled record, exact #anchor section, or raw Markdown
bf read projects/new-website.md | jq '.record.headings'
bf read "bf:///projects/new-website.md#decision"
bf read "workspace:brief" | jq '.record'

# Check stale notes, newer evidence, and open tasks (read-only)
bf review
```

## Connect your coding harness

There is zero global config file. `bf` resolves `--brain PATH` -> nearest enclosing `bf.yaml` -> `BF_BRAIN` -> `$BRAIN_LOCATION` -> `~/brain`. Inside a brain directory, harnesses orient with `bf status` and `bf review` and author notes directly; outside a brain directory, set `export BRAIN_LOCATION="$HOME/my-brain"` (or use `~/brain`) and invoke `bf` through the `/bf` router skill.

## Upgrade and pinned runtimes {#upgrade-and-pinned-runtimes}

Upgrade the installed CLI with `uv tool upgrade brain-framework`. When upgrading across a major format version, follow the manual migration steps under `Breaking changes` in the [changelog](https://fmind.github.io/brain-framework/changelog/), then run `bf run --prune && bf build && bf check`.
