# Contributing

Read [AGENTS.md](AGENTS.md) for the core contracts. Keep the implementation small and preserve evidence, offline retrieval, explicit collection, and recovery. Discuss changes that enlarge the product surface before implementing them.

```bash
mise run install
mise run all
```

Use synthetic fixtures and fake providers. Add tests for useful outcomes and realistic failures. Regenerate `docs/fkf.schema.json` after configuration changes and update the relevant documentation. Do not weaken checks or the branch-coverage floor.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.
