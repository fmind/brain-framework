# Four-tool walkthrough

Answer one question across Google Workspace, Jira, GitHub, and Google Cloud fixtures: **"Can the New website launch, what remains, and why did we choose one page?"** Everything runs offline from local JSON fixtures.

| Path                      | Purpose                                                                                               |
| ------------------------- | ----------------------------------------------------------------------------------------------------- |
| `bf.yaml`                 | YAML configuration (`version: 17`, `name: context-demo`).                                             |
| `schemas/*.json`          | Required per-source JSON Schemas (`projects`, `workspace`, `jira`, `github`, `gcloud`).               |
| `fixtures/*.json`         | Offline JSON fixtures for `workspace`, `jira`, `github`, `gcloud`, plus `jira-done.json`.             |
| `projects/new-website.md` | Authoritative Markdown project note with heading `#id` anchors, link `{rel=...}` metadata, and tasks. |
| `sensors/demo.py`         | Normalizes `fixtures/TOOL.json` into unified JSON records piped into `bf ingest`.                     |
| `evals/smoke.py`          | Runnable Python/pytest retrieval check invoking `bf query` and `bf read`.                             |

## Run on a disposable copy

From a repository checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
context_demo=$(mktemp -d)
cp -R examples/context-hub "$context_demo/brain"
cd "$context_demo/brain"
bf() { uv run --project "$bf_checkout" bf "$@"; }
```

## 1. Ingest four tools and compile Markdown to JSONL

```bash
for tool in workspace jira github gcloud; do
  python3 sensors/demo.py "$tool" 2026-09-26T12:00:00Z | bf ingest "$tool" --mode snapshot
done
bf build
bf check
```

`bf check` reports `"ok":true` with `1` note and `4` records. Both the compiled Markdown project (`memories/projects.jsonl`) and the four sensor sources (`memories/{workspace,jira,github,gcloud}.jsonl`) live as flat JSONL files under `memories/`.

## 2. Query JSONL records (`bf query`, `jq`, `rg`)

```bash
# Every record linked to projects/new-website.md across all four tools
bf query --jq 'select(.kind == "memory" and any(.links[]; .to == "projects/new-website.md")) | {ref, title}'

# Inspect the blocking Jira review via bf read
bf read "jira:review"

# Summarize all sources via jq
bf query --jq '{source, kind, ref, title}'
```

`jira:review` shows `"status":"In review"` because the keyboard navigation check is still open, even though `github:implementation` is `"merged"` and `gcloud:deployment` is `"healthy"`.

## 3. Record the launch hold decision

Update `projects/new-website.md` dated `2026-09-27` citing all four records with `attr_list` metadata, rebuild `memories/projects.jsonl`, and check `bf review`:

```bash
cat > projects/new-website.md <<'EOF'
---
type: project
status: draft
updated: 2026-09-27
tags: [launch, web]
summary: Launch a product website that explains the product before signup.
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
