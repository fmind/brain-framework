# Contributing

Start with [AGENTS.md](AGENTS.md) and [bf-contribute](.agents/skills/bf-contribute/SKILL.md). Keep changes small, preserve offline retrieval and file-based knowledge, and use synthetic evidence. Discuss changes that enlarge the product before implementing them.

## Start from a useful outcome

You can contribute without extending the core:

- **A missed question:** provide a small fictional brain, the question, expected refs and observed result. Preserve the difficulty, such as evidence split across tools or conflicting statuses; leave private records out.
- **An onboarding obstacle:** describe the exact step, BF version, operating system, terminal-agent host and assistance needed. A simpler explanation or command may be the whole fix.
- **A source adapter:** name the recurring question it answers, selected scope, shared-field mappings and fake-provider tests for complete success and failure. Keep provider access in the existing sensor pattern.
- **An experience report:** share a useful outcome or why you stopped using BF through the [usage form](https://github.com/fmind/brain-framework/issues/new?template=usage.yml). Include repeated use and maintenance effort when observed; label estimates.

The [team pilot](docs/docs/team.md#evaluate-a-pilot) compares the current workflow, the same curated files accessed directly and BF retrieval. Present task counts, failures, versions and measurement limits with any claimed improvement. A passing fixture, installation, download or star is not adoption evidence. Keep support in repository issues until actual needs justify another channel.

For a public demonstration, follow the [four-tool walkthrough](docs/docs/context-hub.md): collect the four fixtures, show their shared project relation, record the launch conclusion, then change the Jira fixture and show the project flagged with `newer_evidence`. It is a reproducible fictional demonstration, not a customer case study. Publish real pilot results only with the relevant permission and privacy review.

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

`mise install --locked` installs the pinned, provenance-checked tools from `mise.lock`, whose lockfile revision 3 needs mise 2026.9.16 or later; tasks never install tools themselves (`task.run_auto_install = false`). `install` syncs locked dependencies and installs Git hooks. Run `format` deliberately after editing; it rewrites source files, so preserve unrelated work and inspect its diff. `all` checks, tests (including runnable examples) and builds distributions without rewriting source files. Formatting drift fails the gate; fix it with `format`, then rerun the failed check. Tasks use `uv run --locked`, so an outdated `uv.lock` fails instead of being rewritten; run `uv lock` deliberately. Validation still writes ignored caches, coverage, documentation and build output.

Exercise checkout code on a disposable brain with `uv run --locked bf COMMAND --brain BRAIN` from the checkout, such as `uv run --locked bf validate --brain /tmp/demo`, or `uv run --locked --project CHECKOUT bf COMMAND` inside the brain. `mise run test:watch` reruns tests after edits; `mise run docs:watch` serves the documentation.

Use a focused check while editing, then run the full gate:

| Changed area                   | Focused check                                                                            | What to verify                                                                                          |
| ------------------------------ | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Search                         | `uv run --locked pytest -q tests/test_search.py`                                         | Results and realistic failure cases.                                                                    |
| Markdown parsing               | `uv run --locked pytest -q tests/test_markdown.py`                                       | Heading slugs and anchors, claim footnotes, note leads and located link errors.                         |
| Search cache                   | `uv run --locked pytest -q tests/test_cache.py tests/test_cache_version.py`              | Refreshes match a full build; new cache output bumps [`index.SCHEMA`](#version-the-search-cache).       |
| Limits and usage               | `uv run --locked pytest -q tests/test_bounds.py tests/test_usage.py`                     | Documented limits hold at their edges; usage rotation stays within 1 MiB.                               |
| Retrieval quality              | `uv run --locked pytest -q tests/test_retrieval_example.py`                              | Questions still find the expected source, rank and answer fragment; see [cases](#add-a-retrieval-case). |
| Documentation                  | `mise run check:docs` and `mise run check:links`                                         | Strict build, rendered anchors and local repository links with their anchors.                           |
| Getting started, first sensor  | `uv run --locked pytest -q tests/test_guides.py`                                         | Commands return the documented replies; the test applies each other block as its prose says.            |
| Walkthroughs and examples      | `uv run --locked pytest -q tests/test_example_walkthroughs.py tests/test_context_hub.py` | README walkthroughs and the four-tool page run on disposable copies and print their stated results.     |
| Configuration model or reply   | `mise run generate:schema`                                                               | Generated `docs/*.schema.json` match their loaders and replies; test valid and invalid inputs.          |
| Watch dashboard                | `mise run generate:screenshot`                                                           | `docs/assets/watch.svg` shows the current dashboard; regenerate after dashboard or Rich changes.        |
| Dependencies                   | `mise run generate:notices`                                                              | `THIRD_PARTY_NOTICES.md` and the site's license copies match the synced environment.                    |
| Indexing, retrieval or storage | `mise run benchmark` before and after                                                    | Comparable timings with valid, complete results.                                                        |

### Add a retrieval case

For example, if a query finds the wrong project's budget, add that question and both competing project notes to the [retrieval example](examples/retrieval/README.md), with synthetic evidence. Preserve the question's useful difficulty, such as competing answers, a scope, an expected rank or missing evidence; add a short expected answer fragment with its exact source ref and keep the distractors. Reproduce the miss with the retrieval test before changing ranking and rerun it after the fix. Update the documented and tested case count and `mrr` with each new case, and never change an expectation merely to make a regression pass. A passing retrieval case checks that case, not source truth or arbitrary answer quality. Evaluating a real brain requires authorization; work on a copy and report only aggregate results.

Keep parser errors, process cancellation, pagination mechanics and evaluator failure handling in `tests/`, and curated question-to-evidence cases beside their runnable example. Do not add model grading, another evaluator or a new suite format: the [suite reference](docs/docs/checks.md#suite-reference) owns the fields.

## Test outcomes and failures

Tests belong in `tests/`; runnable brains and their question-to-evidence cases live together under `examples/`. Pytest validates and evaluates disposable copies. Both technical and retrieval checks run without an LLM. Use synthetic fixtures and the fake providers in `tests/conftest.py`; tests run in UTC with neutral terminal settings, isolate HOME, configuration and state and unset `BF_BRAIN`. A test of local dates chooses its zone in a subprocess. Reserve real POSIX tools for process-boundary tests and give each subprocess a timeout of at most 120 seconds: a test still running after 300 seconds ends the whole run. Do not use private brains or live credentials.

For a sensor change, test a valid response and a realistic failure such as an incomplete provider page. Confirm that failure leaves saved evidence intact and diagnostics contain no provider text. Keep the 95% branch-coverage floor and all existing safety checks.

Markdown complexity regressions compare fourfold input growth with an eightfold CPU-time bound, below quadratic growth (sixteenfold). Each adversarial case is measured separately after warmup, using the minimum of three samples at each size; runner scheduling delays do not count. When changing these checks, verify that a temporary quadratic parser variant fails. Process cancellation tests inject signals both during startup and while running, then poll for termination with a bounded deadline; a successful signal call or CLI exit alone does not prove descendants ended.

For onboarding or workflow changes, follow the actual commands in a disposable brain with isolated configuration and state: save a decision, search its reason, read its ref, validate and evaluate. Show the expected result beside the example. Keep the README focused on first use, `docs/` on user contracts and `src/bf/skills/` on agent procedures. Check host discovery separately from installing the skills with `bf skills DIR`.

Update the owning docs, skills and examples with public behavior. An adapter change needs a fake-provider test and an entry in `examples/sensors/README.md`. Regenerate the schemas when configuration models or replies change and the notices when dependencies change. Record user-visible outcomes under `Unreleased` in `CHANGELOG.md`; change versions and tags only for an authorized release.

### Change an example program

`examples/sensors/`, `examples/routines/` and `examples/hooks/` hold scripts users copy into their brains. `tests/test_adapters_*.py` runs each one as a process with fake providers and fake `bf` and `git` executables. Their READMEs tell users what each script collects or prints and its limits; these rules keep the scripts safe to copy:

- **Portable**: Python 3.11+ standard library only (`tests/test_adapters_compatibility.py`), an executable `#!/usr/bin/env python3` entry point, and provider CLIs, `git` and `bf` called with literal argv, never a shell. Provider CLIs own credentials, and sensors start them and `git` from `/`: `gws` loads a `.env` file from its working directory or any parent, so one in the brain could switch its account. The session hook runs `git` in the working directory whose repository it looks up. Scripts never read credential files.
- **Bounded and closed on failure**: bound provider output, time and pagination. A failure, overflow or incomplete page exits nonzero with nothing on stdout and one content-free stderr line, such as the limit reached, a root-relative path or a generic sentence for provider errors. Reject malformed continuation tokens and duplicate identities, so a partial catalog never passes as a complete snapshot.
- **Within BF's record bounds**: a sensor maps selected provider fields explicitly, keeps raw payloads out of text and attributes and never prints `fields`. It normalizes what BF would reject, such as multi-line titles, invalid URLs and links, too many links or unencoded identity paths, so one item never fails a whole collection; highlights instead refuses an export that breaks them.
- **Deterministic routines**: read the brain only through `bf read`, `bf search` and `bf status`, print one OKF action or nothing, follow every `next_offset` and fail before writing on `problems`, `stale` or a stalled continuation. Never edit notes, link from the action or create checkbox tasks; name items by `uri` or ref in code spans no backtick can close.
- **Silent hooks**: read-only and offline, within one time budget per event. Any failure, malformed event, unusable reply or unknown repository prints nothing and exits 0. Print a few hundred bytes of authored-note titles and refs; count records, never quote them; accept a stale reply only when its sole problem is a stale identity cache, and then say the index is refreshing.

### Keep the contract

`tests/contract/` holds the configuration and reply schemas of the current major release, and `tests/test_contract.py` compares every generated `docs/*.schema.json` with them. A minor release may only add optional fields, widen what files accept and narrow what replies return:

- **Configuration** (`bf.yaml`, the registry and eval suites) is compared from the writer's side: every file that loads must keep loading. Removing a field, changing its type, narrowing accepted values or a bound, requiring a new key, or adding or widening a `oneOf` alternative fails the gate.
- **Replies** (`search` and `read`) are compared from the reader's side, because clients validate replies with the schema they saved. Removing a field or making it optional, dropping or changing a type, adding an enum or const value or an alternative that fits no released one, or loosening a bound, pattern, format, `items` or `additionalProperties` fails the gate.

Any other change to a bound, pattern, format or condition, such as `not` or `allOf`, fails closed and waits for the next major release. That release refreshes `tests/contract/` from `docs/*.schema.json` and lists the manual upgrade steps in the changelog.

Each major release costs every installation a manual upgrade, so breaking changes ship together, in at most one major release per quarter; v20.0.0 opened the first window. Until the next window, keep each deferred change and its upgrade step on a list and ship minor releases. A new rule that rejects input loading today warns first where it can, through `bf validate` or a `bf:` diagnostic, and fails in the next major release. Input that BF previously misread is a bug: fix it in a patch release.

### Version the search cache

A brain's cache is reused while `index.SCHEMA` is unchanged, so `tests/test_cache_version.py` digests the tables, indexes and rows that a full build of a fixed brain stores. When indexing output or the cache layout changes, that test fails: bump `index.SCHEMA` once per release, unless it already changed since the last one, and record the `PROJECTION` its message prints.

## Write documentation people can follow

Keep tutorials focused on one working result, task guides on a practical goal, concepts on why the model works and reference pages on exact contracts ([Diátaxis](https://diataxis.fr/start-here/)). Put prerequisites before commands and observable results beside them. Label fictional evidence and optional steps; link to the canonical explanation instead of repeating it.

Use descriptive links, one page title, logical heading levels and text alternatives for images ([W3C guidance](https://www.w3.org/WAI/tutorials/page-structure/)). Preserve published documentation-site paths and heading anchors when reorganizing: redirect a merged page to its new section in `zensical.toml`. `tests/test_readme.py` checks the README's deep links and `tests/test_skills.py` the skills' links. Keep all guides in `zensical.toml`, with each guide's `llms.txt` summary matching its front matter `description` (`tests/test_docs.py`); after layout changes or a Zensical upgrade, check narrow screens, light and dark schemes, keyboard navigation, code copying and the search dialog. `docs/javascripts/accessibility.js` patches Zensical's search by structure because its class names are minified: rerun an accessibility audit, such as axe-core, with search open and closed after an upgrade.

## Read check output

Tasks keep failures, warnings, test counts and total coverage visible. Use these commands when more detail is needed:

```bash
MISE_TASK_QUIET=false mise run check:docs
mise run test -vv
mise run report:coverage
```

The first restores command echoes. The second runs verbose tests; `test` runs them in parallel with pytest-xdist, so add `--numprocesses=0` to follow one test's output in order. The third reads the last saved coverage data, which includes the `python -m bf` subprocesses that tests start without replacing their environment; rerun tests first if the source changed. `mise run coverage` runs tests and writes an HTML report. A run that ends with every thread's traceback had a test still running after 300 seconds.

## Maintain the shared gate

Keep `mise.toml` and workflow steps declarative: one command per step, short command arrays and native flags. A step whose command is a pipeline sets `shell: bash` for pipefail (`tests/test_release.py`). The pre-commit hook formats staged files (dprint, import sorting, then Ruff), scans the staged snapshot for secrets and runs `check`. `check:vuln` and `check:scan` fetch advisory data, so `check`, and with it the pre-commit hook, needs network access. The pre-push hook runs `test` when the pushed commits add or modify files; lefthook skips it for a push that only deletes or renames files, pushes a tag or is up to date, so CI tests those. `check` sees the working tree, including unstaged and untracked files, so commit or set aside unrelated work first. Secret checks cover the working tree and recent history; `check:leaks:staged` checks staged content only. Every secret and vulnerability scan, staged and history scans included, skips the ignored generated directories listed in `.gitleaks.toml` and `trivy.yaml`: `.venv`, `dist`, `site`, `htmlcov`, `.agents/tmp` and the tool caches. `check:leaks` therefore fails while Git tracks a file that a `.gitignore` ignores, even a force-added one. Other ignored files, such as `.env` or `.coverage`, are still scanned, so a local scan can report a finding that a CI checkout does not contain.

`all` runs `check`, `test` and `build` in sequence. CI and CD use the same tasks in `.github/workflows/verify.yml`: `check` runs once on Linux x64, then `test` and `build` run on Linux x64, Linux arm64 and macOS arm64. Shared checks include formatting, lint, types, schema, notices, changelog, screenshot, documentation, workflow validation and security scans. Each job lists unexpected checkout changes before rejecting them. Keep every check in the local gate and its corresponding CI job.

`test:package` installs the wheel and source distribution outside the checkout and verifies initialization, search matches, exact file contents, project and task pages, validation, health, evaluation and the diagnostic `bf mcp` gives without the `mcp` extra, then adds the extra and verifies an MCP stdio handshake with search and read. It rejects incomplete replies and incorrect results even when commands exit successfully. The benchmark uses synthetic records, times record writes, cache builds and retrieval, verifies results and validates the final brain outside its timers and has no performance threshold. Its temporary brain lives under `$XDG_CACHE_HOME/bf-benchmark` (default `~/.cache/bf-benchmark`, or `--dir`), because a RAM-backed `/tmp` hides fsync costs; writing the default 20,000-record corpus then takes a few minutes. The owning scripts document the details.

Dependabot runs security updates only: when an alert has a patched release, it opens one grouped `fix(deps)` pull request for GitHub Actions or `uv.lock`, and version updates stay off. `mise run upgrade` takes the newest compatible releases; for an urgent fix, prefer `uv lock --upgrade-package NAME`. After any dependency update, run `mise run generate:notices`; after a Rich update, also run `mise run generate:screenshot`. On the site's notices page, `check:notices` checks only the license copies and Zensical's version rows. It pins by SHA-256 the npm license copies of the libraries Zensical's prebuilt search client bundles but its license file omits (`OMITTED` in `scripts/notices.py`): after a Zensical update, also search the browser bundle for them, such as Preact and usehooks-ts, and for any new library, and keep their rows on `docs/third-party-notices.md` and their copies current. mise tools, dprint plugins and the workflows' mise `version:` are pinned by hand: `mise run check:outdated` reports drift in mise tools and dprint plugins, honoring the seven-day cooldown for plugins, and `mise version` warns when a newer mise release exists. The scheduled security workflow rescans complete Git history, the checkout and all locked Python dependencies, including development tools, runs the online workflow audits and installs the built package with the newest dependencies its ranges allow on Python 3.14 and the newest release (`scripts/test_package.py --unlocked --python VERSION`), even when no code changes trigger CI.

`docs:build` owns the strict documentation build into `site/brain-framework/`; `check:docs` calls it before checking rendered links. Zensical owns `.cache/`. `check:schema` compares all five generated schema documents (three configuration formats and the `search`/`read` reply schemas) in memory without modifying files or using the documentation cache. `generate:schema` regenerates their checked-in copies under `docs/`. `check:notices` compares `THIRD_PARTY_NOTICES.md`, the site notices page and its license copies with the synced environment and fails on an unreviewed dependency or vendored license; `generate:notices` rewrites them except the pinned npm license copies, and a missing or changed copy fails both tasks. `check:screenshot` renders the watch dashboard over the fictional `examples/watch` history and fails when `docs/assets/watch.svg` differs, including after a Rich upgrade; `generate:screenshot` rewrites it. `check:changelog` requires exactly one nonempty `CHANGELOG.md` section for the version in `pyproject.toml`, which CD publishes as the release notes. `.ignore` keeps generated and duplicated files, such as lockfiles, notices and `tests/contract/`, out of editor search; Ruff, ty and lychee read it too, so never list Python sources or Markdown with local links there.

## Release

CI deploys the shared check job's documentation from current `main` only after all verification jobs pass. Tag-triggered CD publishes the tested Linux x64 package artifacts after the same gate. Report local checks, hosted CI, published artifacts, installed runtime, provider freshness and agent-host integration as separate evidence. Commit, push and publish only when authorized.

Follow the [release checklist](.agents/skills/bf-contribute/references/release.md) only with release authorization. See [SECURITY.md](SECURITY.md) for private vulnerability reports and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
