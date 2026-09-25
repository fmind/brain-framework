# Changelog

All notable changes to Brain Framework (formerly FKF) are documented here. This project follows [Semantic Versioning](https://semver.org/) from its first public release.

## [v11.1.0](https://github.com/fmind/brain-framework/releases/tag/v11.1.0) - 2026-09-25

Brain Framework 11.1 adds optional decision workflows to the agent skills and hardens collection, retrieval and validation after a full review. The brain format is unchanged: `bf.yaml` and retrieval suites stay at version 5.

### Added

- Decision workflows in the `bf-action` and `bf-learn` skills, loaded on demand: a small working context in each action (`## Context {#context}` of at most 300 words, six refs and 4 KiB, and `## Resume {#resume}`), decisions with alternatives and expected outcomes, conditional intentions, explicit unknowns, belief revision with a declared `supersedes` role, dependency review bounded to two hops and ten dependents, procedures learned from outcomes and reviewed transfer to another brain.
- `skills/bf-learn/scripts/evidence.py`, a standard-library helper for Python 3.11 or later: `capture` retains one exact `bf read` with its digest and limitations, and `compare` returns a compact `changed`, `unchanged` or `unknown` verdict for a new read. It reads stdin only and never opens a brain, runs a provider or uses a model.
- An [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) page covers the skills, the everyday loop, resuming actions, decision workflows and evidence captures.
- The example brain demonstrates a decision review: a fictional policy, a superseded decision, an intention, an unknown, a draft procedure, a prepared transfer and retrieval cases for each.

### Changed

- Search excerpts are one line and start below a section's heading, which the result title already names. The search cache rebuilds automatically.
- Exact record reads and search coverage report the same freshness as `bf status` and source pages: a trusted scheduled sensor that never succeeded is `never`. An unreadable registry grants no trust and leaves freshness `unknown`.
- An invalid `--scope`, `--since` or `--until` exits 2 as invalid input, like other command-line errors. A closed terminal (SIGHUP) cancels like SIGTERM and kills running providers; `nohup` keeps it ignored.
- `bf status --check` fails only for scheduled sensors this machine runs, as documented; a failed manual collection is still reported with its log.
- Registered brains win over a same-named directory below the working directory for `--brain NAME`.

### Fixed

- The brain writer lock follows the brain directory's device and inode, so bind mounts and differently spelled paths of one brain no longer commit concurrently. Writers must share one private state directory.
- A state directory below a linked ancestor, such as `/home` linked to `/var/home`, no longer breaks every command; the state root itself still may not be a link.
- A registry entry with a relative path is rejected instead of granting collection trust to whatever that path means in the working directory.
- Relative `PATH` entries no longer let a bare command resolve inside the brain, and every `PYTHON*` variable plus Java, Lua and `GCONV_PATH` startup variables are removed before a sensor or routine starts. Brain executables may exceed 1 MiB.
- Invalid sensor output is reported by record position and field name only: keys the provider printed no longer reach errors, logs or run history.
- An empty snapshot no longer erases a non-empty catalog; the run fails and keeps the records. Delete `memories/SOURCE/` to clear a source deliberately.
- A backfill whose `--until` lies in the future no longer blocks later scheduled successes: coverage never extends past the run.
- A routine run skipped because today's action exists no longer loses its window; the next written action covers it.
- A stored revision claiming a modification after it was first observed, such as a file with a future mtime, no longer freezes a record against newer collections.
- A note dated `0001-01-01` or `9999-12-31` is invalid instead of crashing searches and pages; a damaged cache reports "run bf build" instead of a traceback.
- `read` keeps answering from the other brains when one selected brain has an invalid `bf.yaml`, and reports it under `problems`.
- An alias claimed by several notes no longer merges their backlinks in scoped searches and identity pages.
- Identity searches include links to a note's sections, like scoped searches and backlinks.
- Period pages order `changed` items by modification time across brains.
- `memories/SOURCE/PERIOD` for an unknown source is a missing page instead of an empty answer.
- Reading an identity or an absent source no longer waits for a running writer.
- A record id too long for a BF address still reads, with its backlinks reported as unavailable.
- Headings without word characters get an addressable `section` slug; explicit anchors cannot end in `.md`; a link to a non-Markdown file whose name contains `#` validates.
- The session hook and the weekly review show local dates, and the weekly review writes one paragraph per line and fences record ids so that none can become a link.

## [v11.0.0](https://github.com/fmind/brain-framework/releases/tag/v11.0.0) - 2026-09-25

Brain Framework 11 turns listings into pages, gives actions a resumable page and schedules deterministic routines next to sensors. Search takes words and one scope; everything else is a page that `read` resolves. The brain format changes: `bf.yaml` and retrieval suites move to version 5.

### Changed

- `bf read` without a ref returns the home page: active and blocked projects, the latest actions, notes changed in 7 days, record activity per source in 24 hours, items in the coming 7 days, and scheduled sensors or routines that need `attention`. Projects are marked for `review` when their note is older than 14 days or items dated after it link to them (`new_links`), and show open `tasks` with the `next` one.
- `read` resolves pages: folders (`projects`, `concepts`, `actions` and subfolders), periods (`today`, `yesterday`, `YYYY-MM-DD`, `YYYY-MM`, `12h`, `7d`, `2w`) with items modified in the period and each source's share, `memories`, `memories/SOURCE` with its partitions, and `memories/SOURCE/PERIOD`, `snapshot` or `undated`. Pages combine the selected brains, stay bounded (50 period or source items, 200 notes per folder, 20 per section) and report totals; `bf://NAME/` and `bf://NAME/PAGE` select one brain.
- Whole note and record reads include `backlinks` across the selected brains, grouped by explicit relationship with claim explanations, and `claims` whose explicit subject is the item. An identity without an owning note reads as a page of what links to it. `bf read actions/FOLDER` returns ACTION.md with the action's files and the projects it links to.
- **Breaking**: `bf search QUERY [--scope SCOPE] [--limit N]` requires words or an identity. A scope is a folder or file, a period or an identity. `--since`, `--until`, `--source`, `--type`, `--status`, `--recent`, `--changed-since`, `--current`, `--relation`, `--target` and `--subject` are removed. MCP `search` takes `query`, `scope` and `limit`; MCP `read` accepts an empty ref for the home page.
- **Breaking**: `bf.yaml` version 5 declares `routines:`, deterministic programs that `bf update` runs after the sensors with the same trust, process boundary, timeout, output bound and private log. A routine's Markdown is validated and written as `actions/YYYY-MM-DD_NAME/ACTION.md`; an existing action is never replaced, empty output writes nothing, and a failure writes nothing and keeps it due. Routine names are action slugs distinct from sensor names. `bf status` reports routines, and `--check` fails on stale or failed ones.
- **Breaking**: retrieval suites use version 5. A case is a search (`query`, optional `scope` and `limit`) or a read (`read`: a page, note, record or identity), checked with `expect`, `forbid`, `text` and `empty`; a read that finds nothing is empty.
- Sensors declare `trust`: `owner` for text the brain's owner writes, `external` (the default) for mail, chat, invitations, issues, feeds and other third-party text. Pages show external records by title and ref without excerpts; searches, exact reads and backlinks label them `external`; source coverage reports each source's trust, and undeclared historical sources are external.
- `bf status` no longer lists `review`; project review moved to pages. Process errors name the program rather than a collector, since sensors and routines share the runner. The search cache rebuilds automatically.

### Added

- The `bf-action` skill starts, resumes and closes one action, a single session of work, when the user asks for it; the action template moves there from `bf-learn`.
- `examples/routines/weekly-review.py` renders a weekly review action from `bf read` pages, with a fake-`bf` test and a README contract. It names external items by ref only.
- `examples/hooks/session-context.py` prints a short context for the current repository at agent session start (project, review signal, next task, linked evidence), and nothing when unavailable.

### Fixed

- A manual backfill that ends before a sensor's recorded coverage no longer replaces that coverage, moves its resume point or marks the sensor fresh; a contiguous backfill extends coverage backwards without counting as a fresh success.

### Manual upgrade from 10

1. Pause scheduled writers. Install the same Brain Framework 11 build for every CLI, MCP host, project environment and scheduled writer, and restart long-running MCP readers.
1. Change `bf.yaml` to `version: 5`. Every sensor is now `external` by default: add `trust: owner` to the sensors whose text you write yourself, such as local Git history or your own documents.
1. Change every suite under `evals/` to `version: 5`. Rewrite cases that used removed fields: a time window or filter becomes a `read` of a page (`7d`, `2026-09`, `projects`, `memories/SOURCE`) or a search `scope`; a `relation`, `target` or `subject` case becomes `read: IDENTITY` with `expect`, and `text` naming the role when it matters.
1. Replace scripts, routines, skills and agent instructions that call removed search options: pages for listings and timelines, `bf read IDENTITY` for backlinks and claims, `--scope` for bounded searches. Update the `AGENTS.md` that earlier `bf init` generated, and reinstall `bf-use`, `bf-learn`, `bf-maintain` and the new `bf-action`.
1. Optionally move scheduled review scripts into `routines/` and declare them under `routines:`. Review them like sensors before a trusted machine runs them.
1. Run `bf build`, `bf validate` and `bf eval`; compare the home, `projects` and period pages with your previous listings, then resume the scheduled writer.

No migration tooling or legacy command surface is included. Runtime dependencies are unchanged.

## [v10.0.0](https://github.com/fmind/brain-framework/releases/tag/v10.0.0) - 2026-09-25

### Changed

- Declare directly related brains in `bf.yaml` with stable names and relative, absolute or home-relative paths. Search, read, MCP and evaluations include direct references without recursive discovery or required global configuration. Missing references report incomplete scope; conflicting names are excluded.
- `bf init` defaults to no global registration or collection trust; explicit `--collect` opts in. Maintenance commands do not expand references, and references never authorize sensors. Retrieval suites accept qualified BF addresses to distinguish same-named files across brains.
- Add portable `bf://brain/path#section` addresses, explicit note `entity` identities and typed note `fields`. BF link queries encode declared relationships and attributes, with explicit subject/evidence/attribution and an immutable origin in the derived SQLite projection.
- Add selected-brain alias federation, incoming `target` and outgoing `subject` filters, and relationship explanations through CLI, MCP and retrieval suites. Ambiguous aliases do not merge, external URL queries stay opaque, and foreign links never broaden brain selection.
- Headings support stable `{#anchor}` identifiers. Validation checks local BF addresses and reports foreign unresolved targets. New brains include a small relationship vocabulary and agent instructions for links.

- Brain format 4 restores explicit `schema` fields in `bf.yaml`: descriptions, strict types, cardinality, validated examples and typed relationships. Sensors map their record output with JSON Pointers or constants; normalized values are stored in record `fields` and included in offline search.
- Typed relationships form a disposable, evidence-backed SQLite graph. CLI, MCP search and retrieval cases accept `relation` and `target` together, with exact identities and explicit owner aliases. Schema changes invalidate the cache; record replacement and snapshot deletion remove obsolete edges.
- `bf eval` discovers YAML suites recursively under `evals/`; `--path` selects a suite or directory. Technical tests remain in `tests/`. `bf init --full` creates both folders.
- Git and Calendar example sensors preserve author/repository and organizer/attendee roles for explicit mapping.

### Manual upgrade from 9

1. Preserve the brain files, configuration, machine state and previous runtime; pause all collection writers and finish or recover pending transactions.
1. Install the same Brain Framework 10 build for every CLI, MCP host, project environment and scheduled writer. Restart long-running readers after the switch.
1. Change `bf.yaml` to `version: 4`. Declare shared fields under `schema` and each sensor's `fields` mapping. Keep provider transformations in sensors; do not infer missing relationships from names or flattened links.
1. Create `evals/`, move `queries.yaml` to `evals/retrieval.yaml`, change suite `version` to 4, and update paths in tasks, docs and skills. Multiple named suites may share the directory.
1. Existing records without normalized fields remain readable. Preserve originals before explicitly backfilling fields from retained structured evidence; unavailable historical roles remain unknown. Mapping changes alone do not rewrite history or contact providers.
1. Add directly related brains under `brains: {team: {path: ../team}}` in `bf.yaml`; ensure target names match. Readers now include these direct references, so review the intended audience. Use paths or local names for discovery. Existing optional registrations remain usable; newly initialized brains need explicit `--collect` before collection.
1. Keep the brain `name` stable across clones; use it as the BF URI authority. Declare roles before adding BF link queries. Give entity notes explicit `entity` identities and reviewed aliases; do not reinterpret external URL queries or fabricate historical relationships. Refresh separately installed skills and brain agent instructions.
1. Run `bf build`, `bf validate`, technical tests and `bf eval`; check role-specific queries and exact reads, then resume the scheduled writer. Keep originals until recovery is verified. Rollback requires the prior runtime and original configuration and records, not only a cache rebuild.

No migration tooling or legacy command surface is included. Runtime dependencies are unchanged.

## [v9.2.0](https://github.com/fmind/brain-framework/releases/tag/v9.2.0) - 2026-09-24

Brain Framework 9.2 starts new brains smaller, checks action folders and repairs the PyPI project page. The brain format is unchanged: existing version 3 brains need no conversion.

### Added

- `bf init --full` also creates the optional versioned folders: `memories/`, `assets/`, `sensors/`, `routines/`, `settings/`, `skills/` and `tests/`.
- `bf validate` reports action folders that are not named `YYYY-MM-DD_slug` or lack `ACTION.md`. Rename such folders with `git mv`; validation then reports any links to update.
- Document the optional brain folders: versioned `assets/` for media that notes link to, and the unversioned `inputs/`, `originals/` and `logs/` that new brains already ignore.

### Changed

- `bf init` creates only `projects/`, `concepts/` and `actions/`; other folders appear when first needed.
- `bf register` keeps the registry's leading comment lines when it rewrites the file.
- Search, MCP and retrieval cases resolve relative times in one place. An invalid search time exits 2 as invalid input, and an invalid retrieval case names the case.
- `bf status` builds its report in the health service; the CLI stays a thin adapter.
- PyPI publication accepts deployments only from `v*` tags.

### Fixed

- README links are absolute, so the PyPI project page reaches the documentation, skills and examples; a test keeps every link resolvable offline.
- `bf eval` names a missing `queries.yaml` instead of reporting an inaccessible file.
- Claude Code discovers the repository's `bf-contribute` skill through `.claude/skills`.

## [v9.1.0](https://github.com/fmind/brain-framework/releases/tag/v9.1.0) - 2026-09-24

Brain Framework 9.1 prepares team deployments. The brain format is unchanged: existing version 3 brains need no conversion.

### Added

- `bf init --no-collect` registers a shared brain for search without collection trust, and `init` accepts a fresh clone of an empty repository.
- A [team brains](https://fmind.github.io/brain-framework/docs/team/) guide: distinctive names, joining, CI collection with preserved run state, allowlisted `memories/` publication, required review of sensor code, and data retention.
- A documented compatibility policy: the brain, `bf.yaml` and `queries.yaml` formats change only in a major release, with manual upgrade steps.

### Fixed

- `bf init` anchors `.gitignore` patterns to the brain root, so `actions/*/inputs/` stay versioned and a teammate's clone validates like the author's copy. Existing brains that copied the old patterns should prefix `inputs/`, `logs/`, `memories/`, `originals/` and `.bf/` with `/`.
- `bf init` names a brain after its directory instead of `knowledge`, avoiding registry name collisions between personal and team brains, and explains how to resolve a taken name.
- Unsupported platforms fail with a clear message instead of an import traceback.

### Changed

- Call executable collectors sensors in command help, documentation and the `bf-maintain` skill; record refs, `--source`, health `sources` and OKF `sources` keep their provenance names. `search --source` now has help text.

## [v9.0.1](https://github.com/fmind/brain-framework/releases/tag/v9.0.1) - 2026-09-24

First published Brain Framework release. The v9.0.0 candidate stopped at the macOS ARM64 concurrent-registration check before publication; its tag remains unchanged. This release includes the complete identity and format transition described below.

### Fixed

- Recover when a competing process creates the shared lock during its initial open, while retaining the same confined parent and no-follow checks. A missing or unsafe lock still fails explicitly.
- Cover initial lock creation races, persistent missing locks and unsafe contenders without relaxing writer serialization.

### Breaking changes

- Rename FKF to Brain Framework: install `brain-framework`, import `bf`, and run `bf`. The repository and documentation move to `fmind/brain-framework`.
- Use format 3 for `bf.yaml` and `queries.yaml`, with `sensors:`, `{{brain}}`, `--brain`, `BF_BRAIN`, and a `brains:` registry under `~/.config/bf/`. Private runtime state lives under `~/.local/state/bf/`.
- Organize authored knowledge in `projects/`, `concepts/`, and `actions/` with `ACTION.md`; collected records in `memories/`; collection code in `sensors/`; maintenance in `routines/` and `settings/`; disposable search in `.bf/`.
- Rename workflows to `bf-use`, `bf-learn`, `bf-maintain`, and `bf-contribute`. CLI and MCP results identify their brain; collection reports identify sensors while record provenance retains `source:id` and OKF `sources`.
- Keep one current format without legacy aliases. Preserve original evidence and per-machine collection trust during any one-time transition, and rebuild caches from the migrated files.

### Preserved

- Offline search and exact reads, two read-only MCP tools, plain-file evidence, bounded trusted collection, atomic recovery and external scheduling.
- Stable record IDs, provider provenance, explicit concept types, and searchable Markdown in action inputs and outputs.

## [v9.0.0](https://github.com/fmind/brain-framework/releases/tag/v9.0.0) - 2026-09-24

Unpublished transition candidate. Publication stopped at the macOS ARM64 concurrent-registration gate; the tag remains unchanged. The corrected transition is released in v9.0.1.

## [v8.2.2](https://github.com/fmind/fkf/releases/tag/v8.2.2) - 2026-09-24

### Fixed

- Refuse collection into a source with duplicate stored IDs before changing evidence, including snapshot replacement.
- Serialize concurrent base registrations and write the registry durably with owner-only permissions.
- Preserve `#` and percent escapes in note filenames and local links; require exact record IDs in retrieval cases.

### Changed

- Explain the value of shared, file-based knowledge through concrete questions and the search, read, work and update loop.
- Add runnable team-pilot retrieval cases, routine upgrade instructions and host connection checks.
- Align documentation and skills on base selection, skill discovery, collection trust, recovery and native scheduling.

## [v8.2.1](https://github.com/fmind/fkf/releases/tag/v8.2.1) - 2026-09-23

First published 8.2 release. The v8.2.0 candidate stopped at the Intel macOS test gate before publication; its tag remains unchanged.

FKF 8.2 makes incomplete results visible, preserves evidence through interrupted writes, and helps distinguish recently edited knowledge from historical records. Existing version 2 bases need no conversion; disposable search caches rebuild automatically.

### Added

- `search --changed-since` and `--current` in the CLI, MCP and retrieval cases, with active, disabled and historical source coverage, freshness and change counts.
- Reserved record attributes `updated`, `observed` and `partial` distinguish upstream revision, collection time and incomplete content from event time.
- Scoped local document extraction and optional Calendar agenda snapshots in the standalone collector examples.

### Fixed

- Select test timezones before Python starts, preserving all DST and shared-cache checks on builds without `time.tzset()`, including Intel macOS.
- Recover interrupted multi-partition commits from durable originals through explicit `build`, collection or backup. Serialize source runs and state updates, preserve directory entries before journal writes, and bound partitions before writing.
- Preserve newer upstream revisions during backfills, repair duplicates and corrupt caches, and retry concurrent cache replacement.
- Report skipped files and unavailable bases in search; reject incomplete retrieval-case answers. Fail stale-cache health checks and skipped-file updates, reject malformed partition paths, confine link validation, and distinguish unreadable record sources from missing records.
- Correct recent and identity ordering, per-item passage selection, local-date and DST handling, and malformed-note isolation. Expose identity ambiguity and OKF provenance links.
- Validate Drive snapshot response identity before accepting empty catalogs, bound provider output while subprocesses run, and cancel collector descendants on SIGTERM.

### Changed

- Rewrite onboarding around a runnable first decision and a small team pilot. Clarify offline and security limits, scheduler cadence and example isolation.
- Expand failure tests, French retrieval cases and the benchmark's changed and unchanged record paths. No runtime dependencies or services added.

## [v8.1.0](https://github.com/fmind/fkf/releases/tag/v8.1.0) - 2026-09-23

### Added

- `fkf status` lists active or blocked project notes whose `updated` date is more than 14 days old under `review`, as a reminder rather than a failure.
- `search` and `read` keep local usage counts in the private state directory (time, operation and result count, never the query), summarized by `fkf status` over 7 and 30 days so an owner can see whether agents use a base. Retrieval cases are not counted.

### Changed

- The example Git collector skips hidden repositories, repositories named with `--skip`, and bot or reserved-test-domain authors.

## [v8.0.1](https://github.com/fmind/fkf/releases/tag/v8.0.1) - 2026-09-23

### Changed

- Match English word forms with the FTS5 Porter stemmer, so `meetings` finds `meeting` and `decided` finds `decide`. Existing caches rebuild on the next search.

## [v8.0.0](https://github.com/fmind/fkf/releases/tag/v8.0.0) - 2026-09-22

FKF 8 focuses on the loop that makes a knowledge base useful: collect on a schedule, search from anywhere, read the exact source, keep notes current. It removes machinery that made bases hard to run and notes hard to read.

### Breaking changes

- Store records as monthly JSON Lines, `records/<source>/<YYYY-MM>.jsonl`, with one line per source item upserted by id; snapshot sources keep one `snapshot.jsonl`. Immutable per-run capture files, capture hashes, latest/historical snapshots and `--history` are removed; Git or backups keep history. This also removes the 20,000-file ceiling that hourly collection reached within two months.
- Replace `find` and `context` with one `search [QUERY] [--since] [--until] [--source] [--type] [--status] [--limit] [--recent]`. An empty query lists a time window newest first. Byte budgets are removed; `--limit` bounds results.
- Use readable refs everywhere: `path`, `path#section` and `source:id`. Base ids, `fkf://` qualified references and `record:<hash>` URIs are removed.
- `fkf.yaml` is `version: 2` with a `name` and no `id`; `fkf.local.yaml` is removed. Bases are registered per user in `~/.config/fkf/config.yaml`, which also grants collection trust per machine.
- `collect SOURCE [--since] [--until] [--dry-run]` replaces positional windows and `--preview`. `build` always rebuilds from scratch; `--check` and `--if-stale` are removed.
- Remove containers, `parents`, `--within` and `indexes/structures.json`; link folders and labels with ordinary `links`. Remove note supersession, the special `## History` heading, trust-signal computation, `reviewed` and `effective`; note status is one of draft, active, paused, blocked, done, stable, deprecated or archived, and `updated` dates a note.
- MCP exposes `search` and `read`.
- Convert a v7 base with a one-off script in that base; see the upgrade notes in the documentation.

### Added

- Search every registered base from any directory, or only the enclosing base; results name their base.
- `fkf register PATH [--collect]` adds a cloned team base; a base never runs collectors on a machine that has not trusted it.
- The search cache refreshes itself incrementally before each query, skips and reports unparsable files, and keeps answering, marked `stale`, while another writer holds the base.
- Relative times: `now`, `today`, `yesterday`, `12h`, `7d`, `2w` and local `YYYY-MM-DD` dates.
- `fkf status [--check]` reports each source's last run, success, error and private stderr log, and fails when a trusted scheduled source is stale.
- `fkf update` covers every trusted base, resumes each window source from its last success with overlap, catches up at most 30 days, and keeps run state outside the base.
- `fkf validate` reports every problem at once, including broken links, missing headings, cited records that do not exist and misplaced or duplicate records.

### Changed

- Rank exact identities first, then items matching all words, then any word; notes above records; deprecated and archived notes last; one result per note through its best section.
- Collectors run from the base root with the user's environment minus loader-injection variables, and write stderr to a bounded private log.
- Retrieval cases use `version: 2` with `expect`, `forbid`, `text`, `empty` and time filters.
- Mark the package as beta while the format settles.

## [v7.0.1](https://github.com/fmind/fkf/releases/tag/v7.0.1) - 2026-09-21

First published v7 release. The v7.0.0 candidate stopped at the macOS package gate before publication; its tag remains unchanged.

### Fixed

- Resolve temporary smoke environments to physical paths, including macOS `/var`, so strict private-state symlink protection remains enabled during distribution checks. Copy packages explicitly when temporary environments and the cache use different filesystems.

### Breaking changes

- Replace the Go implementation with one typed Python package and the `fkf` command. Python 3.14 or newer on Linux or macOS is required; install with `uv tool install --python 3.14 'fkf==7.0.1'`.
- Use one current base format: `fkf.yaml`, authored `projects/`, `wiki/` and `tasks/`, immutable normalized `records/`, and disposable `.fkf/` and `indexes/`. Preserve a v6 base and executable separately; there is no in-place migration or compatibility command.
- Remove bundled provider integrations, presets, harness installation, execution approval registries and learning proposal machinery. Bases own collectors, schedules and knowledge editing. Reusable Markdown skills and three source examples are maintained separately from the Python distribution.
- Require an explicitly built, ready cache for indexed retrieval. Missing, stale or corrupt caches name the `fkf build` recovery command; direct authored and capture-file reads remain available.

### Added

- Explicit `update` with a side-effect-free dry-run, per-source refresh policy and durable successful automatic checkpoints. Failed windows stay due, manual collections do not move automatic progress, and `build --if-stale` skips a ready index.
- Base-qualified exact evidence references, explicit source structure and membership filters, current versus historical capture retrieval, and authored section references.
- OKF v0.2 wiki structure validation, provenance and asserted verification signals, lifecycle filters, and resumable task folders with inputs and outputs.
- A runnable fictional base with a credential-free collector and retrieval acceptance cases; focused `fkf-use`, `fkf-learn`, `fkf-maintain` and repository-local `fkf-contribute` skills.

### Changed

- Keep CLI and the three read-only MCP tools on shared services. Context includes the complete JSON and final newline within four UTF-8 bytes per budget unit; MCP also counts its response wrapper.
- Match literal lexical terms with case and diacritic folding, explicit identities and authored-note preference. Keep event-time filtering separate from capture recency and decision validity.
- Confine filesystem access, bound subprocess output and runtime, sanitize collection environments, and preserve atomic immutable evidence and a tested rebuild/recovery path.
- Publish a documented installation and upgrade path, complete command reference, skill setup example, and contributor release checklist. Remove obsolete implementation proposals from the current tree.

## [v6.0.2](https://github.com/fmind/fkf/releases/tag/v6.0.2) - 2026-09-09

### Fixed

- Share a fifteen-second deadline across passive-hook children while allowing individual context calls up to ten seconds. Expired budgets prevent new children, and timed-out process groups are terminated.
- Create every missing parent directory with owner-only permissions during atomic writes, without changing existing directory modes.
- Isolate the security-rule checkout from Git environment variables inherited by linked-worktree hooks, while retaining rejection of local rule edits.

## [v6.0.1](https://github.com/fmind/fkf/releases/tag/v6.0.1) - 2026-09-09

### Fixed

- Keep passive hooks compatible with system Python 3.9 and newer, independently of the FKF package’s Python 3.14 environment. The formatter now preserves that syntax boundary and a regression test checks it.
- Consolidate the hook’s validated direct-argv dispatch through the fixed system `env` executable.

## [v6.0.0](https://github.com/fmind/fkf/releases/tag/v6.0.0) - 2026-09-09

### Breaking changes

- Consolidate source execution and improve offline retrieval. Base-owned helpers now live in `sources/`, retrieval evaluations in `checks/queries.yaml`, and optional app scripts in `clients/`. Update declarations and reinstall managed harness hooks, review the resulting execution plan, and renew trust before collecting. Stored evidence remains readable without re-collection.

### Changed

- Select a persistent launcher explicitly with `harness print/install --executable` when package-manager PATH entries disagree.
- Reuse status narrative pages for briefing commitments instead of reading and parsing task/project files again.
- Rename base collection helpers from `bin/` to `sources/` and retrieval acceptance to `checks/queries.yaml`.
- Declare single-script uv app clients under `clients:`; hash and disclose their separate execution tree.
- Make CLI context receipt persistence opt-in with `--save-receipt`, keeping ordinary context reads lock-free.
- Surface explicit project next actions, review dates, deadlines, and blockers in the offline brief.
- Improve multi-term lexical ranking and excerpts, reuse Unicode-preserving text analysis, and reduce exact-budget packing work.
- Let retrieval evaluations require answer-bearing excerpts and verified offline reads, beyond URI recall.
- Describe harvested lessons as trace citations rather than a knowledge-quality measure.

### Fixed

- Report exact missing completed dates per enabled event source using the configured collection window, including in briefing attention.
- Reuse task pages for fallback selection and the global learned backlog, and skip a second Markdown parse when rendered headings contain no Learned section.
- Keep conjunctions out of question scoring and select commitment and body excerpts independently so metadata cannot displace the answer.
- Give body-cache manifests an independent 8 MiB bound, retaining the 4,096-entry and 512 MiB content limits.
- Select prompt transcripts through bounded archive metadata and resolve bodies from stored lineage, generation, and turn provenance without changing evidence IDs or deleting history.
- Search project commitments and preserve active handoffs in compact identity context; unify indexed and fallback lesson backlog semantics.
- Compile canonical fragment validation once per process and report hook timeouts without exposing child output.
- Compare GitHub commit bounds as instants, project safe fallback titles for untitled browser visits, and discard RSS stylesheet metadata without fetching it.

- Open Chromium-family browser roots and profile directories through retained no-follow descriptors so linked path components cannot redirect local history or bookmark collection.

## [v5.0.1](https://github.com/fmind/fkf/releases/tag/v5.0.1) - 2026-09-07

### Fixed

- Prevent long-lived MCP stdio servers on Python 3.14 from accumulating timeout callbacks during cancellation polling, eliminating age-correlated CPU and memory growth.
- Let local release verification ignore only uv's exact one-byte `dist/.gitignore` marker while continuing to reject every other unexpected asset.
- Parse RSS, Atom, and OPML with a DTD-rejecting XML parser so valid CDATA and predefined or numeric references retain their text without enabling entity expansion.
- Preserve provider-formatted Gmail recipient names while continuing to derive normalized participant identities from mailbox addresses.
- Keep agent session hooks non-blocking when invoked with terminal standard input.

## [v5.0.0](https://github.com/fmind/fkf/releases/tag/v5.0.0) - 2026-09-07

### Highlights

- Reimplement FKF as one typed Python 3.14 package while preserving the `fkf` command, `fkf: 1` configuration and evidence envelopes, trust digests, rebuildable graph and lexical cache contracts, ranking version 7, offline reads, and bounded read-only MCP surface.
- Publish a wheel and source distribution for `uv tool install fkf` and one-shot `uvx fkf` use, with locked uv development, strict Ruff and ty checks, hermetic branch-coverage tests, PyPI trusted publishing, and GitHub build-provenance attestations.
- Consolidate provider execution behind one direct-argv boundary, package presets and skills as runtime resources, and retain deterministic differential coverage against the final Go implementation.
- Load the MCP SDK only for MCP commands so ordinary CLI startup does not pay for its server and transport stack.
- Replace the Hugo module with a locked, self-contained Zensical documentation build while preserving the published Pages routes.

### Breaking changes

- Replace native release archives, `install.sh`, and the self-replacing `fkf upgrade` command with standard Python packaging. Use `uv tool upgrade fkf` for a persistent uv installation.
- Narrow the existing `?jq=` and `--where` selector spelling to the safe field-path grammar plus optional terminal `| length`; arbitrary jq programs are rejected instead of running an embedded evaluator.

### Upgrade notes

- Existing bases and collected evidence need no migration or re-collection. After installing v5, refresh official helpers, review and renew execution trust, then rebuild derived caches: `fkf config helpers --refresh`, `fkf trust --all`, and `fkf build all`.
- Use a persistent `uv tool install fkf` launcher for harness and schedule integrations. Reserve `uvx` for one-shot commands.

## [v4.0.1](https://github.com/fmind/fkf/releases/tag/v4.0.1) - 2026-09-04

### Fixed

- Make Ruff linting independent of contributor-level configuration and use the correct exception types in the Gmail body helper, restoring the clean four-platform release gate.
- Keep historical agent-session collection available as the append-only store grows by bounding in-window identities before selecting their newest complete generation, while rejecting a partial manifest scan.

## [v4.0.0](https://github.com/fmind/fkf/releases/tag/v4.0.0) - 2026-09-04

### Highlights

- Make every derived-cache check read-only with `build [graph|index|wiki|all] --check`, add selective body-cache pruning by source and age, and report lexical-index integrity alongside graph health.
- Add reviewed Gmail and Calendar body helpers plus four disabled Google Workspace metadata presets, while keeping provider diagnostics private and bounding provider output before it can fill memory or temporary storage.
- Keep append-only agent-session stores collectible without a lifetime generation ceiling, normalize Git commit titles without discarding raw messages, and refresh the compact embedded usage skill.
- Harden installation, upgrade, and release delivery with exact dirty-build version handling, portable macOS installation, a four-platform CI gate, draft-before-attestation publication, and exact-head Pages deployment.
- Rewrite the README around the concrete benefit—owned, inspectable memory shared across coding agents—and tighten the command, source, graph, privacy, and contributor guides.

### Breaking changes

- Remove the documented but ineffective `fkf status --all` flag; bare `status` already performs the complete offline check.
- Emit one `{wiki, projects, records, lint?, ok}` document from bare structured `fkf validate` instead of concatenating several top-level JSON reports.
- Bound `receipt.consulted_bodies` under the requested pack budget and expose the complete count as `consulted_bodies_total`.
- Aggregate text `find --count` output across the selected window, bound text `who` neighbours per relation kind, and make body-prune text output explicit.
- Treat a corrupt lexical cache as a status error. A missing or stale rebuildable cache remains a warning.
- Remove the unused exported `services.StatusRequest.All` field and add build/prune request types plus cache-health and receipt fields. Go source consumers using unkeyed literals or strict JSON decoders must update.

### Fixed

- Preserve body-cache crash consistency and confinement across selective pruning, corrupt or missing cache entries, malformed timestamps, symlinked roots, source/age no-ops, and newest-event restore markers.
- Keep command output inside exact byte budgets, make graph-generation state a published confined URI, and prevent stale or corrupt derived caches from masquerading as current.
- Refuse unsafe response-file-style body arguments, disclose the real body execution policy during trust review, and retain one finite shutdown path for oversized or uncooperative body providers.
- Prevent an equal or newer Git-describe development build from being replaced by an older release, and ensure all release archives carry the README, license, notices, checksums, and build-provenance attestations.

### Upgrade notes

- Existing `fkf: 1` configuration and evidence remain valid; no re-collection or data migration is required.
- Refresh any installed official helpers, review and renew execution trust, then rebuild derived caches: `fkf config helpers --refresh`, `fkf trust --all`, and `fkf build all`.
- Update consumers of bare structured `validate` and the removed `status --all` flag before upgrading automation.

## [v3.0.2](https://github.com/fmind/fkf/releases/tag/v3.0.2) - 2026-09-03

### Fixed

- Keep the cross-process writer-lock test helper alive without triggering Go's deadlock detector, removing a timing-dependent macOS CI failure without changing runtime behavior.

## [v3.0.1](https://github.com/fmind/fkf/releases/tag/v3.0.1) - 2026-09-03

### Fixed

- Make schedule CLI tests select the native fake scheduler and managed-file layout, restoring the hermetic CI contract on macOS without changing runtime behavior.

## [v3.0.0](https://github.com/fmind/fkf/releases/tag/v3.0.0) - 2026-09-03

### Highlights

- Add deterministic `brief`, `day`, `timeline`, `who`, and `eval` workflows, temporal query grammar, declared identity aliases, and compact text and structured retrieval receipts.
- Add digest-bound lexical and constant-time graph caches while keeping durable evidence authoritative, offline reads reproducible, and indexed and fallback retrieval semantically identical.
- Add login-aware opportunistic sync, hourly systemd and launchd scheduling, and idempotent harness integration for Claude Code, Codex, Gemini CLI, Copilot CLI, Antigravity, OpenCode, Grok, Cursor, Kiro, and Cline.
- Add bounded, ignored, manifest-verified body caching with per-source `none`, `cache`, and `sync` policies; first-class meeting-note and local agent-memory sources can prefetch searchable text without copying it into durable evidence.
- Expand and harden the reviewed personal presets, session traces, staged learning workflow, MCP surface, provider pagination, process isolation, trust revalidation, and graph generation consistency.

### Breaking changes

- Every collected record must now project one meaningful, control-free `title`; update custom source schemas and field mappings before the next sync.
- Structured `find` and `context` results omit raw provider records and internal day selections by default; pass `--raw` only when those diagnostic fields are required.

### Upgrade notes

- Existing evidence remains valid and requires no re-collection. Run `fkf build all --base <base>` to create the new derived graph and lexical caches.
- Refresh FKF-owned helpers and harness integrations, review the resulting execution plan, and renew trust before running changed collectors: `fkf config helpers --refresh`, `fkf harness install --all`, then `fkf trust --all`.
- Body caching stays opt-in per source. The default `bodies: none` fetches only on an explicit `read --body`; `cache` retains an explicitly fetched body and `sync` prefetches it after evidence is written.

## [v2.1.0](https://github.com/fmind/fkf/releases/tag/v2.1.0) - 2026-08-30

### Highlights

- Add a dedicated base `tests/` execution tree for source verification hooks, recursively covered by trust and prepended to `PATH` only for `fkf test`; collection and body commands cannot see test fixtures or shadows.
- Report source-hook readiness separately from ordinary `requires:`, disclose `bin/` and `tests/` as distinct trust items, and carry the new layout through init, permissions, schemas, documentation, and bundled skills.
- Preserve v2 compatibility: bases without `tests/` keep their existing trust digest, hooks can still resolve from `bin/`, and an empty optional selection remains a successful 0/0 report. Completion gates should name mandatory sources.

### Fixed

- Restrict repository metadata projected by bundled session, Git, and agent-hook helpers to GitHub remotes, while continuing to strip credentials and reject malformed paths.
- Open Atuin history read-only in batch mode, omit deleted rows and command text, and declare the Git dependency used by the agent-sessions preset.

### Upgrade notes

- A pre-existing base `tests/` directory is now reserved, recursively trust-covered execution material and must contain no symlinks. Move source hooks and their support files there, keep generic repository tests elsewhere, then review and renew trust.

## [v2.0.1](https://github.com/fmind/fkf/releases/tag/v2.0.1) - 2026-08-29

### Fixed

- Make verified release archives the documented v2 installation path and explain that Go's major-version import rules keep the unchanged module path's `go install ...@latest` resolution on v1.

## [v2.0.0](https://github.com/fmind/fkf/releases/tag/v2.0.0) - 2026-08-29

### Highlights

- Add optional, trust-covered source `test:` argv and `fkf test`, with enabled-source defaults, explicit disabled-source selection, bounded timeouts, stable reports, and provider-stderr privacy.
- Publish `graph.meta.json` schema version 2 with separate SHA-256 inputs for events, index, projects, tasks, wiki, and edge-relevant schema semantics, plus a framed aggregate and exact `graph.tsv` output digest.
- Give every bundled shell helper an explicit `.sh` extension and require `.sh` or `.py` when scaffolding a helper, including the interpreter in the generated readiness contract when needed.

### Breaking changes

- Existing derived graph metadata must be rebuilt with `fkf build graph`; collected evidence remains readable and requires no re-collection.
- Base configurations and harness integrations using bundled extensionless helper names must move to the corresponding `.sh` names before refreshing helpers.

## [v1.1.2](https://github.com/fmind/fkf/releases/tag/v1.1.2) - 2026-08-27

### Highlights

- Flatten the documentation sidebar on desktop and mobile so Overview and every guide are peers, while preserving all published routes and enforcing the rendered navigation contract in tests.

## [v1.1.1](https://github.com/fmind/fkf/releases/tag/v1.1.1) - 2026-08-27

### Highlights

- Make failed collection diagnostics actionable with the source, date or window, safe substituted command, neutral working directory, timeout, and exit class while keeping provider stderr and body-derived arguments private.
- Stage release installation beside the destination before an atomic replacement, preserving an existing binary if staging fails; cover every published Linux and macOS architecture tuple hermetically.
- Add a dedicated configuration-schema guide, present every documented agent harness at the same level, refresh vendor hook contracts, simplify root help and contributor instructions, and keep the Overview first in the documentation tree.
- Scope toolchain drift checks to project pins, refresh the embedded usage skill and supported-version policy, and retain a strict, generated, link-checked documentation contract.

## [v1.1.0](https://github.com/fmind/fkf/releases/tag/v1.1.0) - 2026-08-27

### Highlights

- Add `fkf upgrade`, which selects the current platform archive, verifies its published SHA-256 checksum and reported version, and atomically replaces the running executable.
- Send the documentation root directly to the Overview, expose Overview in the navigation, and remove the intermediate "Read the docs" landing page.
- Explain repeated `fkf sync` safety and how coding agents learn from project and wiki content through the read-only MCP server and embedded skills.

## [v1.0.0](https://github.com/fmind/fkf/releases/tag/v1.0.0) - 2026-08-26

The initial Fmind Knowledge Framework release: one Go binary that collects developer activity into an owned, inspectable base of JSON and Markdown, links it as a graph of relative URIs, and gives coding agents a deterministic context pack under a token budget.

### Highlights

- Collect complete daily events and point-in-time indexes from local tools, GitHub, Google Workspace, and Google Cloud presets, plus arbitrary reviewed provider commands.
- Compose each source from direct argv or a trust-digested helper whose shebang selects its interpreter, with curated preset helpers for provider boundaries that need shared pagination, completeness, or privacy handling.
- Declare one root semantic `schema:` with descriptions, cardinality, examples, and relation roles; sources associate those shared fields with provider paths, and stored documents retain the exact schema subset they used.
- Build an open, transcription-only graph: any non-reserved lowercase entity scheme is valid, relation field names are base-defined, and edges come only from declared fields, authored links, tags, and explicit `relations:` frontmatter.
- Read, find, rank, and graph stored knowledge entirely offline; the read-only MCP server cannot collect, write, shell, or fetch bodies.
- Share one base across Claude Code, Codex, Gemini CLI, OpenCode, Copilot CLI, Antigravity, Cursor, Kiro, Cline, and other harnesses through portable Agent Skills and documented hooks.
- Keep execution explicit with a canonical-plan trust digest covering enabled commands, body-bound paths, helper scripts, executable bits, executable search directories, retry, pacing, and collection policy without re-arming on YAML presentation, inherited environment, or retrieval-only metadata.
- Use one explicit `fkf: 1` marker value for strict base configuration and the separate additive evidence envelope.
- Store plain JSON and Markdown in five typed layers, with rebuildable `graph.tsv` and `graph.meta.json` at the base root, strict schemas, bounded and atomic I/O, path confinement, owner-only permissions, and untrusted-content framing.
- Reproduce lexical retrieval under a hard whole-pack budget with a selection receipt that records scores, reasons, counted exclusions, rejected pins, evaluation day, ranking version, and semantic-input digest.
- Ship synthetic demos, personal and team presets, a strict Hugo documentation site, hermetic race-tested Go suites, security scans, reproducible archives, checksums, and build-provenance attestations.

### Supported platforms

Release archives are provided for Linux and macOS on amd64 and arm64. Native Windows is intentionally out of scope; WSL2 uses the Linux archive.
