# AGENTS.md

Brain Framework is one typed Python package and one console command. Its purpose is to keep a person's or team's knowledge in plain files (Markdown notes, JSON Lines records) and let agents search and read it offline. The README describes the product; this file defines implementation boundaries.

## Scope

- Keep only the current configuration and brain format. Do not add legacy code, migration tooling, provider SDKs or compatibility command surfaces. Change the brain, `bf.yaml` or `evals/retrieval.yaml` format only in a major release, with manual upgrade steps in its changelog entry; teams pin one release.
- `projects/`, `actions/` and `concepts/` contain authored Markdown; concepts follow OKF v0.2. Actions use `actions/YYYY-MM-DD_slug/ACTION.md` plus `inputs/` and `outputs/`. `memories/<source>/<YYYY-MM>.jsonl` holds one line per source item, upserted by id (`undated.jsonl`, `snapshot.jsonl` for snapshot sources). `.bf/` is a disposable SQLite cache. `evals/` holds retrieval suites; `tests/` holds technical tests. `sensors/` holds sensors, `routines/` deterministic routines and other upkeep code, `settings/` and `tests/` brain maintenance, `skills/` workflow packages. One `bf.yaml` (version 5) defines the brain, direct named `brains:` paths, sensors and routines; search/read expand these once without requiring global configuration. Init defaults to no registration; `~/.config/bf/config.yaml` registers brains per user and grants collection trust per machine.
- Records have `id`, `title`, optional `text`, `time`, `url`, `links`, `aliases` `attributes` and normalized `fields`. `bf.yaml` declares typed schema fields and explicit sensor mappings. Provider-specific extraction belongs in brain-owned sensors. Reserved attributes `updated`, `observed` and `partial` describe revision provenance. Sensors declare `trust` (default `external`); pages never show excerpts of external records. No provider SDK, model, embedding, scheduler, harness installer or plugin framework belongs in the core.
- CLI and MCP call the same services. MCP exposes exactly `search` and `read`, with no execution or write operation. Listings, timelines, sources and relationships are pages that `read` resolves; search takes words and one scope. Add a page or a page section before a search option.

## Invariants

- Search and read never run sensors or routines or contact the network. They may refresh the disposable cache incrementally; a file that fails to parse is skipped and reported, never fatal. FTS uses quoted literal terms; SQL uses parameters.
- Every result carries a readable ref that resolves to a file: `path`, `path#section` or `source:id`. The cache may locate a record, but the answer always comes from the record file.
- Explicit aliases and links provide identity and relationship evidence; prose and name similarity never create relations. `bf://<bf.yaml name>/path#section` is a portable address, with edge queries interpreted only for BF links. Entity ownership stays within its brain namespace. Federation uses only selected roots and directly declared brains and fails visibly on ambiguous aliases; every claim retains its actual origin independently of supplied evidence.
- Use the confined Store for brain access. Refuse symlinks and special files below the brain, bound traversal and bytes, write atomically, and serialize writers by physical brain identity. Private lock, run state, usage counts and sensor logs stay outside the brain; usage never records queries or refs.
- Collection and routines are explicit, use direct configured argv from the brain root, and require per-machine trust in the user configuration. Routines are deterministic programs: the core never runs a model, validates routine Markdown before writing, and only creates a new day's action folder after success. Remove loader-injection variables, kill process groups on timeout, cancellation or output overflow, write nothing on provider failure (including an empty snapshot over a non-empty catalog) and recover interrupted commits from `memories/.pending/`, and keep provider stdout/stderr out of errors (stderr goes to a bounded private log).
- Files are the only source of truth. Deleting `.bf/` or the state directory loses nothing but convenience.

## Code map

| Module         | Responsibility                                                               |
| -------------- | ---------------------------------------------------------------------------- |
| links.py       | BF URI grammar, canonical identities and explicit link claims.               |
| graph.py       | Bounded selected-brain alias resolution and claim explanations.              |
| ontology.py    | Explicit field projection, schema validation and typed relationship edges.   |
| models.py      | Strict records, note metadata, configuration, queries and time parsing.      |
| storage.py     | Confined filesystem operations, private state, physical-brain writer lock.   |
| config.py      | Strict YAML, `bf.yaml`, the user registry and brain selection.               |
| records.py     | Monthly JSON Lines partitions: parse, upsert, snapshot replace, direct find. |
| markdown.py    | Markdown projection, heading identities, exact sections, link checks, OKF.   |
| index.py       | Incremental SQLite cache, ranking and time-window queries.                   |
| retrieve.py    | Multi-brain search, exact reads and page dispatch.                           |
| pages.py       | Home, folder, period, source and identity pages; backlinks; search scopes.   |
| collect.py     | Process boundary, collection, routines, run state and due windows.           |
| update.py      | Collect due sensors, run due routines of trusted brains, refresh caches.     |
| validate.py    | Whole-brain offline checks.                                                  |
| evaluate.py    | Owner-written retrieval cases.                                               |
| health.py      | Source and routine freshness, attention and the status report.               |
| usage.py       | Local usage counts in private state: operation and result count, no query.   |
| cli.py, mcp.py | Thin public adapters.                                                        |

## Workflow

1. Inspect staged and unstaged changes and preserve user-owned work. Do not commit or push unless the session authorizes it.
1. Test changed behavior and realistic failures with synthetic fixtures. Tests use temporary HOME, state and config directories and unset BF_BRAIN. Provider-backed commands always use fakes; real POSIX tools are allowed only to test the process boundary itself.
1. Run `mise run all` warning-free. Keep static and security checks, the 85% branch-coverage floor, documentation verification and distribution smoke tests. Removing an obsolete API removes its tests; preserve meaningful safety and outcome coverage.
1. Generate the configuration schema with `mise run generate:schema`. Update README, docs and skills together with public behavior. Runtime dependency changes require complete third-party notices.
1. Distinguish local qualification, release, installed runtime, provider freshness and native integration proof. Never infer one from another.

## Skills

`skills/` contains the user workflows (`bf-use`, `bf-learn`, `bf-action`, `bf-maintain`) distributed as Markdown, `examples/sensors/` four reviewed standalone sensors that brains copy into `sensors/`, `examples/routines/` a reviewed deterministic routine that brains copy into `routines/`, and `examples/hooks/` a read-only session-start hook, both tested with a fake `bf` in `tests/test_adapters_*.py`. `.agents/skills/bf-contribute/` owns repository maintenance; start contribution work there. The Python package neither bundles nor installs skills or sensors. Sensors use the standard library only, call provider CLIs with literal argv, fail closed before partial output, and are tested with fake providers in `tests/test_adapters_*.py`.

## Active learning

Decision workflows belong to the existing action and learning skills, with on-demand guides. Keep working context at most 300 words, six evidence refs and 4 KiB; bound dependency reviews to two hops and ten distinct dependents. The learning skill's stdin-only evidence helper retains selected exact reads and compares revisions without a model. These are optional file conventions, not implicit collection, general temporal inference or a new brain format.

After meaningful work, recommend one useful knowledge update when warranted: a project decision or next action, reusable concept knowledge, or a repository-local skill via skillify. Extend the owning skill before adding a new one. For a private brain, use `skills/bf-learn/SKILL.md` and keep private evidence out of this public repository.
