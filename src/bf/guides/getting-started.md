# Getting started

Create a brain, ingest one piece of evidence, author a Markdown decision with `attr_list` metadata, compile it to JSON, and query `memories/` with `bf query`, `bf read`, `bf review`, and `bf check`.

## Prerequisites

- Linux or macOS
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (for Python 3.14 tool installation)
- Standard CLI tools for retrieval: [`ripgrep` (`rg`)](https://github.com/BurntSushi/ripgrep) and [`jq`](https://jqlang.org/)

## Install and initialize a brain

```bash
uv tool install --python 3.14 brain-framework
bf init ~/brain
cd ~/brain
bf check
```

`bf init` creates a minimal flat layout (`projects/`, `concepts/`, `actions/`, `memories/`, `logs/`, `evals/`, `skills/bf/SKILL.md` linked at `.agents/skills/bf`, with optional `schemas/`, `sensors/`, `routines/`, `loops/`, `prompts/`, `settings/`, and `docs/` added as needed) and compiles `concepts/welcome.md` into `memories/concepts/welcome.json` plus `.bf/cache/index/`. Core schemas (`bf`, `record`, `concepts`, `evals`) ship inside `bf` and print anytime with `bf schema`.

## Ingest evidence and author a Markdown note

Pipe one sensor record into `memories/workspace/<sha256>.json`:

```bash
printf '%s\n' '[{"id":"brief","title":"Website brief","text":"Visitors need one clear product page before signup.","time":"2026-09-20T09:00:00Z"}]' | bf ingest workspace
```

Scaffold and write `projects/new-website.md` using Python-Markdown `attr_list` metadata and links, then verify in one pass with `bf check`:

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
{: #launch-scope confidence=high}

## Next actions

- [ ] Draft the hero copy.
  {: #task-hero assignee=alice}
EOF
bf check
```

## Query pure JSON records (`bf query`, `bf read`, `bf review`)

```bash
# Lexical search across all compiled note and sensor JSON records (rg)
bf query "product page" --fields ref,title,snippet

# Filter heading and keyed attr_list metadata (jq)
bf query --jq 'select(.anchors.decision.owner == "product") | {ref, title, anchors}'

# Read outline, exact section, line slice, or sensor record
bf read projects/new-website.md --outline
bf read "bf:///projects/new-website.md#decision"
bf read "workspace:brief" --fields ref,record

# Check stale notes, newer evidence, and open tasks
bf review
```

## Connect your coding agent

There is zero global config file. `bf` resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH`. Inside a brain directory, agents orient with `bf status` and `bf review` and open dated workspaces via `bf new action <slug>`; outside a brain directory, set `export DEFAULT_BRAIN_PATH="$HOME/brain"`, copy `skills/bf` to `~/.agents/skills/bf`, and invoke `/bf` explicitly (or configure `bf mcp`).

## Upgrade and pinned runtimes {#upgrade-and-pinned-runtimes}

Upgrade the installed CLI with `uv tool upgrade brain-framework`. When upgrading across a major format version, follow the manual migration steps under `Breaking changes` in the [changelog](https://fmind.github.io/brain-framework/changelog/) and verify with `bf check`.
