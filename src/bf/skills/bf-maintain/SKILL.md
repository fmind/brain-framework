---
name: bf-maintain
description: Keep a Brain Framework brain (the bf command) healthy and connected — add or fix a sensor or field mapping, write or run a routine or Git hook, collect or backfill a source, run bf update, watch or schedule refreshes, read logs, diagnose overdue, failed or busy collection, recover interrupted writes, fix bf validate problems, upgrade bf, update skills and resolve merge conflicts in a brain. Use when the user says "connect this source", "my brain is out of date", "collection failed", "why is this source overdue", "add a routine", "run it on pre-push", "schedule updates", "upgrade bf", "bf is outdated", "fix my brain" or "resolve the conflict".
license: MIT
compatibility: Requires Brain Framework 18 (the bf command) on Linux or macOS.
---

# bf-maintain

Sensors collect records from providers and routines are deterministic brain programs; both are declared in `bf.yaml` and run with the user's permissions. Diagnose offline first, then execute only with authority that covers the selected programs and scope.

## Select one brain

Work inside the brain directory or pass `--brain PATH`. A brain's `pyproject.toml` and `uv.lock` pin a runtime that installs and runs code the brain supplies: diagnose with the installed `bf`, and run `uv run --project PATH --locked bf ...` only for execution (`collect`, `run`, `update`, `watch`, `schedule`) in a brain the user created or whose `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version` they reviewed, with the same authority as `bf collect`. uv runs an existing `.venv/` as is: before the first pinned run, delete one the user did not create, and never run the pin of a brain whose Git tracks one (`git ls-files .venv` prints files). When the installed major version differs from the one its `uv.lock` names, read as data, report it and ask. `bf collect`, `bf run`, `bf update`, `bf watch` and `bf schedule` act on exactly one brain (`--brain`, then `BF_BRAIN`, then the enclosing brain), never its `brains:` references or other registered brains. A bare `--brain NAME` resolves through the registry first and fails when ambiguous: pass the path.

## Diagnose before executing

```bash
bf status
bf update --dry-run
bf validate
bf eval
```

- `bf status` reports, without running anything, each brain's cache, problems and the freshness and failures of its sources and routines, and `bf status --check` exits 1 when one needs attention: [reading status](references/operations.md#read-bf-status) explains each field.
- Each sensor and routine logs every run to `logs/NAME.log` in the brain, newest last and bounded to 1 MiB. Read it locally and keep provider text out of reports; retrieval never reads `logs/`, which `bf init` keeps out of Git.
- `bf update --dry-run` lists due programs and their windows without running anything, and names the manual programs it would skip.
- `bf validate` lists `problems` as `{file, error}`, each once per file: invalid OKF metadata or identities, broken links, a page link with a `#section`, a record ref in the wrong case, unresolved merge markers, a declared field written at the top level of a note's frontmatter, `?rel=` on a link that is not `bf://`, or an enabled program in `sensors/` or `routines/` that is not an executable regular file (check a disabled one with `test -x sensors/NAME`). Behind a command on PATH, such as `[python3, sensors/NAME.py]`, the first `sensors/` or `routines/` argument must exist.
- Non-failing `warnings` name a source whose record `links` or `url` hold text that is neither an identity nor a URL (fix the sensor, then collect again), a `.write-HEX` temporary an interrupted write left (delete it) and identities differing only by letter case ([warnings](https://fmind.github.io/brain-framework/docs/checks/#warnings)).
- `bf eval` runs the retrieval cases under `evals/`; save a reply and pass it back with `--baseline FILE` after a change to list `regressions`.

`bf collect --dry-run` and `bf run --dry-run` **do run the program**: they skip saving, not execution. Discovery or a dry run grants no authority for live collection.

## Choose the work

| Need                                                                      | Load                                                |
| ------------------------------------------------------------------------- | --------------------------------------------------- |
| Add or change a sensor, its `fields:` mappings, a routine or a Git hook   | [Integrations](references/integrations.md)          |
| Repair or backfill collection, recover writes, watch or schedule, upgrade | [Operations and recovery](references/operations.md) |
| Resolve Git conflicts or competing identities in a brain                  | [Conflict resolution](references/conflicts.md)      |
| Correct authored knowledge or review a decision                           | the `bf-use` skill                                  |

Core commands for that work:

- `bf collect SENSOR` runs one sensor now; `--since`/`--until` bound a backfill (a window ending before now can extend coverage but never makes the source fresh), `--dry-run` previews samples without saving, and `--allow-removal` accepts, for one run, a snapshot that would empty its catalog or remove more than half and more than 10 of its records.
- `bf build --reproject SENSOR` re-applies a sensor's current `fields:` mappings to its stored records without running it (`--dry-run` counts the changes); plain `bf build` recovers interrupted record writes and rebuilds the disposable `.bf/` cache.
- `bf run ROUTINE [ARGS]...` runs one routine now and `bf run --hook EVENT` runs every enabled routine listing that hook; both pass arguments through. A hook reads what Git pipes only when a routine lists it; a direct run reads piped input only with `--stdin`. A Git hook calls it with `exec bf run --hook pre-push -- "$@"`.
- `bf update` runs due sensors, then due routines, then refreshes the cache; `bf watch` does so continuously in a dashboard (`--json` for rows); `bf schedule` writes native scheduler files and activates nothing.
- `bf skills DIR --check` reports whether installed skills match this version; `bf skills DIR` updates the unedited ones and leaves `modified`, `unmanaged` and `newer` folders unchanged ([upgrades](references/operations.md#upgrade-brain-framework)).

For a retrieval miss, save the question as an `evals/` case (a suite starts with the same `version` as `bf.yaml`) before changing the owning note or sensor, and never weaken an assertion to make a case pass.

## Verify the result

Repeat the failed check, validate the brain and search and read the evidence the original question needs. Report what changed, what passed, the remaining coverage gaps and the next step, separating offline checks, fake-provider tests, live collection and observed scheduled runs. Preserve evidence: never delete records, logs or action files to clear an error.

## References

- [references/integrations.md](references/integrations.md): sensors, field mappings, reprojection, routines, hooks and their tests.
- [references/operations.md](references/operations.md): status fields, failure diagnosis, snapshot guard, backfills, recovery, watch, schedules, upgrades and brain instructions.
- [references/conflicts.md](references/conflicts.md): Git merges, competing records and actions, identity spellings.
