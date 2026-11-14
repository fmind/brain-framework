# Jobs — records, maps, routines, prompts, and messaging

Brain Framework declares every executable program under unified `jobs:` in `bf.yaml` (`defaults:` holds shared `harness`, `timezone`, and `post`). Each job runs only on an explicit execution command on one selected brain.

## Subprocess isolation and safety

Jobs and `gate:` checks run only via `bf collect`, `bf run`, `bf post`, `bf queue --retry`, `bf update`, or `bf watch`:

- Subprocesses launch configured `argv` lists with no shell (`shell=False`), clean allowlisted environment variables (no startup-injection env vars), bounded execution time (`timeout`) and output (`max_bytes`), and full process-group termination on timeout or failure.
- Failed record jobs never commit partial evidence. Snapshot catalogs that empty or drop most records require `--allow-removal` so truncated upstream responses never erase saved evidence.
- Job logs live under `logs/NAME.log`, bounded and excluded from retrieval. Execution state lives inside `.bf/` (`.bf/state/<job>/` and `.bf/runtime.sqlite3`).
- Placeholders expand in `argv`: `{{brain}}`, `{{home}}`, `{{state}}`, `{{run}}`, `{{start}}`, `{{end}}`, `{{cursor}}`, `{{context}}`, plus `{{prompt}}` (on harnesses), `{{text}}`/`{{target}}`/`{{thread}}` (on `send:`), and whole-argument `{{lines:FILE}}`.

## Record jobs & `map:` (`sensors/` + `bf collect`)

Record jobs (`emit: records`, `mode: window|cursor|snapshot`) store validated JSON records in `memories/<source>/<sha256>.json`. Pair any JSON/JSONL CLI with `map:` (`items`, `id`, `@type`, `title`, `text`, `url`, `created`, `updated`, `relations`, `attributes`, `cursor`) or write standalone scripts emitting normalized records:

```bash
bf collect JOB
bf collect JOB --since 7d --dry-run
bf collect JOB --input sample.json
```

- `--dry-run` executes the command (and optional `map:`) without writing `memories/`.
- `--input FILE` (or `-` for stdin) validates saved output offline against the job's configuration (`map:` included) without running the command.
- `--allow-removal` accepts a snapshot run that clears or shrinks most of a catalog.
- Never edit `memories/` manually.

## Deterministic routines (`routines/` + `bf run`)

Jobs with `run:` and `emit: log|action` run deterministic scripts on intervals, wall-clock `at:`, events, or Git hooks:

```bash
bf run JOB -- ARGS
bf run JOB --stdin
bf run JOB --dry-run
```

Pass `--` before routine arguments starting with `-` so CLI option parsing stays unambiguous.

## Prompt jobs (`prompts/` + `bf run`)

Prompt jobs set `prompt: prompts/NAME.md` and invoke an external `harness` (or `defaults.harness`) with `emit: message|action|log`, optional JSON `extract: /pointer` extraction, and `NO_REPLY` quiet suppression:

```bash
bf run JOB --stdin
```

BF prepares prompts and invokes the external harness; BF runs no model itself. Runs, wakes, and deliveries are queued durably in `.bf/runtime.sqlite3` without blocking retrieval.

## Inbound & outbound messaging (`bf post` + `bf wake`)

Jobs with `emit: messages` ingest inbound JSON batches or daemon streams (`{id, sender, conversation, time, text}`); jobs with `send:` deliver outbound messages (target via `post:` or `defaults.post`):

```bash
bf post "Message text" --to JOB [--thread CONVERSATION] [--run RUN_ID]
bf wake 10m --job JOB --reason "Follow up" [--run RUN_ID]
```

`bf doctor` checks host binaries (`requires.bins`), env vars (`requires.env`), executables, and paths offline before running jobs.
