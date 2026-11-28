# Governance

## Model

The founder maintains Brain Framework and decides. Decisions follow the direction and invariants in [AGENTS.md](AGENTS.md) and happen in public: in issues, pull requests, this file, and the [changelog](CHANGELOG.md).

## Maintainers

- Médéric Hurier ([fmind](https://github.com/fmind)): all areas, including releases and security reports.

## Propose a change

Open a [feature request](https://github.com/fmind/brain-framework/issues/new?template=feature.yml) before building something that enlarges the product. Name the user outcome, the evidence from real use, and any scope decision below that it crosses. Fixes go straight to a [pull request](CONTRIBUTING.md#submit-a-pull-request).

## Scope decisions

Each decision states why it holds and what evidence would reopen it. This list was first recorded on 2026-11-08; a line without its own `Changed` date dates from then. Propose crossing a decision rather than working around it.

- **Three formats, one job each + 3x3 convention: YAML for config, Markdown for humans, canonical JSONL for records + lean offset-indexed SQLite FTS5 cache.** `bf.yaml` (`version: 19` with `notes:`, `brains:`, `search.exclude:`, `concurrency:`, `env:`, and `loops:` using flat/windowed `when:`, `window:`, explicit `ingest:`/`timeout:`, and per-loop `env_from:`) and note frontmatter (`created`, `updated`, `description`) use YAML 1.2; authored CommonMark Markdown notes (`markdown-it-py`) live in `concepts/`, `projects/`, `actions/` (flat `<slug>.md` + `<slug>/index.md` bundles with semantic slugs) and compile incrementally into `memories/_notes.jsonl`; loops live in `sensors/`, `routines/`, `agents/` (`.sh`/`.py`/executable, `stdout` JSONL data/events, `stderr` structured JSONL logs); records live in unified `memories/<source>.jsonl` and private `logs/<loop>.jsonl`, validated unconditionally by fast native core `record` validation plus optional `schemas/<source>.json` (or explicit `ingest.schema:`), while `.bf/` cleanly separates strictly disposable `.bf/cache/index.sqlite3` (contentless offset-indexed FTS5 hydrated via `os.pread`), `.bf/state/` (`<loop>.json`, `<loop>/`, `events.jsonl`), and `.bf/locks/`. Why: canonical UNIX-inspectable JSONL keeps `rg`, `jq`, and DuckDB first-class while the offset-indexed SQLite FTS5 cache delivers instant ranked BM25 search, alias lookups, and backlink traversal without duplicating JSONL bodies on disk. Reopen: a core workflow plain Markdown, JSONL, SQLite FTS5, and JSON Schema cannot express. See [the overview](docs/index.md). Changed 2026-11-24.
- **Strictly read-only retrieval via stdlib SQLite FTS5 + JSONL fallback, `bf watch` on stdlib polling, and zero raw-TTY TUI.** Core BF ships zero built-in LLMs, embeddings, MCP server, external `rg`/`jq` runtime dependency, native `watchfiles` wheel, or raw-TTY interactive TUI (`termios`/keybinding/mouse loops), keeping `bf status` (exits `0`; `--check` exits `1`; `--watch [SECONDS]` live-refreshes a failure-first ANSI table), `bf review`, `bf read`, and `bf query` (including `--links-to` and `--all-sources`) strictly read-only over `.bf/cache/index.sqlite3` (`mode=ro` with per-source JSONL fallback), alongside read-only `bf events`, `bf check`, and `bf schema`, and `bf watch` (`live` failure-first ANSI table on TTY, `jsonl` when piped/daemonized, `--format text` scrolling log) on just 3 minimal runtime dependencies (`jsonschema`, `markdown-it-py`, `pyyaml`, Python 3.12+) while `evals/` holds user-owned Python/pytest scripts. Practical flat-JSONL rewrite sweet spot is `<= 20,000` records (`<= 64 MiB`) per source (soft warning in `bf check` / `bf status`; hard cap `256 MiB` / `1M` total records); partition monotonically growing sources by year sharing one `ingest.schema:`. Why: coding harnesses invoke CLIs directly with low latency, ranked note-boosted BM25 results, and minimal runtime dependencies, while a plain ANSI status screen surfaces failing and running loops at a glance without brittle raw-TTY input handling. Reopen: a reproducible retrieval task over `memories/*.jsonl` that SQLite FTS5 and standard CLI tools cannot serve. See [Retrieval](docs/retrieval.md). Changed 2026-11-27.
- **Zero-magic loops, per-loop `env_from:`, no model inference, provider SDK, templating DSL, or global config.** `bf.yaml` orchestrates literal `argv` commands (with a fixed 7-token allowlist `{{brain}}`/`{{loop}}`/`{{state}}`/`{{start}}`/`{{end}}`/`{{trigger}}`/`{{run}}`, never a shell or general template DSL) and `sensors/`/`routines/`/`agents/` scripts via `BF_*` env, per-loop `env_from:` host/`.env` allowlists (zero top-level secret fan-out; `.env` overrides tracked `bf.yaml`), and matched events JSONL on `stdin`. Folder conventions locate scripts only when `run:` is omitted (never silently enabling `ingest:` or changing `timeout:`). Keep at most 5 copy-in stdlib sensor examples (`examples/sensors/`), reserve `clients/` and `prompts/` in `RESERVED_SOURCES`, and resolve brains via `--brain` -> `BF_BRAIN` -> enclosing `bf.yaml` -> `~/brain`. Why: per-loop allowlists prevent cross-loop credential leakage, existing CLIs own auth, external harnesses own model calls, and explicit contracts eliminate hidden magic and global registries. Reopen: an integration stdlib/shell scripts plus `bf ingest` cannot express. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0). Changed 2026-11-24.
- **Majors ship deliberately after live dogfooding.** Breaking changes bump `FORMAT`, drop legacy runtime branches with no compatibility layer, and list manual upgrade steps under `### Breaking changes` in `CHANGELOG.md`. Accumulate breaking contract proposals under `Next major` below for at least two weeks of live dogfooding before cutting a new major release so ergonomics stabilize instead of flip-flopping across consecutive days. Why: one current runtime contract stays simple to test, document, and maintain without imposing daily migration churn. Reopen: critical security contract break. See [keep the contract](CONTRIBUTING.md#keep-the-contract). Changed 2026-11-23.
- **Links and metadata are explicit, never inferred.** Notes link via universal refs (`concepts/foo.md`, `source:id`, declared `bf://<brain>/<ref>`), Markdown links, `?rel=...` link query parameters, and `attr_list` attributes (`{#id key=val}`, `[label](target){rel=...}`), and optional domain schemas in `schemas/` tighten validation wherever domain fields matter without empty boilerplate stubs. Why: inferred entities mix two subjects' evidence without a trace. Reopen: none planned. See [Authoring](docs/authoring.md). Changed 2026-11-24.

## Next major

These planned breaks change a contract, so they wait for the next major release, which has no date yet. Never ship one in a minor or patch release.

None queued yet.

## Record a decision

The pull request that adds or changes a decision updates its line here and ends it with the date, such as `Changed 2026-12-01.` The changelog records the decision when it ships.

## Add a maintainer

The founder invites a contributor with sustained, reviewed work in an area to maintain it. Once there are two maintainers, decisions move to lazy consensus: a proposal passes when no maintainer objects within seven days.
