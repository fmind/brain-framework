<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework: Plain-file knowledge and offline retrieval for humans and AI agents"
    width="100%"
  />
</p>

Brain Framework (`bf`) turns a 3x3 folder of human-authored Markdown notes, shell/Python loops, and JSONL records (`memories/<source>.jsonl`, `usages/<source>.jsonl`, `logs/<loop>.jsonl`) into a schema-validated, UNIX-native knowledge brain with zero database or external indexer, ready for humans and AI coding harnesses to query offline with `rg`, `jq`, and DuckDB.

- **Three formats, one job each.** **YAML for config** (`bf.yaml` `version: 18` + note frontmatter), **Markdown for humans** (`concepts/`, `projects/`, `actions/` with flat `<slug>.md` and universal `<slug>/index.md` bundles), and **JSONL for records** (`memories/`, `usages/`, and private `logs/`, validated by bundled `bf`/`record` schemas plus `schemas/<source>.json` and `schemas/usages.json`).
- **Strict read/write boundary & UNIX composability.** `bf status`, `bf review`, `bf read`, `bf query`, `bf check`, and `bf schema` are strictly read-only and never mutate disk; flat JSONL files stay directly inspectable with `rg`, `jq`, and DuckDB.
- **Convention-first `loops:` with zero built-in LLMs.** Under 6,500 lines of Python with just 5 minimal runtime dependencies (`croniter`, `jsonschema`, `markdown`, `pyyaml`, `watchfiles`), unifying deterministic collectors (`sensors/`), routines (`routines/`), coding-harness agents (`agents/`), and daemons (`when: daemon`) via `BF_*` env + `stdin` JSON, `stdout` JSONL data/events, `stderr` structured JSONL logs, and `logs/events.jsonl`.

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

## Try it

Requires Linux or macOS, [uv](https://docs.astral.sh/uv/getting-started/installation/), [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`), and [jq](https://jqlang.org/), with no model or cloud account needed:

```bash
uv tool install --python 3.14 brain-framework
bf init ~/brain
cd ~/brain
bf check
bf status --format text
bf review
bf query --jq '{ref, title, headings}'
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

- **Uniform notes, `index.md` bundles, & required schemas ([Authoring](https://fmind.github.io/brain-framework/authoring/)):** Organize Markdown across `concepts/`, `projects/`, `actions/` (or custom `notes:` dirs) using flat `<slug>.md` files or universal `<slug>/index.md` bundles, enforce bundled `record` + `schemas/<source>.json` / `schemas/usages.json`, attach `#id` heading anchors and `{rel=...}` link metadata, and link across notes, records (`source:id`), or declared sibling brains (`bf://`).
- **Convention `loops:` (`sensors/`, `routines/`, `agents/`), two-stream JSONL, & `bf watch` ([Operations](https://fmind.github.io/brain-framework/operations/)):** Drop shell (`.sh`), Python (`.py`), or executable scripts in `sensors/` (auto-implies `ingest: upsert`), `routines/`, or `agents/` (15m default timeout), stream structured JSONL logs on `stderr` into `logs/<loop>.jsonl`, coordinate over `logs/events.jsonl`, and supervise with `bf watch` (bird's-eye status + live text or `--format jsonl`).
- **Strictly read-only retrieval & FinOps separation ([Retrieval](https://fmind.github.io/brain-framework/retrieval/)):** Resolve notes, `#anchor` sections, `source:id` records, or `bf://` URIs uniformly with `bf read`, search `memories/*.jsonl` or `usages/*.jsonl` via `bf query` (`rg`/`jq`) or DuckDB, and inspect live health with `bf status --format text`.
- **Integrity, freshness, & schemas ([Reference](https://fmind.github.io/brain-framework/reference/)):** `bf check` validates `bf.yaml`, schemas, compiled-note sync, `memories/` & `usages/`, enabled `loops:`, links, `#anchor` headings, and declared cross-brain `bf://` refs; `bf review` surfaces `newer_evidence`, overdue `stale_after` notes, and open tasks; `bf schema` prints the 2 bundled Draft 2020-12 JSON Schemas (`bf`, `record`).

## Good to know

1. **Local-first and private.** Zero global config (`--brain` -> enclosing `bf.yaml` -> `BF_BRAIN` -> `$BRAIN_LOCATION` -> `~/brain`), `O_NOFOLLOW` path confinement, disposable `0700` `.bf/` (locks + loop cursors), and private `0700` `logs/` ([Operations](https://fmind.github.io/brain-framework/operations/)).
2. **Harnesses reason; BF verifies.** Coding agents orient with `bf status` and `bf review`, author Markdown notes directly, emit events via `bf emit`, and verify with `bf build && bf check` plus Python/pytest suites in `evals/` ([Example brain](https://github.com/fmind/brain-framework/blob/main/examples/brain/README.md)).
3. **Git-native collaboration.** Track notes, `bf.yaml`, `schemas/`, `sensors/`, `routines/`, `agents/`, `clients/`, `prompts/`, and `evals/` in Git while ignoring `memories/` and `usages/` (keeping `*.forgotten`), `logs/`, and `.bf/`.

[Documentation](https://fmind.github.io/brain-framework/) · [Reference](https://fmind.github.io/brain-framework/reference/) · [Issues](https://github.com/fmind/brain-framework/issues) · [Contribute](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) · [Governance](https://github.com/fmind/brain-framework/blob/main/GOVERNANCE.md) · [Security](https://github.com/fmind/brain-framework/blob/main/SECURITY.md) · [MIT License](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
