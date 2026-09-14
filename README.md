# FKF

**Owned evidence. Small context.**

FKF keeps useful work history in JSON and Markdown and gives an agent a small, reproducible context pack with exact references. Python, one command, offline reads, and a disposable SQLite index. Provider commands own their credentials. FKF requires Python 3.14 or newer on Linux or macOS.

FKF has one current configuration and evidence format. Keep authored knowledge in Markdown and selected source evidence in normalized JSON.

## Start locally

```bash
uv sync --locked
uv run fkf init ~/knowledge
# Write your project decisions in ~/knowledge/projects/.
uv run fkf build --base ~/knowledge
uv run fkf context "project constraints" --base ~/knowledge --budget 850
uv run fkf read wiki/welcome.md --base ~/knowledge
```

This is an unreleased v7 checkout. Use `uv run fkf` or install a locally built wheel; publication is a separate step.

## The core

| Operation                    | Purpose                                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------------------ |
| `collect SOURCE START END`   | Run one reviewed adapter over an explicit timezone-aware window and store an immutable capture.  |
| `build`                      | Rebuild the disposable SQLite index.                                                             |
| `find QUERY`                 | Find local evidence and explicit identities.                                                     |
| `context QUERY`              | Assemble a bounded context pack.                                                                 |
| `read URI`                   | Read exact durable evidence, without fetching anything.                                          |
| `validate`, `status`, `eval` | Check content, index state and per-source capture freshness, and owner-authored retrieval cases. |
| `mcp --base PATH`            | Expose only find, context and read to an agent over stdio.                                       |

A base has `fkf.yaml`, authored `projects/`, `tasks/` and `wiki/`, and immutable `records/`. `.fkf/` contains disposable index artifacts. Sources are optional, base-owned commands that emit normalized records. There is no provider SDK, background agent, telemetry, model or embedding in retrieval. Ordinary search uses the latest known capture per source record; `find` and `context --history` can recover older observations with explicit snapshot labels. Latest capture does not imply a still-valid decision.

## Guarantees

- Ordinary reads execute no command and make no network request.
- Collected content is untrusted evidence and never becomes executable input.
- Collection is explicit: `collect` runs the currently configured adapter. Review source commands before running them; adapters execute with your user permissions.
- Collection uses direct argv from `/`, sanitized process startup inputs, bounded output and process-group cancellation. Failures write no partial capture.
- Durable data is independent of SQLite. A ready index is opened read-only in place; a missing, stale or corrupt index falls back to the same retrieval algorithm in memory and names that path in its response.
- Context budgets include the complete compact JSON response, including notice and diagnostics: four UTF-8 bytes per budget unit. MCP also counts its complete text/structured tool-result wrapper. This is a byte allowance, not a model-specific token count.

Scheduling, provider adapters, private skill installation, and ordinary knowledge editing belong to base maintenance and agent skills. The core does not install or supervise them.

## Agent skills

The [skills catalog](skills/README.md) provides `fkf-use` for retrieval and `fkf-learn` for maintaining sourced knowledge. Copy the required skill directory into your base or agent host’s `.agents/skills/` directory and follow the host’s native discovery rules. These Markdown resources are maintained here separately from the Python distribution. Repository development uses [.agents/skills/fkf-contribute](.agents/skills/fkf-contribute/SKILL.md).

## Adapters

The [adapter catalog](adapters/README.md) provides reviewed standalone Python collectors for Git history, Google Calendar, Gmail, Tasks, Contacts, Drive meeting notes, Chat, GitHub issues, pull requests and notifications, and coding-agent sessions, task folders, Chrome bookmarks, Drive folders and Gmail labels. Copy the ones you need into your base's `sources/`, declare them in `fkf.yaml`, and review them like any code you run. They are maintained here separately from the Python distribution and tested with fake providers.

## Development

```bash
mise run all
mise run benchmark
```

The gate formats, checks types and security, runs hermetic tests with an 85% branch-coverage floor, builds the documentation, and installs both distributions in isolated smoke environments. See [AGENTS.md](AGENTS.md), [contributing](CONTRIBUTING.md), and the [documentation](docs/docs/index.md).

MIT. Runtime dependency licenses are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Daily learning and separate bases

Agents should actively recommend useful project/wiki updates or repository-local skillification after substantial work. The learning skill keeps routine verified edits within existing authority and proposes changes to accepted decisions or new skills. Tasks use `tasks/<task>/TASK.md` with `inputs/` and `outputs/`.

Each base has a persistent id and independent configuration, evidence, index and recovery. Replies identify their base; exact qualified references fail in a different base. `find` and `context` support knowledge type/status and explicit folder/label membership filters. Current decisions and H2+ passages are searchable independently of retained history. See [base layout and contracts](docs/docs/base.md).
