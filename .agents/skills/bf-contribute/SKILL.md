---
name: bf-contribute
description: Review, change and release the Brain Framework Python framework while preserving evidence and execution boundaries. Use for this repository, not private-brain upkeep.
license: MIT
---

# bf-contribute

[AGENTS.md](../../../AGENTS.md) holds the direction, invariants, and safety boundaries, and [CONTRIBUTING.md](../../../CONTRIBUTING.md) holds the checks. This skill adds three rules.

1. **Use consistent user vocabulary**: Write everyday user commands from inside the brain directory; reserve `--brain` or `$BRAIN_LOCATION` / `~/brain` for outside-brain selection and unattended scripts. Keep the three-format + 3x3 vocabulary consistent: YAML config (`bf.yaml` `version: 17` with `notes:`, `concurrency:`, `env:`, `defaults: {timeout, window}`, and `loops:` using `when:`, `window:`, `ingest:`, `emit:`; note frontmatter), Markdown notes (`concepts/`, `projects/`, `actions/`, flat `<slug>.md` + universal `<slug>/index.md` bundles with `#id` anchors and `ref`/`source:id`/`bf://` links), convention loops (`sensors/`, `routines/`, `agents/`, `.sh`/`.py`/executable, `stdout` JSONL + `stderr` structured JSONL logs), and JSONL records (`memories/<source>.jsonl`, `usages/<source>.jsonl`, private `logs/<loop>.jsonl` + `logs/events.jsonl`, disposable `.bf/` locks/state). Prefer short tables and runnable examples (`bf build && bf check`, `bf status --format text`, `bf query`, `bf read`, `rg`, `jq`, DuckDB), and link to the owning guide instead of repeating rules.
2. **Test with fake providers only**: Never run live providers or use private-brain data in contribution tests; use the fake providers in `tests/conftest.py`. Provider credentials, OS timers, and agent-host installation belong to brain maintenance, not this repository.
3. **Release only with authorization**: Commit, push, and publish only when authorized. An authorized release follows the [release checklist](references/release.md).
