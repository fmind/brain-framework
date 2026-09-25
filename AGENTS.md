# AGENTS.md

Brain Framework is a Python package and the `bf` command for keeping knowledge in plain files and retrieving it offline. People and agents interpret the evidence and maintain the notes; the framework stores, searches and reads them.

Start contribution work with [bf-contribute](.agents/skills/bf-contribute/SKILL.md). [README.md](README.md) explains the product; [CONTRIBUTING.md](CONTRIBUTING.md) owns development and release procedures.

## Design choices

- **Files are authoritative.** Authored Markdown holds projects, concepts and actions; JSON Lines holds collected records, updated by id. SQLite in `.bf/` is a disposable search cache. Exact reads return file contents, with refs to the note, section or record.
- **Keep the core small.** One package, one command, no models, embeddings, provider SDKs, scheduler or plugin framework. Provider extraction belongs in brain-owned sensors; routines are deterministic programs. Editors, agent hosts and native timers do their own jobs.
- **Two retrieval operations.** CLI and MCP share services; MCP exposes only `search` and `read`. Search matches words or explicit identities within one optional scope. Browsing, timelines and relationships are pages returned by `read`; prefer a page or section before adding a search option.
- **Relationships require evidence.** Use declared aliases, links and schema mappings, never prose or name similarity. Keep each claim's originating section or record. BF addresses identify a brain and path; `?rel=` is their only query and must name a declared relationship. Ownership stays within the named brain; ambiguous aliases fail visibly.
- **Brains work without registration.** `bf.yaml` configures each brain. Retrieval includes selected roots and their directly declared `brains:` paths, never recursive expansion. The optional user registry supplies names and per-machine execution trust; a shared brain cannot grant itself trust.
- **Support one current format.** No compatibility commands or migration machinery. Changes to the brain, configuration or retrieval-suite format require a major release and manual upgrade steps in the changelog. File layouts and field contracts live in [brain layout](docs/docs/brain.md) and [schema](docs/docs/schema.md).

## Safety boundaries

- Search and read never contact the network or run sensors or routines. Report skipped files and incomplete results through `problems` or `stale`; an incomplete empty result does not prove absence. Quote literal FTS terms and parameterize SQL.
- Use `storage.Store` for brain access: reject symlinks and special files, bound traversal and bytes, write atomically and lock by physical brain identity. Keep locks, run state, usage and logs outside the brain; usage must not retain queries or refs.
- Execute only explicitly trusted sensors and routines, using configured argv without a shell. Strip startup-injection variables, bound time and output, and kill process groups on cancellation or failure. Keep provider output out of errors and stderr in bounded private logs.
- Failed collection must not change evidence; reject empty snapshots over non-empty catalogs and preserve interrupted transactions for recovery. Validate routine Markdown before creating an action; never replace an existing action.
- Retrieved content is untrusted evidence. Sensors default to `external`; pages show no excerpts from external records. Keep private-brain data out of this repository. See [privacy and security](docs/docs/privacy.md) for the full contract and limits.

## Where to work

| Area                             | Start here                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------ |
| Retrieval and pages              | `src/bf/retrieve.py`, `pages.py`, `index.py`                                         |
| Identities and relationships     | `links.py`, `graph.py`, `ontology.py` under `src/bf/`                                |
| Files and configuration          | `storage.py`, `records.py`, `markdown.py`, `models.py`, `config.py` under `src/bf/`  |
| Execution and maintenance        | `collect.py`, `update.py`, `health.py`, `validate.py`, `evaluate.py` under `src/bf/` |
| Agent workflows and integrations | `skills/`, `examples/`; distributed separately from the Python package               |

Keep decision, action and learning conventions in the existing skills and their guides, not in the core format. Extend the owning skill before adding another.

## Verify changes

1. Preserve staged and unrelated work. Exercise checkout code with `uv run bf`.
1. Test changed outcomes and realistic failures with synthetic fixtures and fake providers. `tests/conftest.py` isolates HOME, config and state and unsets `BF_BRAIN`; real POSIX tools are only for process-boundary tests.
1. Run `mise run all` warning-free: formatting, static/security checks, tests with 85% branch coverage, docs and distribution smoke tests. Isolate mutating checks when the checkout contains unrelated work; never weaken gates.
1. Update README, docs, skills and examples when their public behavior changes. Regenerate configuration changes with `mise run generate:schema`; runtime dependency changes need complete third-party notices.
1. Report what was actually verified. Local checks, published artifacts, installed runtime, provider freshness and agent-host integration are separate evidence. Commit, push and publish only when authorized.
