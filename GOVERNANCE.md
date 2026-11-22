# Governance

## Model

The founder maintains Brain Framework and decides. Decisions follow the direction and invariants in [AGENTS.md](AGENTS.md) and happen in public: in issues, pull requests, this file, and the [changelog](CHANGELOG.md).

## Maintainers

- Médéric Hurier ([fmind](https://github.com/fmind)): all areas, including releases and security reports.

## Propose a change

Open a [feature request](https://github.com/fmind/brain-framework/issues/new?template=feature.yml) before building something that enlarges the product. Name the user outcome, the evidence from real use, and any scope decision below that it crosses. Fixes go straight to a [pull request](CONTRIBUTING.md#submit-a-pull-request).

## Scope decisions

Each decision states why it holds and what evidence would reopen it. This list was first recorded on 2026-11-08; a line without its own `Changed` date dates from then. Propose crossing a decision rather than working around it.

- **Three formats, one job each + 3x3 convention: YAML for config, Markdown for humans, JSONL for records.** `bf.yaml` (`version: 18` with `notes:`, `concurrency:`, `env:`, `defaults: {timeout, window}`, and `loops:` using `when:`) and note frontmatter (`created`, `updated`, `description`) use YAML 1.2; authored Markdown notes live in `concepts/`, `projects/`, `actions/` (flat `<slug>.md` + `<slug>/index.md` bundles with semantic slugs); loops live in `sensors/`, `routines/`, `agents/` (`.sh`/`.py`/executable, `stdout` JSONL data/events, `stderr` structured JSONL logs); records live in `memories/<source>.jsonl`, `usages/<source>.jsonl`, and private `logs/<loop>.jsonl` + `logs/events.jsonl` validated unconditionally against bundled `record` plus required `schemas/<source>.json` / `schemas/usages.json`, while `.bf/` holds only disposable `0700` locks and per-loop state (no SQLite, no validation cache). Why: standard UNIX-inspectable JSONL and a predictable 3x3 layout keep the core minimal while allowing `rg`, `jq`, and DuckDB natively. Reopen: a core workflow plain Markdown, JSONL, and JSON Schema cannot express. See [the overview](docs/index.md). Changed 2026-11-22.
- **Agents query JSONL via CLI and standard UNIX tools (`rg`, `jq`, DuckDB), with no MCP server or TUI.** Core BF ships no custom query parser, SQLite index, YAML eval DSL, embeddings, MCP server, or Rich TUI, keeping `bf status` (including `--format text`), `bf review`, `bf read`, `bf query`, `bf check`, and `bf schema` strictly read-only with 5 minimal runtime dependencies while `evals/` holds user-owned Python/pytest scripts. Why: coding harnesses invoke CLIs and UNIX tools directly with lower overhead and fewer dependencies (`<= 6,500` LOC). Reopen: a reproducible retrieval task over `memories/*.jsonl` and `usages/*.jsonl` that standard CLI tools cannot serve. See [Retrieval](docs/retrieval.md). Changed 2026-11-22.
- **No model inference, provider SDK, templating DSL, or global config.** `bf.yaml` orchestrates literal `argv` commands (with a fixed 7-token allowlist `{{brain}}`/`{{loop}}`/`{{state}}`/`{{start}}`/`{{end}}`/`{{trigger}}`/`{{run}}`, never a shell or general template DSL) and `sensors/`/`routines/`/`agents/` scripts via `BF_*` env + `stdin` JSON context, keeping at most 5 copy-in stdlib sensor examples (`examples/sensors/`), reserving `clients/` and `prompts/` as convention-only folders, and resolving brains via `--brain` -> enclosing `bf.yaml` -> `BF_BRAIN` -> `$BRAIN_LOCATION` -> `~/brain`. Why: existing CLIs own credentials, external harnesses own model calls, and strong conventions replace template DSLs and global registries. Reopen: an integration stdlib/shell scripts plus `bf ingest` cannot express. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0). Changed 2026-11-22.
- **Majors ship whenever the design needs them.** Breaking changes bump `FORMAT`, drop legacy runtime branches with no compatibility layer, and list manual upgrade steps under `### Breaking changes` in `CHANGELOG.md`. Why: one current runtime contract stays simple to test, document, and maintain. Reopen: none planned. See [keep the contract](CONTRIBUTING.md#keep-the-contract). Changed 2026-11-21.
- **Links and metadata are explicit, never inferred.** Notes link via universal refs (`concepts/foo.md`, `source:id`, declared `bf://<brain>/<ref>`), Markdown links, `?rel=...` link query parameters, and `attr_list` attributes (`{#id key=val}`, `[label](target){rel=...}`), and required user schemas in `schemas/` validate every note, memory, and usage source explicitly. Why: inferred entities mix two subjects' evidence without a trace. Reopen: none planned. See [Authoring](docs/authoring.md). Changed 2026-11-22.

## Next major

These planned breaks change a contract, so they wait for the next major release, which has no date yet. Never ship one in a minor or patch release.

None queued yet.

## Record a decision

The pull request that adds or changes a decision updates its line here and ends it with the date, such as `Changed 2026-12-01.` The changelog records the decision when it ships.

## Add a maintainer

The founder invites a contributor with sustained, reviewed work in an area to maintain it. Once there are two maintainers, decisions move to lazy consensus: a proposal passes when no maintainer objects within seven days.
