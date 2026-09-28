---
name: bf-maintain
description: Diagnose and repair Brain Framework collection, routines, retrieval or conflicts; implement selected integrations and operate authorized refresh jobs. Use bf-setup for onboarding and bf-scan for discovery.
license: MIT
compatibility: Requires Brain Framework 14 (the bf command) on Linux or macOS.
metadata:
  version: "14.0.0"
---

# bf-maintain

Start with offline diagnosis in the intended brain directory. Outside it, use `--brain PATH`; check an inherited `BF_BRAIN` before relying on the directory. `update`, `collect`, `watch` and `schedule` act on exactly one brain (`--brain`, `BF_BRAIN` or the enclosing brain you own), never its `brains:` references or all registered brains: registration selects brains for retrieval only. A bare `--brain NAME` resolves through the user's registry first and fails as ambiguous when a related brain claims it elsewhere; pass the path.

## Diagnose before executing

```bash
bf status
bf update --dry-run
bf validate
bf eval
```

`status` reports each brain's `cache` (`ready` or `stale`), sources (`state`, `freshness`, `last_collected`, `window`, `last_run`) and routines (`state`, `last_success`, `action`); a failed program adds `failed`, `error`, `failures` and its private `log`. `update --dry-run` lists due programs and windows without running them. Validation and evaluation check file structure and saved retrieval expectations; inspect failures and incomplete evidence before reporting success. `bf validate` lists `problems` as `{file, error}` objects, at most 200 with `problems_truncated`; fix those and validate again.

`bf collect --dry-run` **does execute provider code** and may write private stderr logs. Live collection, update, routine previews and watch need the user's authority for that scope; recurring execution needs matching authority. Reuse approval already supplied. Discovery alone grants neither.

## Choose the repair

| Need                                                              | Load                                                |
| ----------------------------------------------------------------- | --------------------------------------------------- |
| Implement a selected sensor, mapping or routine                   | [Integrations](references/integrations.md)          |
| Repair collection, backfill, recover a journal, watch or schedule | [Operations and recovery](references/operations.md) |
| Compare `AGENTS.md` with the installed version's instructions     | [Operations and recovery](references/operations.md) |
| Resolve Git conflicts or competing identities                     | [Conflict resolution](references/conflicts.md)      |
| Correct authored knowledge or review a decision                   | `bf-learn`, if installed                            |

For a retrieval miss, save the question as an `evals/` case before changing the owning note or sensor; a new suite starts with `version: 5`. Keep technical regressions in `tests/`. Run `bf eval --path evals/NAME.yaml` for one suite or `bf eval` for all. Every suite is validated before retrieval: a malformed `read` ref or an empty window such as `0d` stops the whole run and names the suite and field. Never substitute empty assertions to make a case pass; see [retrieval cases](https://fmind.github.io/brain-framework/docs/checks/#suite-reference).

Use `bf schema` for the installed brain schema, including `watch` preferences, or `--kind registry|eval` for other configuration formats. These commands are offline and need no selected brain. Editor schemas check structure; runtime validation, evaluation and update planning check their owning semantics.

Review `usage` in `bf status` when diagnosing poor adoption: few searches suggest checking agent access, while many empty searches suggest missing vocabulary or evidence. Counts indicate where to investigate; they do not establish a cause. Inspect review reminders with `bf read projects` and open work with `bf read tasks`; a reminder or recent edit is not verification.

## Verify the result

Repeat the failed check, validate the changed brain, and search/read the evidence needed by the original question. Report what changed, what passed, unresolved coverage and the next step. Distinguish offline checks, fake-provider tests, live collection and observed scheduler runs. Preserve evidence; do not delete records or action artifacts to clear an error.

For first use, choose `bf-setup`; for source recommendations, choose `bf-scan`. Companion skills are installed separately through the [installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md). Neither discovery nor a link to another skill installs or activates an integration.
