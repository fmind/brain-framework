# Contributing

Read [AGENTS.md](AGENTS.md) for the core contracts and the local [bf-contribute skill](.agents/skills/bf-contribute/SKILL.md) for the contribution workflow. Keep the implementation small and preserve file-based knowledge, offline search, explicit trusted collection and readable refs. Discuss changes that enlarge the product surface before implementing them.

```bash
mise run install
mise run all
```

Use synthetic fixtures and fake providers. Add tests for useful outcomes and realistic failures. Regenerate `docs/bf.schema.json` after configuration changes and update the relevant documentation. Do not weaken checks or the branch-coverage floor.

For documentation changes, walk through the commands with an isolated registry and a disposable brain, then check rendered links with `mise run check:docs` and repository links with `mise run check:links`. The README explains value and fit; `docs/` owns usage and reference details; `skills/` teaches agents the corresponding workflows. Keep claims consistent across them, including setup, brain selection, collection trust and limits. Do not put private-brain examples or evidence in this repository.

Release maintainers follow the [release checklist](.agents/skills/bf-contribute/references/release.md). Publishing requires explicit authority and uses the tag-triggered CD workflow.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
