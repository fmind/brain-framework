# Events — schedules, gates, hooks, queue, and watch

Coordinate automated runs cleanly across periodic schedules, pre-execution `when:` gates, Git/tool hooks, durable assistant queues, and the `bf watch` runner.

## Scheduled runs (`bf update`)

Configured sensors, routines, and assistants declaring `every:` intervals become due automatically based on stored run history in `.bf/`:

```bash
bf update --dry-run
bf update
bf update --kind sensor --sensor SENSOR
```

- Always preview due schedules with `--dry-run` before executing `bf update` unless authorized to run due programs.
- `--kind sensor|routine|assistant` and `--sensor`/`--routine`/`--assistant` filter which enabled programs run.
- `bf update` runs due sensors, then due routines, then due assistants, and refreshes `.bf/cache.sqlite3`.

## Conditional `when:` gates

Programs may attach a `when:` check command in `bf.yaml`. Before launching the full program (such as an expensive assistant harness), BF runs the bounded gate command; if the gate indicates nothing to do, the main command is skipped cleanly without recording an error.

## Event hooks (`bf run --hook`)

Routines can bind to lifecycle hook names (such as `pre-push`):

```bash
bf run --hook pre-push
```

All enabled routines declaring that hook execute in name order, receiving any stdin Git provides to the hook. If no routine subscribes to the hook name, the hook exits cleanly with status 0.

## Durable work queue (`bf queue`)

Assistant runs, channel wakes, and outgoing deliveries persist in `<brain>/.bf/runtime.sqlite3`:

```bash
bf queue
bf queue ID
bf queue ID --retry
bf queue ID --cancel
```

- `bf queue` summarizes pending, queued, or unresolved deliveries and assistant jobs (without exposing private message bodies).
- `bf queue ID` inspects full durable fields for one entry (`payload`, `prepared`, `command`, `receipt`).
- **Explicit recovery rule**: queue ACK follows durable fan-out. When a harness or channel delivery fails in an uncertain state, BF records it as unresolved rather than guessing. Inspect first, then run `--retry` (accepting possible replay side effects) or `--cancel` explicitly.
- Queue state never blocks `bf collect`, `bf search`, or `bf read`.

## Continuous runner (`bf watch`)

Run `bf watch` to monitor schedules and channels in a loop, or `bf status --watch` to observe local run history strictly read-only without executing programs:

```bash
bf watch --interval 60 --notify failure
bf watch --json
bf status --watch
```

- Configure defaults under `watch:` in `bf.yaml` (`interval`, `poll_interval`, `notifications`).
- `bf watch` only runs programs for one explicitly selected brain; run it from an authorized local terminal or OS service unit (`systemd` / `launchd`).
