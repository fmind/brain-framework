---
name: bf-maintain
description: Keep a Brain Framework brain (bf) healthy — write or fix sensors, field mappings, routines, assistants and Git hooks; collect, update, watch or schedule them; diagnose failed or overdue collection, recover writes, fix bf validate problems, upgrade bf and resolve merge conflicts. Use for "add a sensor for this source", "collection failed", "my brain is out of date" or "upgrade bf"; choosing which sources to connect belongs to bf-setup.
license: MIT
compatibility: Requires Brain Framework 22 (the bf command) on Linux or macOS.
---

# bf-maintain

Sensors collect provider records, routines run deterministic brain tasks and assistants invoke the user's agent harness; all are declared in `bf.yaml` and run with user permissions. Diagnose offline first, then execute only within explicit authority for that brain and scope.

## Brain selection and pinned runtime

- Execution commands (`bf collect`, `bf run`, `bf channel`, `bf queue`, `bf update`, `bf watch`) act on one brain (`--brain PATH`, `BF_BRAIN`, or enclosing brain — never `brains:` or registry ambiguity). Always pass `--brain PATH` in unattended jobs.
- Diagnose with the installed `bf` (matching `compatibility`). Ask when `bf status` shows a `runtime.pinned` major mismatch.
- Run `uv run --project PATH --locked bf ...` **only** for execution (`collect`, `run`, `channel`, delivery retries, `update`, `watch`) in a brain the user created or whose `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version` they reviewed — never with an unreviewed or Git-tracked `.venv/` ([pinned runtime](https://fmind.github.io/brain-framework/docs/upgrades/#pin-a-brains-runtime)).

## Diagnose before executing

```bash
bf status
bf update --dry-run
bf validate
bf eval
```

- `bf status` runs nothing and reports caches, `problems`, `runtime`, uniform `attention` (`{kind, name, ...}`), sources, programs and `messaging`; `--check` exits 1 when unhealthy ([status](references/operations.md#read-bf-status)). Read `logs/NAME.log` locally without leaking provider output.
- `bf update --dry-run` plans due work offline (`bf collect --dry-run` and `bf run --dry-run` **do execute** the program without saving).
- `bf validate` reports `problems` (`{file, error}`) and non-failing [warnings](https://fmind.github.io/brain-framework/docs/checks/#warnings); `bf eval` checks `evals/` (`--baseline FILE` flags `regressions`).

## Choose the work

| Need                                                                                 | Load                                                |
| ------------------------------------------------------------------------------------ | --------------------------------------------------- |
| Add or update sensors (`_sensor.py`), `fields:`, routines (`_bf.py`) or assistants   | [Integrations](references/integrations.md)          |
| Read status, repair or backfill collection, recover writes, watch, schedule, upgrade | [Operations and recovery](references/operations.md) |
| Resolve Git merge conflicts or competing identities                                  | [Conflict resolution](references/conflicts.md)      |
| Reconcile `bf read review`, update notes or run turnkey consolidation                | the `bf-use` skill                                  |

Core commands ([llms.txt](https://fmind.github.io/brain-framework/llms.txt) and `bf COMMAND --help` own contracts):

- `bf collect SENSOR` (`--since`/`--until` backfill, `--allow-removal` one-time snapshot shrink, `--input FILE` offline JSON/JSONL check); `bf forget REF...` (`--dry-run` first) and `expire_after` delete evidence only on explicit user request.
- `bf build` recovers interrupted writes and rebuilds `.bf/`; `--reproject SENSOR` re-applies `fields:` mappings offline.
- `bf run ROUTINE|ASSISTANT [ARGS]...` (`--stdin`, `--hook EVENT`), `bf update`, `bf watch` (`--json`) and `bf queue` ([assistants and channels](references/integrations.md#assistants-and-channels)). Never delete durable runtime queue state as cache cleanup.

Verify by repeating the failed check, running `bf validate` and `bf eval`, and reporting offline checks, fake-provider tests, live runs and scheduled runs separately.
