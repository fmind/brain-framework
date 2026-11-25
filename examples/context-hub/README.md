# Four-tool walkthrough

Answer one question across Google Workspace, Jira, GitHub, and Google Cloud fixtures: **"Can the New website launch, what remains, and why did we choose one page?"** Everything runs offline from local JSON fixtures.

| Path                      | Purpose                                                                                                |
| ------------------------- | ------------------------------------------------------------------------------------------------------ |
| `bf.yaml`                 | YAML configuration (`version: 19`, `name: context-demo`).                                              |
| `schemas/*.json`          | Optional domain JSON Schemas (`projects.json`, `jira.json`); sources without one use bundled `record`. |
| `fixtures/*.json`         | Offline JSON fixtures for `workspace`, `jira`, `github`, `gcloud`, plus `jira-done.json`.              |
| `projects/new-website.md` | Authoritative Markdown project note with heading `#id` anchors, link `{rel=...}` metadata, and tasks.  |
| `sensors/demo.py`         | Normalizes `fixtures/TOOL.json` into unified JSON records piped into `bf ingest`.                      |
| `evals/smoke.py`          | Runnable Python/pytest retrieval check invoking `bf query` and `bf read`.                              |

## Run on a disposable copy

From a repository checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
context_demo=$(mktemp -d)
cp -R examples/context-hub "$context_demo/brain"
cd "$context_demo/brain"
bf() { env -u BF_BRAIN uv run --project "$bf_checkout" bf "$@"; }
```

## 1. Ingest four tools and compile Markdown to JSONL

```bash
for tool in workspace jira github gcloud; do
  python3 sensors/demo.py "$tool" 2026-09-26T12:00:00Z | bf ingest "$tool" --mode snapshot
done
bf build
bf check
```

`bf check` reports `"ok":true` with `1` note and `4` memories. Compiled notes live in `memories/_notes.jsonl`, the four sensor sources live in `memories/{workspace,jira,github,gcloud}.jsonl`, and `.bf/cache/index.sqlite3` indexes all of them for fast BM25 search.

## 2. Query records (`bf query`, `bf read`, `jq`)

```bash
# Ranked BM25 search across notes and all four tools
bf query "keyboard navigation" --format text

# Filter memory records with structured flags
bf query --kind memory --format jsonl

# Inspect the blocking Jira review via bf read
bf read "jira:review"

# Inspect link graph directly on flat JSONL via jq
jq 'select(.kind == "memory" and any(.links[]; .to == "projects/new-website.md")) | {ref, title}' memories/*.jsonl
```

`jira:review` shows `"status":"In review"` because the keyboard navigation check is still open, even though `github:implementation` is `"merged"` and `gcloud:deployment` is `"healthy"`.

## 3. Record the launch hold decision

Update `projects/new-website.md` dated `2026-09-27` citing all four records with `attr_list` metadata, rebuild `memories/_notes.jsonl`, and check `bf review`:

```bash
cat > projects/new-website.md <<'EOF'
---
type: project
status: active
created: 2026-09-22
updated: 2026-09-27
tags: [launch, web]
description: Launch a product website that explains the product before signup.
---

# New website

## Decision {#decision owner=product}

Start with a single product page: visitors need a clear explanation before signing up. Evidence: [Workspace brief](workspace:brief){rel=cites}.

## Launch review {#launch-review gate=accessibility}

Hold public launch. The [implementation](github:implementation){rel=implements} is merged and the [preview](gcloud:deployment){rel=verifies} is healthy, but the [launch review](jira:review){rel=blocks} still blocks launch on keyboard navigation.

## Next actions

- [ ] Run the keyboard navigation check.
  {: priority=p1}
EOF
bf build
bf check
bf review
```

Because `updated: 2026-09-27` is newer than the `2026-09-21..26` evidence timestamps, `bf review` reports `"stale":[]` and `"total_tasks_open":1`.

## 4. Detect upstream evidence changes with `bf review`

Advance the Jira fixture to `Done` at `2026-09-28T10:00:00Z` and re-ingest `jira`:

```bash
cp fixtures/jira-done.json fixtures/jira.json
python3 sensors/demo.py jira 2026-09-28T10:00:00Z | bf ingest jira --mode snapshot
bf review
```

`bf review` immediately flags `projects/new-website.md` with `"reasons":["newer_evidence"]` and `"newer":["jira:review"]`.

Clean up the temporary directory when finished:

```bash
cd "$bf_checkout"
unset -f bf
rm -rf -- "$context_demo"
```
