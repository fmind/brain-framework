# Four-tool walkthrough

Answer one question across Google Workspace, Jira, GitHub, and Google Cloud fixtures: **"Can the New website launch, what remains, and why did we choose one page?"** Everything runs offline from local JSON fixtures.

| Path                      | Purpose                                                                                              |
| ------------------------- | ---------------------------------------------------------------------------------------------------- |
| `bf.yaml`                 | YAML configuration (`version: 13`, `name: context-demo`).                                            |
| `schemas/jira.json`       | Per-source JSON Schema enforcing `type`, `status`, and `project` on `memories/jira/*.json`.          |
| `fixtures/*.json`         | Offline JSON fixtures for `workspace`, `jira`, `github`, `gcloud`, plus `jira-done.json`.            |
| `projects/new-website.md` | Authoritative Markdown project note with `attr_list` metadata on headings, links, blocks, and tasks. |
| `sensors/demo.py`         | Normalizes `fixtures/TOOL.json` into unified JSON records piped into `bf ingest`.                    |

## Run on a disposable copy

From a repository checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
context_demo=$(mktemp -d)
cp -R examples/context-hub "$context_demo/brain"
cd "$context_demo/brain"
bf() { uv run --project "$bf_checkout" bf "$@"; }
```

## 1. Ingest four tools and compile Markdown to JSON

```bash
for tool in workspace jira github gcloud; do
  python3 sensors/demo.py "$tool" 2026-09-26T12:00:00Z | bf ingest "$tool" --mode snapshot
done
bf build
bf validate
bf eval
```

`bf validate` reports `"ok":true` with `1` note and `4` records. Both the compiled Markdown project (`memories/projects/new-website.json`) and the four sensor folders (`memories/{workspace,jira,github,gcloud}/*.json`) live as individual JSON files under `memories/`.

## 2. Query pure JSON records (`bf query`, `jq`, `rg`)

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

Update `projects/new-website.md` dated `2026-09-27` citing all four records with `attr_list` metadata, rebuild `memories/projects/new-website.json`, and check `bf review`:

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

Start with a single product page: visitors need a clear explanation before signing up. Evidence: [Workspace brief](../memories/workspace/29a8825bd242f14386ee528d76e0e8f1e38f3c8c4047d7b2d6df7493368a17d0.json){rel=cites}.
{: #single-page confidence=high}

## Launch review {#launch-review gate=accessibility}

Hold public launch. The [implementation](../memories/github/da31012c40330e7e21538e7dd57503b16e8a0839159e96137090cccc9910b171.json){rel=implements} is merged and the [preview](../memories/gcloud/aee50b18a03c89e4883a5acadf5c8614dd7fd2f111c3b616e1a8d6427edc3e3f.json){rel=verifies} is healthy, but the [launch review](../memories/jira/c97ace4c8fef2cee8fa0f3c9f52aab18dbd4f42438afe362ffb8f75ce4c04b84.json){rel=blocks} still blocks launch on keyboard navigation.

## Next actions

- [ ] Run the keyboard navigation check.
  {: #task-a11y priority=p1}
EOF
bf build
bf validate
bf review
```

Because `updated: 2026-09-27` is newer than the `2026-09-21..26` evidence timestamps, `bf review` reports `"stale":[]` and `"tasks_open":1`.

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
