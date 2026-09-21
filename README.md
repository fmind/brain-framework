# FKF

**Owned evidence. Small context.**

FKF is an opinionated, file-based framework for a personal or team second brain. It keeps useful work history in JSON and Markdown and gives an agent a small, reproducible context pack with exact references. Python, one command, offline reads, and a disposable SQLite index. Provider commands own their credentials. FKF requires Python 3.14 or newer on Linux or macOS.

Use FKF to resume a project, recover why a decision was made, or give an agent cited context from selected work history. It works best with concise project notes and sources that answer recurring questions; it does not reason over your data or synchronize every service. Keep authored knowledge in Markdown and selected source evidence in normalized JSON.

## Start locally

```bash
uv tool install --python 3.14 'fkf==7.0.0'
fkf init ~/knowledge
fkf build --base ~/knowledge
fkf context "project decisions" --base ~/knowledge --budget 850
fkf read wiki/welcome.md --base ~/knowledge
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first; it supplies Python 3.14 if needed. Add the tool directory to your PATH with `uv tool update-shell` if `fkf` is not found, then open a new shell. Write decisions in `projects/` or `wiki/` and run `fkf build` after edits. From a development checkout, use `uv sync --locked` and `uv run fkf` instead.

**Upgrading from v6:** v7 is a breaking Python rewrite with a new base format and command surface. Preserve the old base and executable; create a separate v7 base. There is no in-place migration or compatibility command. See the [v7 release notes](CHANGELOG.md).

## The core

| Operation                    | Purpose                                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------------------ |
| `update [--dry-run]`         | Collect due sources using successful automatic captures as checkpoints, then build if stale.     |
| `collect SOURCE START END`   | Run one reviewed adapter over an explicit timezone-aware window and store an immutable capture.  |
| `build [--if-stale]`         | Rebuild the disposable SQLite index, optionally only when stale.                                 |
| `find QUERY`                 | Find local evidence and explicit identities.                                                     |
| `context QUERY`              | Assemble a bounded context pack.                                                                 |
| `read URI`                   | Read exact durable evidence, without fetching anything.                                          |
| `validate`, `status`, `eval` | Check content, index state and per-source capture freshness, and owner-authored retrieval cases. |
| `mcp --base PATH`            | Expose only find, context and read to an agent over stdio.                                       |

A base has `fkf.yaml`, authored `projects/`, `tasks/` and `wiki/`, and immutable `records/`. `.fkf/` contains disposable index artifacts. Sources are optional, base-owned commands that emit normalized records. There is no provider SDK, background agent, telemetry, model or embedding in retrieval. Ordinary search uses the latest known capture per source record; `find` and `context --history` can recover older observations with explicit snapshot labels. Latest capture does not imply a still-valid decision.

## Guarantees

- Ordinary reads execute no command and make no network request.
- Collected content is untrusted evidence and never becomes executable input.
- Collection is explicit: `collect` runs one configured adapter; `update` runs due configured adapters. Review source commands before running them; adapters execute with your user permissions.
- Collection uses direct argv from `/`, sanitized process startup inputs, bounded output and process-group cancellation. Failures write no partial capture.
- Durable data is independent of SQLite. A ready index is opened read-only in place; a missing, stale or corrupt index stops indexed retrieval with an instruction to run `fkf build`. Direct Markdown and capture-file reads remain available for recovery.
- Context budgets include the complete compact JSON response, including notice and diagnostics: four UTF-8 bytes per budget unit. MCP also counts its complete text/structured tool-result wrapper. This is a byte allowance, not a model-specific token count.

Scheduling, provider adapters, private skill installation, and ordinary knowledge editing belong to base maintenance and agent skills. The core does not install or supervise them.

## Agent skills

The [skills catalog](skills/README.md) provides `fkf-use` for retrieval, `fkf-learn` for sourced knowledge, and `fkf-maintain` for base upkeep. Copy required packages into the base’s canonical `skills/` directory and expose reviewed packages through project-local `.agents/skills/`, using the host’s supported links or copies. A host-wide installation is a separate choice. These Markdown resources are maintained here separately from the Python distribution. Repository development uses [.agents/skills/fkf-contribute](.agents/skills/fkf-contribute/SKILL.md).

## Source examples

The [three source examples](examples/sources/README.md) demonstrate local Git history, windowed Google Calendar events, and complete Drive folder snapshots. Copy a useful example into your base's `sources/`, adapt and test it there, and declare it in `fkf.yaml`. Other integrations and their tests belong to the bases that use them. The Python distribution neither installs nor synchronizes collectors.

The [runnable example base](examples/base/README.md) includes fictional evidence, an OKF concept, a project, a resumable task, and retrieval acceptance cases. Its fake collector needs no provider or credentials; use it to learn the complete workflow.

## Development

```bash
mise run all
mise run benchmark
```

This framework repository keeps its Python package in `src/fkf/`, documentation in `docs/`, starter material in `examples/`, and framework tests in `tests/`. A user base has its own `tests/` for its own collectors and scripts.

The gate formats, checks types and security, runs hermetic tests with an 85% branch-coverage floor, builds the documentation, and installs both distributions in isolated smoke environments. See [AGENTS.md](AGENTS.md), [contributing](CONTRIBUTING.md), and the [documentation](docs/docs/index.md).

MIT. Runtime dependency licenses are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Daily learning and separate bases

Agents should actively recommend useful project/wiki updates or repository-local skillification after substantial work. The learning skill keeps routine verified edits within existing authority and proposes changes to accepted decisions or new skills. Tasks use `tasks/<task>/TASK.md` with `inputs/` and `outputs/`.

Each base has a persistent id and independent configuration, evidence, index and recovery. Replies identify their base; exact qualified references fail in a different base. `find` and `context` support knowledge type/status and explicit folder/label membership filters. Current decisions and H2+ passages are searchable independently of retained history. See [base layout and contracts](docs/docs/base.md).

## Base conventions

Keep one private base per person or team. Work from that directory; expose it through project-local harness configuration or an explicit CLI base. A directory is a context boundary, not an operating-system sandbox: a team with different access rights needs separate bases and appropriate filesystem permissions.

| Directory                | Purpose                                                                                                 |
| ------------------------ | ------------------------------------------------------------------------------------------------------- |
| `wiki/`                  | Reusable knowledge in OKF v0.2 Markdown and a small `index.md`.                                         |
| `projects/`              | Current project context, decisions and TODOs, with links to canonical repositories.                     |
| `tasks/YYYY-MM-DD_slug/` | Resumable delegated work: `TASK.md`, `inputs/`, `outputs/`.                                             |
| `records/`               | Immutable normalized captures.                                                                          |
| `configs/`               | Maintained feed lists, channel catalogs and script configuration; pass paths explicitly in source argv. |
| `inputs/`                | Optional original imports; retain them when they cannot be recreated.                                   |
| `sources/`               | Collectors, preferably standalone Python; executable Bash or other languages work too.                  |
| `scripts/`               | Base operations and maintenance.                                                                        |
| `tests/`                 | Collector and maintenance tests with synthetic fixtures and fake providers; include in recovery.        |
| `skills/`                | Team workflows; discover selected skills through the host's project-local `.agents/skills/`.            |
| `indexes/`, `.fkf/`      | Generated navigation and SQLite; safe to rebuild.                                                       |
| `logs/`                  | Optional operational receipts; exclude payloads and credentials, bound retention.                       |

`fkf init` creates the main folders, including `tests/`; root `inputs/` and `logs/` are optional and created when needed. Only Markdown under `projects/`, `wiki/` and `tasks/`, plus captures under `records/`, enters retrieval. Tests, scripts, configuration and skills are not indexed.

`fkf.yaml` owns identity, source argv and refresh policy. `configs/` holds adapter-owned data, not another FKF configuration layer. Start with native scheduler logs and compact JSON receipts; no telemetry service is required. See [sources](docs/docs/sources.md) for refresh semantics and [base layout](docs/docs/base.md) for the knowledge contract.
