# Governance

## Model

The founder maintains Brain Framework and decides. Decisions follow the direction and invariants in [AGENTS.md](AGENTS.md) and happen in public: in issues, pull requests, this file, and the [changelog](CHANGELOG.md).

## Maintainers

- Médéric Hurier ([fmind](https://github.com/fmind)): all areas, including releases and security reports.

## Propose a change

Open a [feature request](https://github.com/fmind/brain-framework/issues/new?template=feature.yml) before building something that enlarges the product. Name the user outcome, the evidence from real use, and any scope decision below that it crosses. Fixes go straight to a [pull request](CONTRIBUTING.md#submit-a-pull-request).

## Scope decisions

Each decision states why it holds and what evidence would reopen it. This list was first recorded on 2026-11-08; a line without its own `Changed` date dates from then. Propose crossing a decision rather than working around it.

- **Three formats, one job each: YAML for config, pure JSON for records, Markdown for humans.** `bf.yaml` (`version: 15`), `evals/*.yaml`, and note frontmatter use YAML 1.2; `bf build` and `bf ingest` emit individual JSON files (`memories/**/*.json`) validated by package-owned core schemas (`bf schema`) and user JSON Schemas (`schemas/*.json`), while `bf run`/`bf watch` keep private loop state, `.bf/index.jsonl`, and `.bf/events.jsonl` in `.bf/` and emit plain stdlib JSON lines to `logs/*.log`; Markdown notes use Python-Markdown (`attr_list`, `extra`, `toc`, `admonition`, `sane_lists`). Why: standard formats and proven libraries replace custom query engines, SQLite caches, and ontology DSLs. Reopen: a core workflow plain Markdown, JSON, and JSON Schema cannot express. See [the overview](docs/index.md). Changed 2026-11-21.
- **Agents query JSON with standard CLI tools (`rg`, `jq`) and `bf://` URLs.** Core BF ships no SQLite index, custom query parser, or embeddings, delegating `bf query`, `bf read`, `bf eval`, and read-only `bf mcp` to `rg` and `jq` (accelerated by a plain `.bf/index.jsonl` metadata cache). Why: standard CLI tools provide fast lexical search and structured JSON filtering across `memories/**/*.json` with compact BF code (`<= 5,500` LOC / ~5,000 target). Reopen: a reproducible retrieval task over `memories/**/*.json` that standard CLI tools cannot serve. See [Retrieval](docs/retrieval.md). Changed 2026-11-21.
- **No model inference, provider SDK, or global config.** `bf.yaml` (`timezone`, `env`, unified `loops:`, `watch` with bounded `concurrency`) orchestrates literal `argv` commands (sensors, routines, git hooks, `daemon: true` listeners, and external thinking bots) while keeping at most 5 copy-in stdlib sensor examples (`examples/sensors/`, including circular `agent-telemetry.py`). Why: provider CLIs own credentials, external harnesses own model calls, and `DEFAULT_BRAIN_PATH` replaces global registries. Reopen: an integration stdlib scripts plus `bf ingest` cannot express. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0). Changed 2026-11-21.
- **Majors ship whenever the design needs them.** Breaking changes bump `FORMAT`, drop legacy runtime branches with no compatibility layer, and list manual upgrade steps under `### Breaking changes` in `CHANGELOG.md`. Why: one current runtime contract stays simple to test, document, and maintain. Reopen: none planned. See [keep the contract](CONTRIBUTING.md#keep-the-contract). Changed 2026-11-21.
- **Links and metadata are explicit, never inferred.** Notes link via `bf://` URLs, Markdown links, and `attr_list` attributes (`{#id key=val}`, `[label](target){rel=...}`), and user schemas in `schemas/` validate record shapes explicitly. Why: inferred entities mix two subjects' evidence without a trace. Reopen: none planned. See [Authoring](docs/authoring.md). Changed 2026-11-15.

## Next major

These planned breaks change a contract, so they wait for the next major release, which has no date yet. Never ship one in a minor or patch release.

None queued yet.

## Record a decision

The pull request that adds or changes a decision updates its line here and ends it with the date, such as `Changed 2026-12-01.` The changelog records the decision when it ships.

## Add a maintainer

The founder invites a contributor with sustained, reviewed work in an area to maintain it. Once there are two maintainers, decisions move to lazy consensus: a proposal passes when no maintainer objects within seven days.
