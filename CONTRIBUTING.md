# Contributing

Start with [AGENTS.md](AGENTS.md) and [bf-contribute](.agents/skills/bf-contribute/SKILL.md). Keep changes small, preserve offline retrieval and file-based knowledge, and use synthetic evidence. Discuss changes that enlarge the product before implementing them.

## Set up and check a change

From the checkout:

```bash
mise run install
uv run bf --help
mise run all
```

`install` syncs locked dependencies and installs Git hooks. `all` formats, checks, tests, evaluates retrieval and builds the distributions. Inspect its diff before committing; run mutating checks in an isolated copy when the checkout has unrelated work.

Use a focused check while editing, then run the full gate:

| Changed area                   | Focused check                                    | What to verify                                                      |
| ------------------------------ | ------------------------------------------------ | ------------------------------------------------------------------- |
| Search                         | `TZ=UTC uv run pytest -q tests/test_search.py`   | Results and realistic failure cases.                                |
| Retrieval quality              | `mise run eval`                                  | Questions still find the expected source, rank and answer fragment. |
| Documentation                  | `mise run check:docs` and `mise run check:links` | Strict build, rendered anchors and local repository links.          |
| Configuration model            | `mise run generate:schema`                       | Generated `docs/bf.schema.json` matches the loader.                 |
| Indexing, retrieval or storage | `mise run benchmark` before and after            | Comparable timings with valid, complete results.                    |

For example, if a query finds the wrong project's budget, add that question and both competing project notes to [evals/](evals/README.md). Reproduce the miss before changing ranking; keep the expected answer tied to its source. A passing retrieval case checks that case, not source truth or arbitrary answer quality.

## Test outcomes and failures

Technical tests belong in `tests/`; question-to-evidence cases belong in `evals/`. Both run without an LLM. Use the fake providers in `tests/conftest.py`; tests isolate HOME, configuration and state. Do not use private brains or live credentials.

For a sensor change, test a valid response and a realistic failure such as an incomplete provider page. Confirm that failure leaves saved evidence intact and diagnostics contain no provider text. Keep the 85% branch-coverage floor and all existing safety checks.

For a documentation change, follow the actual commands in a disposable brain with isolated configuration and state: save a decision, search its reason, read its ref, validate and evaluate. Show the expected result beside the example. Keep the README focused on first use, `docs/` on user contracts and `skills/` on agent procedures.

## Read check output

Tasks keep failures, warnings, test counts and total coverage visible. Use these commands when more detail is needed:

```bash
MISE_TASK_QUIET=false mise run check:docs
mise run test -vv
mise run report:coverage
```

The first restores command echoes. The second runs verbose tests. The third reads the last saved coverage data; rerun tests first if the source changed. `mise run coverage` runs tests and writes an HTML report.

## Maintain the shared gate

Keep `mise.toml` and workflow steps declarative: one command per step, short command arrays and native flags. Git hooks run import sorting and Python formatting separately, with the staged file selection. Secret checks cover the working tree and recent history; `check:leaks:staged` checks staged content only.

`test:package` installs the wheel and source distribution outside the checkout and exercises initialization, retrieval, validation and evaluation. The benchmark uses synthetic records, verifies results outside its timers and has no performance threshold. The owning scripts document the details.

Docs build into `site/brain-framework/`. Zensical owns `.cache/`; schema generation uses separate `.schema-cache/` files so a clean docs build cannot remove them.

## Release

CI and CD share `.github/workflows/verify.yml`, which runs the gate on Linux and macOS, x64 and arm64. CI deploys docs from current `main`; tag-triggered CD publishes the tested package artifacts. Local success is separate from hosted CI and publication.

Follow the [release checklist](.agents/skills/bf-contribute/references/release.md) only with release authorization. See [SECURITY.md](SECURITY.md) for private vulnerability reports and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
