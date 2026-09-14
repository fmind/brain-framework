# AGENTS.md

FKF is one typed Python package and one console command. Its purpose is to preserve useful evidence and deliver small, grounded context to agents. The README describes the product; this file defines implementation boundaries.

## Scope

- Keep only the current configuration and evidence format. Do not add legacy code, migration tooling, provider SDKs, or compatibility command surfaces.
- `projects/`, `tasks/` and `wiki/` contain authored Markdown. Tasks use `tasks/<task>/TASK.md` plus `inputs/` and `outputs/`. `records/` contains immutable version-1 normalized captures. `.fkf/` is a disposable SQLite cache; `indexes/` holds a generated source-structure export. `scripts/` holds operation scripts and `sources/` holds collectors. One `fkf.yaml` defines the base; ignored `fkf.local.yaml` may override source settings only.
- Sources emit records with `id`, `title`, optional `text`, `time`, `url`, `links`, `aliases`, and `attributes`. Provider-specific projection belongs in base-owned adapters. No provider SDK, model, embedding, scheduler, harness installer, or general plugin framework belongs in the core.
- CLI and MCP call the same services. MCP exposes exactly find, context, and read, with no execution or write operation.

## Invariants

- Ordinary retrieval is offline, bounded and reproducible. FTS uses literal terms; SQL uses parameters. Cached and in-memory fallback paths use the same indexing and ranking semantics. Replies name missing, stale or corrupt caches.
- Every selected reference resolves to durable evidence. References to a captured record bind the exact capture bytes and record id, independently of model defaults. Explicit aliases and authored links provide identity and relationship evidence; prose and human-name similarity do not create graph edges.
- Context's complete JSON plus final newline must fit four UTF-8 bytes per budget unit. Reject unsupported budgets and oversized exact reads rather than truncating evidence silently.
- Use the confined Store for base access. Refuse symlinks and special files below the base, bound traversal and bytes, write atomically, and serialize writers by physical base identity. Private lock state stays outside the base.
- Collection is explicit and uses direct configured argv. The owner controls source configuration and adapter code; there is no persistent approval registry or execution digest. Provider processes own credentials; sanitize startup/loader variables, PATH and base-resolving home/config roots. Kill process groups on timeout, cancellation or output overflow. Keep provider stdout/stderr out of errors.
- Retain a tested recovery path for durable evidence. Disposable caches never become the only source of knowledge.

## Code map

| Module         | Responsibility                                                                    |
| -------------- | --------------------------------------------------------------------------------- |
| models.py      | Strict evidence, configuration, query and output models; bounded JSON.            |
| storage.py     | Confined filesystem operations, private state, physical-base lock.                |
| config.py      | Strict YAML and source-only local overrides.                                      |
| collect.py     | Process boundary and immutable normalized collection.                             |
| index.py       | Markdown projections, explicit links, SQLite build/freshness/fallback and search. |
| retrieve.py    | Exact evidence reads and byte-budgeted context.                                   |
| evaluate.py    | Delivered-reference and answer-bearing-text acceptance cases.                     |
| cli.py, mcp.py | Thin public adapters.                                                             |
| markdown.py    | Shared Markdown projection, heading identities and exact section boundaries.      |

## Workflow

1. Inspect staged and unstaged changes and preserve user-owned work. Do not commit or push unless the session authorizes it.
1. Test changed behavior and realistic failures with synthetic fixtures. Tests use temporary HOME/state and unset FKF_BASE. Provider-backed commands always use fakes; real POSIX tools are allowed only to test the process boundary itself.
1. Run `mise run all` warning-free. Keep static/security checks, the 85% branch-coverage floor, documentation verification and distribution smoke tests. Removing an obsolete API removes its presentation tests; preserve meaningful safety and outcome coverage.
1. Generate configuration schema with `mise run generate:schema`. Update README, docs and skills together with public behavior. Runtime dependency changes require complete third-party notices.
1. Distinguish local qualification, release, installed runtime, provider freshness and native integration proof. Never infer one from another.

## Skills

`skills/` contains reusable user workflows distributed as Markdown resources, and `adapters/` contains reviewed standalone Python collectors that bases copy into `sources/`. `.agents/skills/fkf-contribute/` owns repository maintenance. Keep each workflow in one place; the Python package neither bundles nor installs skills or adapters. Adapters use the standard library only, call provider CLIs with literal argv, fail closed before partial output, and are tested with fake providers in `tests/test_adapters_*.py`. Start contribution work with that local skill.

## Active learning

After meaningful work, actively recommend one useful knowledge update when warranted: a project decision or next action, reusable wiki knowledge, or a repository-local skill via skillify. Extend the existing owning skill before adding a new one. Keep substantial procedures in `.agents/skills/`, with a short routing cue here. For an explicitly selected private base, use `skills/fkf-learn/SKILL.md`; keep private evidence out of this public repository. Routine verified updates require standing or task authorization; propose changes to accepted decisions and new skills. A routine lookup needs no task artifact.
