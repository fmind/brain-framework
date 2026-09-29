# Contributing

Start with [AGENTS.md](AGENTS.md) and [bf-contribute](.agents/skills/bf-contribute/SKILL.md). Keep changes small, preserve offline retrieval and file-based knowledge, and use synthetic evidence. Discuss changes that enlarge the product before implementing them.

## Start from a useful outcome

You can contribute without extending the core:

- **A missed question:** provide a small fictional brain, the question, expected refs and observed result. Preserve the difficulty, such as evidence split across tools or conflicting statuses; leave private records out.
- **An onboarding obstacle:** describe the exact step, BF version, operating system, terminal-agent host and assistance needed. A simpler explanation or command may be the whole fix.
- **A source adapter:** name the recurring question it answers, selected scope, shared-field mappings and fake-provider tests for complete success and failure. Keep provider access in the existing sensor pattern.
- **An experience report:** share a useful outcome or why you stopped using BF through the [usage form](https://github.com/fmind/brain-framework/issues/new?template=usage.yml). Include repeated use and maintenance effort when observed; label estimates.

The [team pilot](docs/docs/pilot.md) compares the current workflow, the same curated files accessed directly and BF retrieval. Present task counts, failures, versions and measurement limits with any claimed improvement. A passing fixture, installation, download or star is not adoption evidence. Keep support in repository issues until actual needs justify another channel.

For a public demonstration, start with the [four-tool example](examples/context-hub/README.md): run collection, show the shared project relationships, read all four sources and explain the unresolved launch blocker. It is a reproducible fictional demonstration, not a customer case study. Publish real pilot results only with the relevant permission and privacy review.

## Set up and check a change

From the checkout:

```bash
mise run install
uv run bf --help
mise run format
mise run all
```

`install` syncs locked dependencies and installs Git hooks. Run `format` deliberately after editing; it rewrites source files, so preserve unrelated work and inspect its diff. `all` checks, tests (including runnable examples) and builds distributions without rewriting source files. Formatting drift fails the gate; fix it with `format`, then rerun the failed check. Tasks use `uv run --locked`, so a stale `uv.lock` fails instead of being rewritten; run `uv lock` deliberately. Validation still writes ignored caches, coverage, documentation and build output.

Use `uv run bf` to exercise checkout code on a disposable brain. `mise run test:watch` reruns tests after edits; `mise run docs:watch` serves the documentation.

Use a focused check while editing, then run the full gate:

| Changed area                   | Focused check                                                                 | What to verify                                                                                   |
| ------------------------------ | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Search                         | `uv run pytest -q tests/test_search.py`                                       | Results and realistic failure cases.                                                             |
| Retrieval quality              | [Run the retrieval example's test](examples/retrieval/README.md#run-the-test) | Questions still find the expected source, rank and answer fragment.                              |
| Documentation                  | `mise run check:docs` and `mise run check:links`                              | Strict build, rendered anchors and local repository links.                                       |
| Getting started, first sensor  | `uv run pytest -q tests/test_guides.py`                                       | Commands return the documented replies; the test applies each other block as its prose says.     |
| Configuration model or reply   | `mise run generate:schema`                                                    | Generated `docs/*.schema.json` match their loaders and replies; test valid and invalid inputs.   |
| Watch dashboard                | `mise run generate:screenshot`                                                | `docs/assets/watch.svg` shows the current dashboard; regenerate after dashboard or Rich changes. |
| Dependencies                   | `mise run generate:notices`                                                   | `THIRD_PARTY_NOTICES.md` and the site's license copies match the synced environment.             |
| Indexing, retrieval or storage | `mise run benchmark` before and after                                         | Comparable timings with valid, complete results.                                                 |

For example, if a query finds the wrong project's budget, add that question and both competing project notes to the [retrieval example](examples/retrieval/README.md). Reproduce the miss before changing ranking and rerun its test after the fix; keep the expected answer tied to its source. A passing retrieval case checks that case, not source truth or arbitrary answer quality. Evaluating a real brain requires authorization; work on a copy and report only aggregate results.

## Test outcomes and failures

Tests belong in `tests/`; runnable brains and their question-to-evidence cases live together under `examples/`. Pytest validates and evaluates disposable copies. Both technical and retrieval checks run without an LLM. Use synthetic fixtures and the fake providers in `tests/conftest.py`; tests run in UTC with neutral terminal settings, isolate HOME, configuration and state and unset `BF_BRAIN`. A test of local dates chooses its zone in a subprocess. Reserve real POSIX tools for process-boundary tests. Do not use private brains or live credentials.

For a sensor change, test a valid response and a realistic failure such as an incomplete provider page. Confirm that failure leaves saved evidence intact and diagnostics contain no provider text. Keep the 95% branch-coverage floor and all existing safety checks.

For onboarding or workflow changes, follow the actual commands in a disposable brain with isolated configuration and state: save a decision, search its reason, read its ref, validate and evaluate. Show the expected result beside the example. Keep the README focused on first use, `docs/` on user contracts and `skills/` on agent procedures. Check host discovery separately from copying a skill or installing the package.

Update the owning docs, skills and examples with public behavior. An adapter change needs a fake-provider test and an entry in `examples/sensors/README.md`. Regenerate the schema when configuration models change and the notices when dependencies change. Record user-visible outcomes under `Unreleased` in `CHANGELOG.md`; change versions and tags only for an authorized release.

## Write documentation people can follow

Keep tutorials focused on one working result, task guides on a practical goal, concepts on why the model works and reference pages on exact contracts ([Diátaxis](https://diataxis.fr/start-here/)). Put prerequisites before commands and observable results beside them. Label fictional evidence and optional steps; link to the canonical explanation instead of repeating it.

Use descriptive links, one page title, logical heading levels and text alternatives for images ([W3C guidance](https://www.w3.org/WAI/tutorials/page-structure/)). Preserve published documentation-site paths and heading anchors when reorganizing; `tests/test_readme.py` checks the README's deep links. Keep all guides in `zensical.toml`; after layout changes, check narrow screens, keyboard navigation, code copying and search when enabled.

## Read check output

Tasks keep failures, warnings, test counts and total coverage visible. Use these commands when more detail is needed:

```bash
MISE_TASK_QUIET=false mise run check:docs
mise run test -vv
mise run report:coverage
```

The first restores command echoes. The second runs verbose tests. The third reads the last saved coverage data; rerun tests first if the source changed. `mise run coverage` runs tests and writes an HTML report.

## Maintain the shared gate

Keep `mise.toml` and workflow steps declarative: one command per step, short command arrays and native flags. The pre-commit hook formats staged files (dprint, import sorting, then Ruff), scans the staged snapshot for secrets and runs `check`; the pre-push hook runs `test`. `check` sees the working tree, including unstaged and untracked files, so commit or set aside unrelated work first. Secret checks cover the working tree and recent history; `check:leaks:staged` checks staged content only. Local secret and vulnerability scans skip ignored generated directories (`.gitleaks.toml`, `trivy.yaml`), so they see what a CI checkout sees.

`all` runs `check`, `test` and `build` in sequence. CI and CD use the same tasks in `.github/workflows/verify.yml`: `check` runs once on Linux x64, then `test` and `build` run on Linux and macOS, x64 and arm64. Shared checks include formatting, lint, types, schema, notices, screenshot, documentation, workflow validation and security scans. Each job lists unexpected checkout changes before rejecting them. Keep every check in the local gate and its corresponding CI job.

`test:package` installs the wheel and source distribution outside the checkout and verifies initialization, search matches, exact file contents, project and task pages, validation, health, evaluation and an MCP stdio handshake with search and read. It rejects incomplete replies and incorrect results even when commands exit successfully. The benchmark uses synthetic records, times record writes, cache builds and retrieval, verifies results and validates the final brain outside its timers and has no performance threshold. Its temporary brain lives under `$XDG_CACHE_HOME/bf-benchmark` (default `~/.cache/bf-benchmark`, or `--dir`), because a RAM-backed `/tmp` hides fsync costs; writing the default 20,000-record corpus then takes a few minutes. The owning scripts document the details.

Dependabot updates GitHub Actions and `uv.lock` weekly, with a seven-day cooldown and separate major-version proposals. `mise run upgrade` takes the newest compatible releases without that delay; for an urgent fix, prefer `uv lock --upgrade-package NAME`. After any dependency update, run `mise run generate:notices`; after a Rich update, also run `mise run generate:screenshot`. mise tools, dprint plugins and the workflows' mise `version:` are pinned by hand: `mise run check:outdated` reports drift in mise tools and dprint plugins, honoring the seven-day cooldown for plugins, and `mise version` warns when a newer mise release exists. The scheduled security workflow rescans complete Git history, the checkout and all locked Python dependencies, including development tools, runs the online workflow audits and installs the built package with the newest dependencies its ranges allow (`scripts/test_package.py --unlocked`), even when no code changes trigger CI.

`docs:build` owns the strict documentation build into `site/brain-framework/`; `check:docs` calls it before checking rendered links. Zensical owns `.cache/`. `check:schema` compares all five generated schema documents (three configuration formats and the `search`/`read` reply schemas) in memory without modifying files or using the documentation cache. `generate:schema` regenerates their checked-in copies under `docs/`. `check:notices` compares `THIRD_PARTY_NOTICES.md`, the site notices page and its license copies with the synced environment and fails on an unreviewed dependency or vendored license; `generate:notices` rewrites them. `check:screenshot` renders the watch dashboard over the fictional `examples/watch` history and fails when `docs/assets/watch.svg` differs, including after a Rich upgrade; `generate:screenshot` rewrites it.

## Release

CI deploys the shared check job's documentation from current `main` only after all verification jobs pass. Tag-triggered CD publishes the tested Linux x64 package artifacts after the same gate. Report local checks, hosted CI, published artifacts, installed runtime, provider freshness and agent-host integration as separate evidence. Commit, push and publish only when authorized.

Follow the [release checklist](.agents/skills/bf-contribute/references/release.md) only with release authorization. See [SECURITY.md](SECURITY.md) for private vulnerability reports and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
