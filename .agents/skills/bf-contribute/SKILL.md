---
name: bf-contribute
description: Review, change and release the Brain Framework Python framework while preserving evidence and execution boundaries. Use for this repository, not private-brain upkeep.
license: MIT
---

# bf-contribute

[AGENTS.md](../../../AGENTS.md) holds the direction, invariants, and safety boundaries, and [CONTRIBUTING.md](../../../CONTRIBUTING.md) holds the checks. This skill adds three rules.

1. **Use consistent user vocabulary**: Write everyday user commands from inside the brain directory; reserve `--brain` or `DEFAULT_BRAIN_PATH` for outside-brain selection and unattended scripts. Keep the three-format vocabulary consistent: YAML config (`bf.yaml` `version: 15` with unified `loops:`, `evals/*.yaml`, frontmatter), pure JSON records (`memories/{concepts,projects,actions}/*.json`, `memories/<source>/*.json`, validated by package-owned core schemas and user `schemas/*.json`; plain JSON line logs in `logs/*.log` and private state/index/cache in `.bf/`), and Markdown notes (`projects/`, `concepts/`, `actions/*/ACTION.md` with Python-Markdown `attr_list` metadata and relative / `source:id` / `bf://` links). Prefer short tables and runnable examples (`bf check`, `bf query`, `bf read`, `rg`, `jq`), and link to the owning guide instead of repeating rules.
2. **Test with fake providers only**: Never run live providers or use private-brain data in contribution tests; use the fake providers in `tests/conftest.py`. Provider credentials, OS timers, and agent-host installation belong to brain maintenance, not this repository.
3. **Release only with authorization**: Commit, push, and publish only when authorized. An authorized release follows the [release checklist](references/release.md).
