---
name: fkf-maintain
description: "Maintain an FKF base: inspect refresh plans, source health, generated indexes and knowledge hygiene."
license: MIT
---

# fkf-maintain

Select the authorized base and read its local maintenance instructions. Keep provider execution, software upgrades, source changes and durable-evidence deletion as distinct operations.

1. Run `fkf status --base PATH` and `fkf update --base PATH --dry-run`. Review due sources and time windows before a first refresh. Execute `fkf update --base PATH` only within the task or standing collection authority. Automatic progress comes only from successful `update` captures. Manual collections do not advance it, and the first automatic run uses the configured lookback. A failed source remains due; inspect that source and repair its cause before retrying.
1. Use `fkf build --base PATH --if-stale` to repair generated retrieval state. Run `fkf validate --base PATH` and the base’s `fkf eval --base PATH` cases after knowledge changes. Separate stale provider data from an index fault.
1. Review the active task's Resume section, project next actions, broken authored references and superseded wiki guidance. Apply verified, authorized edits with fkf-learn; preserve contradictions and exact evidence.
1. Upgrade FKF or replace source scripts only as a reviewed software change. Test adapters with fake providers, then run the base's recovery and retrieval checks. Do not infer fresh provider data from successful tests.
1. Bound or rotate operational logs using the base's policy. Rebuild disposable indexes as needed. Never automatically purge records, originals, task inputs or outputs. Before any durable deletion, prepare a specific inventory and verify recovery within explicit owner authority.

Scheduling belongs to the base: a native timer invokes `fkf update` with an explicit base and provider environment. Inspect the next run, prevent overlap and distinguish configured, enabled and observed runs. Do not install global harness access or a schedule merely because a retrieval result suggests it.

## Set up a schedule

Help the owner select a native scheduler and cadence, then write a small service/timer, launchd plist or team job definition into the base’s `configs/`. Invoke `fkf update --base ABSOLUTE_PATH` directly or through the base’s existing task runner. Set an explicit working directory, bounded runtime, private logs and missed-run behavior. Validate the definition using the native tool and prove an invocation with `update --dry-run` in the intended environment. Activate recurring provider calls only within the owner’s authorized scope; report generated, enabled and observed states separately.

The base owns and edits these definitions. FKF supplies this setup workflow and the refresh command, not a scheduler engine, persistent registration database or another command surface for every operating system.
