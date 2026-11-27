---
name: bf-contribute
description: Review, change and release the Brain Framework Python framework while preserving evidence and execution boundaries. Use for this repository, not private-brain upkeep.
license: MIT
metadata:
  internal: true
---

# bf-contribute

[AGENTS.md](../../../AGENTS.md) holds the direction, invariants, and safety boundaries, and [CONTRIBUTING.md](../../../CONTRIBUTING.md) holds the checks. This skill adds three rules.

1. **Use consistent user vocabulary**: Write everyday user commands from inside the brain directory; reserve `--brain`, `BF_BRAIN`, or `~/brain` for outside-brain selection and unattended scripts. Follow the `version: 19` 3x3 contract in [docs/reference.md](../../../docs/reference.md) (Notes: `concepts/`/`projects/`/`actions/` -> `memories/_notes.jsonl`; Loops: `sensors/`/`routines/`/`agents/` with explicit `ingest:`/`timeout:` and per-loop `env_from:`; Records: `memories/<source>.jsonl`, private `logs/<loop>.jsonl`, `.bf/cache/index.sqlite3`, `.bf/state/`). Prefer short tables and runnable examples, and link to the owning guide instead of repeating rules.
2. **Test with fake providers only**: Never run live providers or use private-brain data in contribution tests; use the fake providers in `tests/conftest.py`. Provider credentials, OS timers, and agent-host installation belong to brain maintenance, not this repository.
3. **Release only with authorization**: Commit, push, and publish only when authorized. An authorized release follows the [release checklist](references/release.md).
