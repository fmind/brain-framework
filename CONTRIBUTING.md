# Contributing

Read [AGENTS.md](AGENTS.md) for the core contracts and the local [fkf-contribute skill](.agents/skills/fkf-contribute/SKILL.md) for the contribution workflow. Keep the implementation small and preserve evidence, offline retrieval, explicit collection, and recovery. Discuss changes that enlarge the product surface before implementing them.

```bash
mise run install
mise run all
```

Use synthetic fixtures and fake providers. Add tests for useful outcomes and realistic failures. Regenerate `docs/fkf.schema.json` after configuration changes and update the relevant documentation. Do not weaken checks or the branch-coverage floor.

Release maintainers follow the [release checklist](.agents/skills/fkf-contribute/references/release.md). Publishing requires explicit authority and uses the tag-triggered CD workflow.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
