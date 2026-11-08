---
name: bf-contribute
description: Review, change and release the Brain Framework Python framework while preserving evidence and execution boundaries. Use for this repository, not private-brain upkeep.
license: MIT
---

# bf-contribute

[AGENTS.md](../../../AGENTS.md) holds the direction, invariants and safety boundaries, and [CONTRIBUTING.md](../../../CONTRIBUTING.md) the checks. This skill adds three rules.

1. **Use consistent user vocabulary**: Write everyday user commands from inside the brain directory. Reserve `--brain` for selection examples, host configuration and unattended scripts. Prefer short tables and runnable examples; link to the owning contract instead of repeating it. Use one word per concept: relation (never role or relationship), `fields:`, record, `overdue` for late programs and `stale` only for results from a busy cache.
1. **Test with fake providers only**: Never run live providers or use private-brain data in contribution tests; use the fake providers in `tests/conftest.py`. Provider projection, service installation and agent-host installation belong to brain maintenance, not this repository.
1. **Release only with authorization**: Commit, push and publish only when authorized. An authorized release follows the [release checklist](references/release.md).
