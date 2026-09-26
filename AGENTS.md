# AGENTS.md

Brain Framework is one Python package (`brain-framework`) and command (`bf`) for a brain kept in plain files its owner can inspect, edit and share. It provides collection, validation, a knowledge graph and offline retrieval; people and agents supply interpretation, judgment and action.

Start contribution work with [bf-contribute](.agents/skills/bf-contribute/SKILL.md). [README.md](README.md) explains the product's value and fit, [docs/docs/](docs/docs/) owns user-facing contracts, [CONTRIBUTING.md](CONTRIBUTING.md) owns development tasks and the [release checklist](.agents/skills/bf-contribute/references/release.md) owns publication.

## Product goal

Support the whole loop: **gather → normalize → organize and connect → act → learn**.

- **Gather and normalize.** Sensors select useful evidence from external tools and local files, preserve source identities and provenance, and emit a common record format. Explicit schema mappings put provider fields into a shared vocabulary.
- **Organize and connect.** Memories retain evidence; projects hold goals and decisions; concepts hold reusable knowledge. Markdown links, declared identities and typed relationships form a knowledge graph whose claims point back to their originating sections or records.
- **Act and learn.** Search, exact reads and graph pages assemble context for a person or agent. Actions retain the objective, inputs, outputs and next step; routines prepare repeatable reviews. People and agents perform authorized work, compare outcomes with expectations and update the owning notes through the workflow skills.

Judge a change by whether it helps someone gather relevant evidence, understand its connections or act with better context. Prefer a complete, inspectable path from source to decision over collecting more data or adding another abstraction. Keep the brain useful in an ordinary editor, with retrieval available through both the CLI and MCP.

## Design choices

- **Files are authoritative.** Authored Markdown holds projects, concepts and actions; JSON Lines holds collected records, updated by id. SQLite in `.bf/` is a disposable, self-refreshing search cache. Exact reads return file contents, with refs to the note, section or record.
- **Compose small tools.** Follow the Unix philosophy: persistent knowledge lives in files, and focused utilities compose through files, JSON and process interfaces. Use editors for authoring, Git for history, provider tools for access, native timers for scheduling and agent hosts for reasoning. Keep the core to one package and command, without AI model inference, embeddings, provider SDKs or a plugin framework. Provider extraction belongs in brain-owned sensors; routines are deterministic programs. Prefer an example, a skill guide or a deletion to new core code.
- **Two retrieval operations.** CLI and MCP share services; MCP exposes only `search` and `read`. Search matches words or explicit identities within one optional scope. Browsing, timelines and relationships are pages returned by `read`; prefer a page or section before adding a search option.
- **Machine-readable replies.** Command results print compact JSON on stdout; help and version output are plain text. Diagnostics go to stderr and name the file and position without quoting content. Exit codes are 0 success, 1 failure, 2 invalid input and 130 cancellation ([commands](docs/docs/commands.md)).
- **The graph grows from evidence.** Enrich notes with ordinary Markdown links and records with explicit identities and schema mappings. Use relative links or source URLs where sufficient; BF addresses add portable brain identities and declared relationship roles. Never infer identity or relationships from prose or name similarity. Keep each claim's originating section or record. BF addresses identify a brain and path; `?rel=` is their only query and must name a declared relationship. Ownership stays within the named brain; ambiguous aliases fail visibly.
- **Brains work without registration.** `bf.yaml` configures each brain. Retrieval includes selected roots and their directly declared `brains:` paths, never recursive expansion. The optional user registry supplies names and per-machine execution trust; a shared brain cannot grant itself trust.
- **Support one current format.** Keep compatibility and migration machinery out of the core. Breaking changes to persisted formats or public CLI/MCP reply contracts require a major release and manual upgrade steps in the changelog.

## Safety boundaries

- Search and read never contact the network or run sensors or routines. Report skipped files and incomplete results through `problems` or `stale`; an incomplete empty result does not prove absence. Quote literal FTS terms and parameterize SQL.
- Preserve pagination, chunk digests and source coverage across CLI and MCP. A page or chunk is not a complete result; a fresh cache does not prove fresh source evidence. See the [retrieval contract](docs/docs/search.md).
- Use `storage.Store` for brain access: reject symlinks and special files, bound traversal and bytes, write atomically and lock by physical brain identity. Keep locks, run state, usage and logs outside the brain; usage must not retain queries or refs.
- Execute only explicitly trusted sensors and routines, using configured argv without a shell. Strip startup-injection variables, bound time and output, and kill process groups on cancellation or failure. Keep provider output out of errors and stderr in bounded private logs.
- Failed collection must not change evidence; reject empty snapshots over non-empty catalogs and preserve interrupted transactions for recovery. Validate routine Markdown before creating an action; never replace an existing action.
- Registry collection trust authorizes execution; sensor `trust` describes content provenance. Neither makes retrieved text instructions. Sensors default to `external`; pages show no excerpts from external records. Keep private-brain data out of this repository. See [privacy and security](docs/docs/privacy.md) for the full contract and limits.

## Where to work

| Area                         | Start here                                                                                                                          |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Commands and MCP             | `cli.py`, `mcp.py` under `src/bf/`                                                                                                  |
| Retrieval and pages          | `retrieve.py`, `pages.py`, `index.py` under `src/bf/`                                                                               |
| Identities and relationships | `links.py`, `graph.py`, `ontology.py` under `src/bf/`                                                                               |
| Files and configuration      | `storage.py`, `records.py`, `markdown.py`, `models.py`, `config.py` under `src/bf/`                                                 |
| Execution and maintenance    | `collect.py`, `update.py`, `health.py`, `validate.py`, `evaluate.py`, `usage.py` under `src/bf/`                                    |
| Agent workflows              | [skills/README.md](skills/README.md) routes setup, discovery and daily use; skills ship separately with `metadata.version`          |
| Brain onboarding             | `src/bf/cli.py::AGENTS`: instructions emitted by `bf init`                                                                          |
| CLI/MCP contracts            | `tests/test_interfaces.py`, `tests/test_retrieval_boundaries.py`                                                                    |
| Sensors, routines and hooks  | `examples/`: reviewed copies users adapt in their brains, tested as processes in `tests/test_adapters_*.py`                         |
| Example brain                | `examples/brain/`: fictional brain exercised by `tests/test_example.py`; its `AGENTS.md` is sample content, not rules for this repo |
| Tasks, hooks and CI          | `mise.toml`, `lefthook.yml`, `.github/workflows/verify.yml` (the gate shared by CI and CD), `scripts/`                              |

Keep decision, action and learning conventions in the existing skills and their guides, not in the core format. Extend the owning skill before adding another.

Non-obvious constraints:

- `README.md` is also the PyPI page, so its links must be absolute (`tests/test_readme.py`).
- `skills/*/scripts/*.py` run with the agent's `python3`: standard library only, Python 3.11 syntax.
- `docs/bf.schema.json` is generated from the models; `check:schema` fails on drift.

## Evolve with evidence

- **Retrieval quality.** Capture a retrieval miss as a case in a retrieval suite (`evals/retrieval.yaml`, run by `bf eval`) before changing ranking, and keep the example brain's cases passing. With authorization, also evaluate on a copy of a real brain and report only aggregate results.
- **Performance.** For indexing, retrieval or storage changes, compare `mise run benchmark` before and after and report the numbers.
- **Changelog.** Add user-visible changes under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md) (`Added`, `Changed`, `Fixed`), stated as outcomes for users. Versions, tags and pinned installation examples change only in an authorized release.

## Verify changes

1. Preserve staged and unrelated work. `mise run install` syncs dependencies and Git hooks; exercise checkout code with `uv run bf` on a disposable brain.
1. Run focused tests with `TZ=UTC uv run pytest tests/test_search.py -k NAME`. Test changed outcomes and realistic failures with synthetic fixtures and the fake providers in `tests/conftest.py`, which isolates HOME, config and state and unsets `BF_BRAIN`; real POSIX tools are only for process-boundary tests.
1. For onboarding or workflow changes, save a decision in a disposable brain, search for its reason, read the returned ref, validate and evaluate. Isolate config and state as well as the brain.
1. Run `mise run all` warning-free: formatting, static/security checks, tests with 85% branch coverage, docs and distribution smoke tests. Isolate mutating checks when the checkout contains unrelated work; never weaken gates.
1. Update README, docs, skills and examples in the same change as public behavior. Regenerate configuration changes with `mise run generate:schema`; runtime dependency changes need complete third-party notices (`tests/test_notices.py`).
1. Report what was actually verified. Local checks, published artifacts, installed runtime, provider freshness and agent-host integration are separate evidence. Commit, push and publish only when authorized.
