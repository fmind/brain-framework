---
name: bf-contribute
description: Review, change and release the Brain Framework Python framework while preserving evidence and execution boundaries. Use for this repository, not private-brain upkeep.
license: MIT
---

# bf-contribute

1. Read [AGENTS.md](../../../AGENTS.md) for product constraints, safety boundaries and code ownership. Inspect Git status and the relevant implementation before changing it; preserve staged and unrelated work.
1. Follow [CONTRIBUTING.md](../../../CONTRIBUTING.md#set-up-and-check-a-change) for development commands and verification. Its [test guidance](../../../CONTRIBUTING.md#test-outcomes-and-failures) owns fixtures, workflow checks and documentation updates; example READMEs own runnable steps and expected results.
1. Keep changes within the requested scope. Provider projection, native schedule activation and agent-host installation belong to brain maintenance. Do not run live providers or use private-brain data for contribution tests.
1. Write everyday user commands from inside the brain directory. Reserve `--brain` for selection examples, host configuration and unattended scripts. Prefer short tables and runnable examples; link to the owning contract instead of repeating it. Use one word per concept: relation (never role or relationship), `fields:`, record, `overdue` for late programs and `stale` only for results from a busy cache.
1. Within a major release, configuration and reply schemas only gain optional fields; `tests/test_contract.py` rejects anything else. A breaking change waits for a major release, with manual upgrade steps in the changelog.
1. Report the verified snapshot and evidence boundaries described in [Contributing](../../../CONTRIBUTING.md#release). Commit, push and publish only when authorized; an authorized publication follows the [release checklist](references/release.md).
