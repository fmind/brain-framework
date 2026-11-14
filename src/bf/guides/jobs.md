# Jobs — sensors, routines, prompts, and channels

Brain Framework separates collection, deterministic automation, agent prompts, and messaging into explicit `bf.yaml` programs executed only by execution commands on one selected brain.

## Subprocess isolation and safety

Sensors, routines, assistants, channels, and `when:` gates run only via `bf collect`, `bf run`, `bf channel`, `bf queue --retry`, `bf update`, or `bf watch`:

- Subprocesses launch configured `argv` lists with no shell (`shell=False`), clean startup environment variables, bounded execution time and output size, and full process-group termination on timeout or failure.
- Failed sensor runs never commit partial evidence. Snapshot catalogs that empty or drop most records require `--allow-removal` so truncated upstream responses never erase saved evidence.
- Program logs live under `logs/`, bounded and excluded from retrieval. Execution state lives inside `.bf/` (`.bf/state/<job>/` and `.bf/runtime.sqlite3`).

## Sensors (`sensors/` + `bf collect`)

Sensors are standalone scripts returning normalized JSON records with provenance (`updated`, `observed`, `url`, `title`, `text`), optional `@type`, explicit `relations` (`scheme:value` or `bf://`), and arbitrary `attributes` metadata:

```bash
bf collect SENSOR
bf collect SENSOR --since 7d --dry-run
bf collect SENSOR --input sample.json
```

- `--dry-run` executes the sensor and previews normalized records without writing `memories/`.
- `--input FILE` (or `-` for stdin) validates saved output offline against the sensor configuration without running the sensor command.
- `--allow-removal` explicitly accepts a snapshot run that clears or shrinks most of a snapshot catalog.

Collected records are written atomically to `memories/<source>/<sha256>.json`. Never edit `memories/` manually.

## Routines (`routines/` + `bf run`)

Routines are deterministic programs (code formatting, sync helpers, verification scripts) declared under `routines:` in `bf.yaml` with `command:` argv lists and optional `hooks:` or `every:` intervals:

```bash
bf run ROUTINE -- ARGS
bf run ROUTINE --stdin
bf run ROUTINE --dry-run
```

Pass `--` before routine arguments starting with `-` so CLI option parsing never confuses routine arguments with `bf` flags.

## Assistants & prompts (`prompts/` + `bf run`)

Assistants pair a configured agent harness `command` with a prompt file under `prompts/` (`{{input}}` template injection and optional `channels:`/`triggers:`/`every:`):

```bash
bf run ASSISTANT --stdin
```

BF prepares prompts and invokes the external harness; BF core performs no model inference itself. Assistant work, deliveries, and wakes are queued durably in `.bf/runtime.sqlite3` without blocking retrieval.

## Channels (`channels/` + `bf channel`)

Channels provide read/send boundaries between external messaging transports and assistants:

```bash
bf channel read CHANNEL --since 24h
bf channel send CHANNEL --stdin
```

- `bf channel read` performs a bounded passive read (`--since` required) and never advances subscription cursors or starts assistants.
- `bf channel send` requires `--stdin` carrying a JSON object (`text` plus optional `id`, `conversation`, `reply_to`) and records durable delivery status in `.bf/runtime.sqlite3`.
