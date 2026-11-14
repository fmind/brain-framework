<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework: a robot head whose shell opens on a brain, above twin aperture eyes"
    width="256"
    height="256"
  >
</p>

# Brain Framework 🧠

**🧠 Brain Framework: from information to informed actions.**

Brain Framework (BF) is a local-first brain for you and your AI agents. It collects evidence from your tools into plain files you own, connects them through an explicit ontology, lets any agent harness search and read offline, and wakes your harness when triggers or reviewed gates spot new evidence.

- **Plain files you own, open to any agent.** Markdown notes (`projects/`, `concepts/`, `actions/`) and JSON records (`memories/`), reached through the `bf` CLI, [MCP](https://fmind.github.io/brain-framework/docs/mcp/), [Agent Skills](https://agentskills.io/specification) and `AGENTS.md`.
- **Explicit graph and provenance.** `bf.yaml` `map:` rules or scripts map provider fields into shared relations once at collection. Every claim keeps its source record, and `newer_evidence` flags notes when upstream evidence changes.
- **Controlled autonomy.** Scheduled or event-triggered jobs (`jobs:`) with reviewed `gate:` scripts wake your harness only when needed and deliver answers via `bf post` or validated `ACTION.md` drafts.

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

[Try it](https://github.com/fmind/brain-framework#try-it) · [How it works](https://github.com/fmind/brain-framework#how-it-works) · [Agents](https://github.com/fmind/brain-framework#open-your-agent-in-your-brain) · [Your tools](https://github.com/fmind/brain-framework#bring-your-tools) · [Proactive agents](https://github.com/fmind/brain-framework#let-your-agent-come-to-you) · [Boundaries](https://github.com/fmind/brain-framework#good-to-know) · [Documentation](https://fmind.github.io/brain-framework/)

<a href="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-loop.svg">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-loop.svg"
    alt="The Brain Framework loop: jobs gather evidence into a brain folder; bf.yaml declares the ontology, jobs and watch settings; memories, projects, concepts and actions form a knowledge graph; agents read context, act and compile outcomes back."
    width="960"
  >
</a>

## Try it

Requires Linux or macOS and [uv](https://docs.astral.sh/uv/getting-started/installation/) — no model or cloud account needed:

```bash
uv tool install --python 3.14 brain-framework
bf init ~/brain && cd ~/brain
```

Save `projects/new-website.md`:

```markdown
---
type: project
status: draft
updated: 2026-11-01
summary: Launch a product website that helps visitors understand the product.
---

# New website

## Decision

Start with a single product page because visitors need a clear explanation before signing up.

## Next actions

- [ ] Draft the product page.
```

Search and read the exact section:

```bash
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
bf validate && bf eval
```

Continue with [Getting started](https://fmind.github.io/brain-framework/docs/getting-started/) or the [four-tool walkthrough](https://fmind.github.io/brain-framework/docs/context-hub/) across Workspace, Jira, GitHub and Google Cloud fixtures.

## How it works

Five steps run in one loop: **gather → normalize → organize and connect → act → learn.**

| Folder      | Role                                                                                                    |
| ----------- | ------------------------------------------------------------------------------------------------------- |
| `memories/` | Collected JSON records keyed by SHA-256 of `id`, with provenance, `@type`, `relations` and `attributes` |
| `projects/` | Project state (`Now`), decisions (`Decision`) and open tasks (`Next actions`)                           |
| `concepts/` | Durable reusable patterns and reference syntheses                                                       |
| `actions/`  | Tracked work sessions (`draft` open, `stable` settled, `deprecated` retired)                            |
| `sensors/`  | Standalone scripts printing JSON/JSONL or raw exports shaped by `bf.yaml` `map:`                        |
| `routines/` | Deterministic scripts (like `weekly-review.py`) run on intervals, `at`, events or Git hooks             |

`bf.yaml` declares shared [fields and mappings](https://fmind.github.io/brain-framework/docs/schema/#shared-fields-and-sensor-mappings) and unified [`jobs:`](https://fmind.github.io/brain-framework/docs/prompts/) covering [sensors](https://fmind.github.io/brain-framework/docs/sensors/), [routines](https://fmind.github.io/brain-framework/docs/routines/) and [channels](https://fmind.github.io/brain-framework/docs/channels/). A disposable SQLite cache in `.bf/` powers offline BM25 search, graph backlinks (`bf read project:new-website`), triage (`bf read review`) and JSONL exports (`bf export --kind items`). See the [directory reference](https://fmind.github.io/brain-framework/docs/brain/#directory-reference).

## Open your agent in your brain

Install the packaged `/bf` skill for [Claude Code](https://code.claude.com/docs/en/overview), [Codex](https://openai.com/codex/), [GitHub Copilot](https://github.com/features/copilot) or any [Agent Skills](https://agentskills.io/specification) host:

```bash
bf skill install --global         # ~/.agents/skills/bf (and links ~/.claude/skills/bf if present)
bf skill install                  # inside a brain: skills/bf, linked to .agents/skills/bf and .claude/skills/bf
```

The `/bf` router connects your harness to built-in offline documentation via `bf guide`:

- **`bf guide retrieval`**: orient with `bf read review`, verify and cite `SOURCE:ID` refs and export graph claims.
- **`bf guide authoring`**: update owning `projects/` and `concepts/`, manage tracked `actions/` (`bf action new`, `bf action status`) and connect entities.
- **`bf guide getting-started` / `bf guide operations`**: initialize or register brains, run [MCP](https://fmind.github.io/brain-framework/docs/mcp/), validate, evaluate and troubleshoot.
- **`bf guide jobs` / `bf guide events`**: configure `jobs:`, `map:`, `bf watch` triggers, recover queues and use Git hooks.

See [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) and optional repository [hooks](https://github.com/fmind/brain-framework/tree/main/examples/hooks).

## Bring your tools

Copy a standalone stdlib script into `sensors/` (or pair any JSON/JSONL CLI with `bf.yaml` `map:`): [local documents](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py), [highlights](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py), [Git history](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py), [GitHub history](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md), [Google Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py), [Gmail headers](https://github.com/fmind/brain-framework/blob/main/examples/sensors/gmail-headers.py), [Drive folders](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py), or [any source you can script](https://fmind.github.io/brain-framework/docs/sensors/#any-source-you-can-script). Keep them fresh in a terminal or [background service](https://fmind.github.io/brain-framework/docs/schedule/) with `bf watch` ([offline demo](https://github.com/fmind/brain-framework/tree/main/examples/watch)):

<a href="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/watch.svg">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/watch.svg"
    alt="Watch dashboard for the fictional offline demo: five sensors sorted by state, with a failed sensor's details beside their last success, next due time, item count and record changes."
    width="960"
  >
</a>

## Let your agent come to you

A [prompt job](https://fmind.github.io/brain-framework/docs/prompts/) watches your brain offline and invokes your harness only when an event or `gate:` triggers it:

```yaml
jobs:
  triage:
    prompt: prompts/triage.md
    harness:
      [
        claude,
        --print,
        --output-format,
        json,
        --tools,
        "Read,Grep,Glob,Bash",
        --allowedTools,
        "Bash(bf read *),Bash(bf search *)",
        --permission-mode,
        dontAsk,
      ]
    extract: /result
    emit: message
    post: desktop
    on:
      - { every: 600, event: { kind: record, source: mail } }
```

Event rules and `gate:` scripts skip quiet intervals without starting a model, `extract:` unwraps harness JSON, and `post:` delivers answers through your [channel](https://fmind.github.io/brain-framework/docs/channels/) (while `emit: action` writes validated `draft` `ACTION.md` notes). See [safe prompts](https://fmind.github.io/brain-framework/docs/prompts/#keep-prompts-safe) and [propose, then act](https://fmind.github.io/brain-framework/docs/prompts/#propose-then-act).

## Good to know

1. **Local-first and private.** No BF server, account or telemetry; retrieval is 100% offline ([privacy and security](https://fmind.github.io/brain-framework/docs/privacy/)).
1. **Harnesses reason; BF verifies.** BF runs no LLM and ships no provider SDKs or embeddings (`bf export --kind items` feeds external retrievers; [what BF does not do](https://fmind.github.io/brain-framework/docs/concepts/#what-bf-does-not-do)).
1. **Git-native collaboration.** Version notes, ontology, programs and evals in Git while keeping local memories and `.bf/` caches out of shared history; major releases batch breaking changes cleanly ([versioning](https://fmind.github.io/brain-framework/docs/upgrades/#versioning-and-stability)).

[Commands](https://fmind.github.io/brain-framework/docs/commands/) · [Issues](https://github.com/fmind/brain-framework/issues) · [Contribute](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) · [Governance](https://github.com/fmind/brain-framework/blob/main/GOVERNANCE.md) · [MIT License](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
