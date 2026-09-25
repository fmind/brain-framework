# Contributing

Read [AGENTS.md](AGENTS.md) for the core contracts and the local [bf-contribute skill](.agents/skills/bf-contribute/SKILL.md) for the contribution workflow. Keep the implementation small and preserve file-based knowledge, offline search, explicit trusted collection and readable refs. Discuss changes that enlarge the product surface before implementing them.

```bash
mise run install
mise run all
```

Use synthetic fixtures and fake providers. Add tests for useful outcomes and realistic failures. Regenerate `docs/bf.schema.json` after configuration changes and update the relevant documentation. Do not weaken checks or the branch-coverage floor.

For documentation changes, walk through the commands with an isolated registry and a disposable brain, then check rendered links with `mise run check:docs` and repository links with `mise run check:links`. The README explains value and fit; `docs/` owns usage and reference details; `skills/` teaches agents the corresponding workflows. Keep claims consistent across them, including setup, brain selection, collection trust and limits. Do not put private-brain examples or evidence in this repository.

Keep task and workflow definitions declarative: one command per workflow step, short sequential mise command arrays, and native tool flags before custom shell. Do not compress branching or cleanup programs into inline `run` blocks. `format:imports` sorts imports and `format:python` formats them; Git hooks pass staged files to each separately. `check:leaks` scans recent history and the working tree, while `check:leaks:staged` checks exactly the staged changes. Tests isolate HOME, config and state in `tests/conftest.py`; the task sets UTC before Python starts. `test:package` installs both artifacts outside the checkout using locked dependencies, then exercises initialization, retrieval, validation and evaluation.

The scripts each have one caller or purpose: `benchmark_scale.py` measures retrieval, `test_package.py` checks distributions, and `release-notes` plus `verify-release-tag` support CD. Documentation builds into `site/brain-framework/`, matching the public URL path so Lychee can check links and anchors directly. Schema tasks use disposable `.cache/schema-*.json` files and only replace the committed schema after generation succeeds.

CI and CD share `verify.yml`: it runs the four-platform gate and uploads the tested artifacts from Linux x64. CI deploys the documentation artifact only from current `main`; CD publishes the package artifact only from version tags. Builds have read-only permissions and do not use shared tool caches. Publication gets its own narrowly scoped jobs.

Release maintainers follow the [release checklist](.agents/skills/bf-contribute/references/release.md). Publishing requires explicit authority and uses the tag-triggered CD workflow.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
