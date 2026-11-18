<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework — Plain-file knowledge and offline retrieval for humans and AI agents"
    width="100%"
  />
</p>

Brain Framework (`bf`) turns a flat folder of human-authored Markdown notes and collected JSON records into a schema-validated, pure-JSON knowledge store (`memories/**/*.json`) that humans and AI agents query offline with `rg` and `jq` (`bf query`, `bf read`, or `bf mcp`).

- **Three formats, one job each.** **YAML for config** (`bf.yaml`, `evals/*.yaml`, note frontmatter), **JSON for records** (`memories/**/*.json`, enforced by `schemas/*.json`, plus `structlog` JSON lines in `logs/*.log`), and **Markdown for humans** (`projects/`, `concepts/`, and `actions/` parsed with [Python-Markdown](https://python-markdown.github.io/) `attr_list` metadata, plus plain Markdown inputs under `prompts/`).
- **Agents always query JSON.** `bf build` compiles Markdown notes, headings, keyed `attr_list` metadata (`attrs`), links, and open tasks into `memories/{concepts,projects,actions}/*.json`; `bf ingest` writes validated sensor records into `memories/<source>/*.json`.
- **Minimal, standard-driven core.** Under 4,500 lines of Python delegating retrieval to `rg` and `jq`, cross-references to relative Markdown paths and standard `bf://` URLs, and orchestration (`timezone`, `env`, `jobs`, `services`, `watch`) to `bf.yaml` — with zero custom databases, built-in LLMs, or provider SDKs.

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

## Try it

Requires Linux or macOS, [uv](https://docs.astral.sh/uv/getting-started/installation/), [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`), and [jq](https://jqlang.org/) — no model or cloud account needed:

```bash
uv tool install --python 3.14 brain-framework
bf init ~/brain
cd ~/brain
bf validate
bf eval
bf review
bf query --jq '{ref, title, attrs}'
```

Explore the [four-tool walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/context-hub/README.md) (Workspace, Jira, GitHub, and Google Cloud fixtures) or read [Getting started](https://fmind.github.io/brain-framework/getting-started/).

## How it works

<a href="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-loop.svg">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-loop.svg"
    alt="Gather, normalize, organize, act, and learn loop"
    width="100%"
  />
</a>

- **Notes, `attr_list`, & `bf://` URLs ([Authoring](https://fmind.github.io/brain-framework/authoring/)):** Separate volatile TODOs (`projects/`), durable knowledge (`concepts/`), and dated workspaces (`actions/*/ACTION.md`). Attach metadata on headings (`## MCP {#mcp creator=Anthropic}`) and blocks (`{: #summary confidence=high}`), and cross-reference notes or sibling brains with `bf://` URLs.
- **User schemas, jobs, & live watch ([Operations](https://fmind.github.io/brain-framework/operations/)):** Ship user-owned JSON Schemas in `schemas/`, copy stdlib sensors from [examples/sensors](https://github.com/fmind/brain-framework/tree/main/examples/sensors), and orchestrate cron/duration schedules, `structlog` JSON logs, event triggers, services, and Rich TUI monitoring via `bf.yaml` (`jobs`, `services`, `watch`).
- **Standard JSON retrieval & MCP ([Retrieval](https://fmind.github.io/brain-framework/retrieval/)):** Resolve `bf://` URLs and `#anchor` sections with `bf read`, search and filter `memories/**/*.json` via `bf query` (`rg`, `jq`), run deterministic `bf eval` checks, or expose read-only tools via `bf mcp`.
- **Integrity & freshness ([Reference](https://fmind.github.io/brain-framework/reference/)):** `bf validate` checks every schema, compiled-note sync, link, `#anchor`, and cross-brain `bf://` reference; `bf review` surfaces `newer_evidence`, overdue `stale_after` notes, and open `draft` actions.

## Good to know

1. **Local-first and private.** Zero global config (`--brain` -> `BF_BRAIN` -> enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH`), `O_NOFOLLOW` path confinement, and private `0700` `.bf/` locks/state ([Operations](https://fmind.github.io/brain-framework/operations/)).
2. **Harnesses reason; BF verifies.** Coding agents load the single `/bf` [Agent Skill](https://agentskills.io/specification) from `.agents/skills/bf`, automatically track actions inside a brain (or respond to explicit `/bf` outside), and run `bf build && bf validate && bf eval` ([Example brain](https://github.com/fmind/brain-framework/blob/main/examples/brain/README.md)).
3. **Git-native collaboration.** Track notes, `bf.yaml`, `evals/`, and `schemas/` in Git while ignoring compiled note JSON files, `logs/`, `actions/`, and `.bf/`.

[Documentation](https://fmind.github.io/brain-framework/) · [Reference](https://fmind.github.io/brain-framework/reference/) · [Issues](https://github.com/fmind/brain-framework/issues) · [Contribute](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) · [Governance](https://github.com/fmind/brain-framework/blob/main/GOVERNANCE.md) · [MIT License](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
