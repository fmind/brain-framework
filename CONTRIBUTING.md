# Contributing

Start with [AGENTS.md](AGENTS.md) and [bf-contribute](.agents/skills/bf-contribute/SKILL.md). Keep changes small, preserve the three-format split (YAML for config, pure JSON for records, Markdown for humans), and use synthetic evidence. Discuss changes that enlarge the product before implementing them, as [GOVERNANCE.md](GOVERNANCE.md#propose-a-change) describes.

## Start from a useful outcome

You can contribute without extending the core:

- **A missed query or validation gap:** provide a small fictional brain, the `bf query` / `bf read` / `bf validate` / `bf review` command, expected refs, and observed result. Leave private records out.
- **An onboarding obstacle:** describe the exact step, BF version, operating system, terminal-agent host, and assistance needed. A simpler explanation or command may be the whole fix.
- **A source sensor:** name the recurring question it answers, selected scope, output fields (`schemas/<source>.json` when applicable), and fake-provider tests for complete success and failure (`examples/sensors/` keeps at most 5 canonical stdlib examples).
- **An experience report:** share a useful outcome or why you stopped using BF through the [usage form](https://github.com/fmind/brain-framework/issues/new?template=usage.yml). Include repeated use and maintenance effort when observed; label estimates.

Support stays in [repository issues](https://github.com/fmind/brain-framework/issues): open one to ask for help or to start contributing.

For a public demonstration, follow the [four-tool walkthrough](examples/context-hub/README.md): ingest the four fixtures (`workspace`, `jira`, `github`, `gcloud`), compile the project note (`bf build`), query `memories/**/*.json` with `bf query` (`rg`, `jq`, `duckdb`), then ingest the updated Jira fixture and show the project flagged with `newer_evidence` in `bf review`. Publish real pilot results only with the relevant permission and privacy review.

## Set up and check a change

From the checkout:

```bash
mise trust
mise install --locked
mise run install
uv run --locked bf --help
mise run format
mise run all
```

`mise install --locked` installs the pinned, provenance-checked tools from `mise.lock`, whose lockfile revision 4 needs mise 2026.11.4 or later; tasks never install tools themselves (`task.run_auto_install = false`). `install` syncs locked dependencies and installs Git hooks. Run `format` deliberately after editing; it rewrites source files, so preserve unrelated work and inspect its diff. `all` checks, tests (including runnable examples), and builds distributions without rewriting source files. Formatting drift fails the gate; fix it with `format`, then rerun the failed check. Tasks use `uv run --locked`, so an outdated `uv.lock` fails instead of being rewritten; run `uv lock` deliberately.

Use a focused check while editing, then run the full gate. Modules live in `src/bf/`, and each module's first docstring line states its concern:

| Changed area                | Files                                                       | Focused check                                                                                                             | What to verify                                                                                                           |
| --------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Storage and config          | `storage.py`, `config.py`, `models.py`                      | `uv run --locked pytest -q tests/test_storage_and_config.py`                                                              | `O_NOFOLLOW` containment, `.bf/` locks, safe YAML 1.2 loader, and `DEFAULT_BRAIN_PATH` brain selection.                  |
| Markdown and `attr_list`    | `markdown.py`                                               | `uv run --locked pytest -q tests/test_markdown.py`                                                                        | OKF frontmatter, Python-Markdown `extra`/`toc`/`admonition`/`sane_lists`, keyed `attrs`, `bf://` links, and tasks.       |
| Records and JSON Schemas    | `records.py`, `schemas.py`                                  | `uv run --locked pytest -q tests/test_records.py`                                                                         | Pure JSON `memories/<source>/*.json`, `.forgotten`, removal guard, `logs/events.json`, and `schemas/*.json`.             |
| Build, validate, and review | `engine.py`                                                 | `uv run --locked pytest -q tests/test_validate_and_review.py`                                                             | `memories/{concepts,projects,actions}/*.json`, `bf action`, link/anchor/cross-brain validation, and `bf review`.         |
| Query, eval, jobs, & MCP    | `query.py`, `jobs.py`, `watch.py`, `mcp.py`, `cli.py`       | `uv run --locked pytest -q tests/test_query_and_eval.py tests/test_jobs_and_watch.py tests/test_mcp.py tests/test_cli.py` | `bf read`, `bf query` (`rg`/`jq`/sandboxed `duckdb`), `bf eval`, `bf run`/`watch`, `bf mcp`, and exit codes `0/1/2/130`. |
| Examples and sensors        | `examples/`, `scripts/benchmark_scale.py`                   | `uv run --locked pytest -q tests/test_examples.py`                                                                        | `context-hub`, `brain`, 5 stdlib sensors on fake providers, and benchmark smoke test.                                    |
| Documentation               | `src/bf/guides/`, `docs/*.md`, `zensical.toml`, `README.md` | `mise run check:docs`, `mise run check:links`, and `uv run --locked pytest -q tests/test_docs.py tests/test_readme.py`    | Strict build, rendered anchors, single-source guide wraps (`--8<--`), redirects, and repository/README links.            |
| Module size                 | `src/bf/*.py`                                               | `mise run check:size`                                                                                                     | Keeps total lines across `src/bf/*.py` `<= 4,500` and each file `<= 650`.                                                |
| Schemas                     | `schemas.py`                                                | `mise run generate:schema` and `mise run check:schema`                                                                    | Generated `docs/{bf,record,concepts,logs,evals}.schema.json` match `schemas.py` and `tests/contract/`.                   |
| Dependencies                | `pyproject.toml`, `uv.lock`                                 | `mise run generate:notices` and `mise run check:notices`                                                                  | `THIRD_PARTY_NOTICES.md` and the site's license copies match the synced environment.                                     |

## Test outcomes and failures

Tests belong in `tests/`; runnable brains and stdlib sensors live under `examples/`. Pytest validates disposable copies without an LLM. Use synthetic fixtures and the fake providers in `tests/conftest.py`; tests run in UTC with neutral terminal settings, isolate `HOME`, configuration, and state, and unset `BF_BRAIN` and `DEFAULT_BRAIN_PATH`. Give each subprocess a timeout of at most 120 seconds: a test still running after 300 seconds ends the whole run. Do not use private brains or live credentials.

For a sensor change, test a valid response and a realistic failure such as a nonzero provider exit or symlink refusal. Confirm that failure writes nothing to stdout and diagnostics contain no provider payload text. Keep the 95% branch-coverage floor and all safety checks.

Update the owning docs, guides, `/bf` skill (`src/bf/skills/bf/SKILL.md`), starter (`src/bf/starter/`), and examples with public behavior. Regenerate schemas when `bf.yaml` or starter JSON Schemas change, and regenerate notices when dependencies change. Record user-visible outcomes under `Unreleased` in `CHANGELOG.md`; change versions and tags only for an authorized release.

### Change an example sensor

`examples/sensors/` holds up to 5 stdlib scripts users copy into their brains and pipe into `bf ingest <source>` or run via `bf.yaml` `jobs:`:

- **Portable**: Python 3.11+ standard library only, an executable `#!/usr/bin/env python3` entry point, and provider CLIs invoked with literal argv lists, never a shell; see [Operations](docs/operations.md).
- **Bounded and closed on failure**: bound provider output, time, and pagination. A failure, overflow, or symlink exits nonzero with nothing on stdout and one sanitized stderr line.
- **Schema-compliant records**: emit JSON arrays, `{"records": [...]}`, or JSONL matching `schemas/record.json`.

### Keep the contract

`tests/contract/` holds the published JSON Schemas of the current major release (`bf`, `record`, `concepts`, `logs`, `evals`), and `tests/test_contract.py` compares every generated `docs/*.schema.json` with them from the writer's side: every file that validates must keep validating in minor and patch releases. `bf.*` Python modules are internal and may change in any release.

Any breaking change to CLI commands/flags, `bf.yaml`, stored file formats, link syntax, or published JSON Schemas ships in a major release with no compatibility layer. That release refreshes `tests/contract/` from `docs/*.schema.json` and lists manual upgrade steps under `### Breaking changes` in `CHANGELOG.md` (or queues them under [next major](GOVERNANCE.md#next-major)).

## Write documentation people can follow

Keep tutorials focused on one working result, task guides on a practical goal, concepts on why the model works, and reference pages on exact contracts ([Diátaxis](https://diataxis.fr/start-here/)):

- **Guides show the main path first**: open with what the reader achieves, write short sentences with one idea each, and link to the exact rule.
- **Reference and guides stay single-source**: Six canonical guides live in `src/bf/guides/` (`index`, `getting-started`, `authoring`, `retrieval`, `operations`, `reference`) and wrap cleanly into flat `docs/*.md` via `--8<--`.
- **Preserve published URLs**: redirect retired documentation paths (including legacy `docs/*` URLs) in `zensical.toml`. `tests/test_readme.py` checks the README's deep links, and `tests/test_docs.py` checks `zensical.toml` navigation and `llms.txt` descriptions.

## Submit a pull request

- **One topic**: each pull request covers one topic under a [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) title.
- **A linear history**: `main` keeps no merge commits, so a pull request is rebased or squashed onto it.
- **The template is the checklist**: complete the test plan in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md).
- **Best-effort review**: one maintainer reviews every pull request; [GOVERNANCE.md](GOVERNANCE.md) says who decides and how.
- **Same license in and out**: you contribute under the project's [MIT license](LICENSE), with no contributor license agreement.

## Read check output

```bash
MISE_TASK_QUIET=false mise run check:docs
mise run test -vv
mise run report:coverage
```

## Maintain the shared gate

Keep `mise.toml` and workflow steps declarative: one command per step, short command arrays, and native flags. A step whose command is a pipeline sets `shell: bash` for pipefail (`tests/test_release.py`). `all` runs `check`, `test`, and `build` in sequence, matching `.github/workflows/verify.yml` across Linux x64, Linux arm64, and macOS arm64.

`test:package` (`scripts/test_package.py`) installs the wheel and source distribution outside the checkout and verifies `bf init`, `bf ingest`, `bf build`, `bf validate`, `bf eval`, `bf read`, and `bf review`. `benchmark` (`scripts/benchmark_scale.py`) times pure JSON ingestion, note compilation, validation, and review under `$XDG_CACHE_HOME/bf-benchmark`. `check:schema` compares `docs/{bf,record,concepts,logs,evals}.schema.json` in memory; `check:notices` verifies `THIRD_PARTY_NOTICES.md` and `docs/third-party-notices.md`; and `check:changelog` requires one nonempty `CHANGELOG.md` section for the version in `pyproject.toml`.

## Release

CI deploys documentation from `main` only after all verification jobs pass. Tag-triggered CD publishes the tested Linux x64 package artifacts after the same gate. Commit, push, and publish only when authorized, following the [release checklist](.agents/skills/bf-contribute/references/release.md) (`scripts/release.py`). See [SECURITY.md](SECURITY.md) for private vulnerability reports and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
