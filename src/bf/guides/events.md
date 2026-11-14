# Events — triggers, gates, hooks, queue, and watch

Coordinate automated runs across `on:` triggers (schedules, events, Git hooks, daemon streams), pre-execution `gate:` checks, durable queues, and `bf watch`.

## Scheduled and event-triggered runs (`bf update`)

Enabled jobs declaring `on:` schedules (`every:` or `at:` with optional `days:`/`timezone:`) or `event:` subscriptions (`kind`, optional `source`, regex `match`, `cooldown`, `max`, `expires`) run via `bf update`:

```bash
bf update --dry-run
bf update
bf update --job JOB
```

- Always preview due jobs with `--dry-run` before running `bf update` unless authorized to execute.
- `--job JOB` (repeatable) restricts execution to selected jobs.
- `bf update` runs due record jobs concurrently, then scheduled jobs, reactive events, pending wakes, due outbound deliveries, and refreshes `.bf/cache.sqlite3`.

## Conditional `gate:` scripts

Jobs may attach a `gate:` argv command in `bf.yaml`. Before launching the full command or harness, BF runs the bounded gate command; if the gate exits non-zero (for example when no unseen record matched), the job is skipped cleanly as `gated` without recording an error.

## Event hooks (`bf run --hook`)

Jobs with `on: { hook: EVENT }` subscribe to lifecycle hooks such as `pre-commit` or `pre-push`:

```bash
bf run --hook pre-push
```

All enabled jobs declaring that hook execute in name order, receiving any stdin Git provides to the hook. If no job subscribes to the hook name, the command exits cleanly with status 0.

## Durable work queue (`bf queue`)

Prompt runs, follow-up wakes (`bf wake`), and outbound deliveries (`bf post`) persist in `<brain>/.bf/runtime.sqlite3`:

```bash
bf queue
bf queue ID
bf queue ID --retry
bf queue ID --cancel
```

- `bf queue` summarizes active runs, wakes, and pending or unresolved deliveries (without exposing private message bodies).
- `bf queue ID` inspects full durable fields for one delivery (`payload`, `command`, `error`, `state`).
- **Explicit recovery rule**: queue ACK follows durable fan-out. When a harness or send fails in an uncertain state, BF records it as unresolved rather than guessing. Inspect first, then run `--retry` (accepting possible replay side effects) or `--cancel` explicitly.
- Queue state never blocks `bf collect`, `bf search`, or `bf read`.

## Continuous runner (`bf watch`)

Run `bf watch` to run due schedules, stream `on: { daemon: true }` jobs, and dispatch events continuously, or `bf status --watch` to observe local run history strictly read-only:

```bash
bf watch --interval 60 --notify failure
bf watch --job JOB --json
bf status --watch
```

- Configure defaults under `watch:` in `bf.yaml` (`interval`, `poll_interval`, `notifications`, `notification_cooldown`).
- `bf watch` runs only on one explicitly selected brain from an authorized terminal or OS service (`systemd` / `launchd`).
