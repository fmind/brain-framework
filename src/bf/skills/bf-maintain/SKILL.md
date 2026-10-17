---
name: bf-maintain
description: Keep a Brain Framework brain (bf) healthy — add or fix sensors, field mappings, routines, assistants and Git hooks; collect, update, watch or schedule sources; diagnose overdue or failed collection; recover writes, fix bf validate problems, upgrade bf and resolve brain merge conflicts. Use for "connect this source", "collection failed", "my brain is out of date" or "upgrade bf".
license: MIT
compatibility: Requires Brain Framework 18 (the bf command) on Linux or macOS.
---

# bf-maintain

Sensors collect records from providers, routines are deterministic brain programs and assistants run the user's agent harness; all are declared in `bf.yaml` and run with the user's permissions. Diagnose offline first, then execute only with authority that covers the selected programs and scope.

## Select one brain and its runtime

- `bf collect`, `bf run`, `bf channel`, `bf queue`, `bf update`, `bf watch` and `bf schedule` act on exactly one brain: `--brain`, then `BF_BRAIN`, then the enclosing brain, never its `brains:` references or other registered brains. Pass a path: a bare name resolves through the registry first and fails when ambiguous.
- Diagnose with the installed `bf`, whose major version must match this skill's `compatibility`. When `bf status` shows a `runtime.pinned`, the version the brain's `uv.lock` names, whose major version differs from `runtime.installed`, report it and ask.
- A pin installs and runs code the brain supplies. Run `uv run --project PATH --locked bf ...` only for execution (`collect`, `run`, `channel`, delivery retries, `update`, `watch`, `schedule`), with the same authority as `bf collect`, in a brain the user created or whose `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version` they reviewed. uv runs an existing `.venv/` as is: delete one the user did not create before the first pinned run, and never run the pin of a brain whose Git tracks one (`git ls-files .venv` prints files) ([pinned runtime](https://fmind.github.io/brain-framework/docs/upgrades/#pin-a-brains-runtime)).

## Diagnose before executing

```bash
bf status
bf update --dry-run
bf validate
bf eval
```

- `bf status` runs nothing and reports each brain's cache, problems, runtime and program health; `--check` exits 1 when one needs attention ([reading status](references/operations.md#read-bf-status)). Each program logs its runs to `logs/NAME.log`: read it locally and keep provider text out of reports.
- `bf update --dry-run` lists due programs and their windows without running anything.
- `bf validate` lists `problems` as `{file, error}`, such as invalid OKF metadata, broken links, merge markers, `assistants.NAME.prompt: PATH is not a regular file` or an enabled program that is not an executable regular file (check a disabled one with `test -x sensors/NAME`).
- Its non-failing [warnings](https://fmind.github.io/brain-framework/docs/checks/#warnings) name sensors emitting `links` or `url` that are neither identities nor URLs, `.write-HEX` temporaries to delete and identities differing only by case.
- `bf eval` runs the retrieval cases under `evals/`; `--baseline FILE` with a saved reply lists `regressions`.

`bf collect --dry-run` and `bf run --dry-run` **do run the program**: they skip saving, not execution. Discovery or a dry run grants no authority for live collection.

## Choose the work

| Need                                                                                             | Load                                                |
| ------------------------------------------------------------------------------------------------ | --------------------------------------------------- |
| Add or change a sensor, its `fields:` mappings, a routine, an assistant, a channel or a Git hook | [Integrations](references/integrations.md)          |
| Read status, repair or backfill collection, recover writes, watch, schedule, upgrade             | [Operations and recovery](references/operations.md) |
| Resolve Git conflicts or competing identities in a brain                                         | [Conflict resolution](references/conflicts.md)      |
| Correct authored knowledge or review a decision                                                  | the `bf-use` skill                                  |

Core commands:

- `bf collect SENSOR` runs one sensor now; `--since`/`--until` bound a backfill, and `--allow-removal` accepts a snapshot removal for one run.
- `bf build` recovers interrupted record writes and rebuilds the disposable `.bf/` cache; `--reproject SENSOR` re-applies current `fields:` mappings without running the sensor.
- `bf run ROUTINE [ARGS]...` runs one routine, `bf run ASSISTANT` one assistant (`--stdin` passes piped text as its input) and `bf run --hook EVENT` every routine listing that hook.
- `bf update` runs due sensors, routines and assistants, then refreshes the cache; `bf watch` does so continuously (`--json` for rows); `bf schedule` writes native scheduler files and activates nothing.

For a retrieval miss, save the question as an `evals/` case before changing the owning note or sensor, and never weaken an assertion to make a case pass.

## Verify the result

Repeat the failed check, validate the brain and read the evidence the original question needs. Report what changed, what passed, the coverage gaps and the next step, separating offline checks, fake-provider tests, live collection and observed scheduled runs. Never delete evidence to clear an error.

Inspect assistant obligations with `bf queue`; read the [integration guide](references/integrations.md#assistants-and-channels) before configuring channel admission, routing or recovery. Never delete durable runtime state as cache cleanup.
