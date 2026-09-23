# AGENTS.md

FKF is one typed Python package and one console command. Its purpose is to keep a person's or team's knowledge in plain files (Markdown notes, JSON Lines records) and let agents search and read it offline. The README describes the product; this file defines implementation boundaries.

## Scope

- Keep only the current configuration and base format. Do not add legacy code, migration tooling, provider SDKs or compatibility command surfaces.
- `projects/`, `tasks/` and `wiki/` contain authored Markdown; wiki concepts follow OKF v0.2. Tasks use `tasks/YYYY-MM-DD_slug/TASK.md` plus `inputs/` and `outputs/`. `records/<source>/<YYYY-MM>.jsonl` holds one line per source item, upserted by id (`undated.jsonl`, `snapshot.jsonl` for snapshot sources). `.fkf/` is a disposable SQLite cache. `sources/` holds collectors, `scripts/`, `configs/` and `tests/` base maintenance, `skills/` workflow packages. One `fkf.yaml` (version 2) defines the base; `~/.config/fkf/config.yaml` registers bases per user and grants collection trust per machine.
- Records have `id`, `title`, optional `text`, `time`, `url`, `links`, `aliases` and `attributes`. Provider-specific projection belongs in base-owned collectors. No provider SDK, model, embedding, scheduler, harness installer or plugin framework belongs in the core.
- CLI and MCP call the same services. MCP exposes exactly `search` and `read`, with no execution or write operation.

## Invariants

- Search and read never run collectors or contact the network. They may refresh the disposable cache incrementally; a file that fails to parse is skipped and reported, never fatal. FTS uses quoted literal terms; SQL uses parameters.
- Every result carries a readable ref that resolves to a file: `path`, `path#section` or `source:id`. The cache may locate a record, but the answer always comes from the record file.
- Explicit aliases and links provide identity and relationship evidence; prose and name similarity never create relations.
- Use the confined Store for base access. Refuse symlinks and special files below the base, bound traversal and bytes, write atomically, and serialize writers by physical base identity. Private lock, run state, usage counts and collector logs stay outside the base; usage never records queries or refs.
- Collection is explicit, uses direct configured argv from the base root, and requires per-machine trust in the user configuration. Remove loader-injection variables, kill process groups on timeout, cancellation or output overflow, write nothing on failure, and keep provider stdout/stderr out of errors (stderr goes to a bounded private log).
- Files are the only source of truth. Deleting `.fkf/` or the state directory loses nothing but convenience.

## Code map

| Module         | Responsibility                                                               |
| -------------- | ---------------------------------------------------------------------------- |
| models.py      | Strict records, note metadata, configuration, queries and time parsing.      |
| storage.py     | Confined filesystem operations, private state, physical-base writer lock.    |
| config.py      | Strict YAML, `fkf.yaml`, the user registry and base selection.               |
| records.py     | Monthly JSON Lines partitions: parse, upsert, snapshot replace, direct find. |
| markdown.py    | Markdown projection, heading identities, exact sections, link checks, OKF.   |
| index.py       | Incremental SQLite cache, ranking and time-window queries.                   |
| retrieve.py    | Multi-base search and exact reads.                                           |
| collect.py     | Process boundary, collection, run state and due windows.                     |
| update.py      | Collect due sources of trusted bases, then refresh caches.                   |
| validate.py    | Whole-base offline checks.                                                   |
| evaluate.py    | Owner-written retrieval cases.                                               |
| usage.py       | Local usage counts in private state: operation and result count, no query.   |
| cli.py, mcp.py | Thin public adapters.                                                        |

## Workflow

1. Inspect staged and unstaged changes and preserve user-owned work. Do not commit or push unless the session authorizes it.
1. Test changed behavior and realistic failures with synthetic fixtures. Tests use temporary HOME, state and config directories and unset FKF_BASE. Provider-backed commands always use fakes; real POSIX tools are allowed only to test the process boundary itself.
1. Run `mise run all` warning-free. Keep static and security checks, the 85% branch-coverage floor, documentation verification and distribution smoke tests. Removing an obsolete API removes its tests; preserve meaningful safety and outcome coverage.
1. Generate the configuration schema with `mise run generate:schema`. Update README, docs and skills together with public behavior. Runtime dependency changes require complete third-party notices.
1. Distinguish local qualification, release, installed runtime, provider freshness and native integration proof. Never infer one from another.

## Skills

`skills/` contains the user workflows (`fkf-use`, `fkf-learn`, `fkf-maintain`) distributed as Markdown, and `examples/sources/` three reviewed standalone collectors that bases copy into `sources/`. `.agents/skills/fkf-contribute/` owns repository maintenance; start contribution work there. The Python package neither bundles nor installs skills or collectors. Collectors use the standard library only, call provider CLIs with literal argv, fail closed before partial output, and are tested with fake providers in `tests/test_adapters_*.py`.

## Active learning

After meaningful work, recommend one useful knowledge update when warranted: a project decision or next action, reusable wiki knowledge, or a repository-local skill via skillify. Extend the owning skill before adding a new one. For a private base, use `skills/fkf-learn/SKILL.md` and keep private evidence out of this public repository.
