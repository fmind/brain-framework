# Implement a selected integration

Use once a source is selected and implementation is authorized, carrying forward its question, scope, fields, exclusions and needed freshness. Test with fictional data before any provider or model runs.

## Sensors

Start from a reviewed [sensor example](https://github.com/fmind/brain-framework/tree/main/examples/sensors), copied from the release matching `bf --version`, and the [sensor contract](https://fmind.github.io/brain-framework/docs/sensors/). Put the executable in `sensors/` and declare it disabled first, with an explicit account, folder or channel scope:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
fields:
  # Beside the existing entries, such as depends-on: bf.yaml has one fields: key.
  kind: { description: Common kind of source item., type: string, cardinality: one }
sensors:
  docs:
    command: [sensors/local-documents.py, team, "{{home}}/Documents/team"]
    mode: snapshot
    enabled: false # confirm the folder before enabling
    fields:
      kind: { value: document }
```

- `command` is direct argv without a shell; arguments accept `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}`.
- `mode: window` (the default) collects a time window each run; `mode: snapshot` returns the whole catalog, and records it no longer returns are removed. Know how the provider reports changes, pagination, partial content and deletions.
- `priority: low` quiets a high-volume feed on period and home pages.

A sensor's `fields:` maps each shared field, declared once under the top-level `fields:` of `bf.yaml`, from a JSON pointer into its output (`path`, such as `/attributes/repository_refs`) or a constant (`value`); the sensor prints `attributes`, never `fields`. Emit explicit namespaced identities only (`person:email/address`, `repo:github.com/owner/name`, lowercase as the reviewed examples write them); `links` and `url` take identities or URLs. Keep the upstream modification time in `attributes.updated`, which flags linking notes for review, and mark incomplete text with `attributes.partial`. See [good records](https://fmind.github.io/brain-framework/docs/sensors/#good-records) and the [limits](https://fmind.github.io/brain-framework/docs/limits/).

Test with a fake provider: a complete response and a realistic failure, such as an incomplete page, which must leave saved evidence untouched and provider text out of diagnostics. Then, within live authority, set `enabled: true`, preview and collect once:

```bash
bf collect docs --dry-run
bf collect docs
bf read memories/docs
```

Recurring collection needs its own authority: add `refresh: 3600` (seconds) only once it is granted.

After changing a sensor's `fields:` mappings, `bf build --reproject SENSOR --dry-run` counts the records that would change and `bf build --reproject SENSOR` re-applies the mappings to stored `attributes`; a value never kept there needs a new collection.

## Routines

A routine is a deterministic brain program under `routines:`, such as a review generator, a backup or a Git hook check: it uses no model and makes no decision, and may read the brain through `bf`, since every program receives `BF_BRAIN`. Start from the reviewed [routine example](https://github.com/fmind/brain-framework/tree/main/examples/routines) and the [routine contract](https://fmind.github.io/brain-framework/docs/routines/).

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
  check-links:
    command: [routines/check-links.sh]
    hooks: [pre-push]
```

- `output: log` (the default) keeps stdout in `logs/NAME.log`. `output: action` turns non-empty OKF Markdown (`type: action`, `status: draft`) into `actions/YYYY-MM-DD_NAME-XXXXXXXX/ACTION.md`, at most one a day; empty output means nothing to review.
- Without `refresh`, a routine runs only on demand; `hooks` lists the events `bf run --hook EVENT` runs it for. A failing routine writes no action and reports `failed` in `bf status`.

Put `--` before arguments that start with a dash. A direct run reads piped input only with `--stdin`; `--hook` reads what Git pipes, so close stdin with `< /dev/null` when running a hook by hand. `--dry-run` runs the routine but writes no action, returning its Markdown as `text`.

```bash
bf run weekly-review --dry-run
bf run weekly-review
bf run --hook pre-push -- origin https://example.test/repo.git < /dev/null
```

To run hook routines from Git, make `.git/hooks/pre-push` executable with:

```sh
#!/bin/sh
exec bf run --hook pre-push --brain "$HOME/brain" -- "$@"
```

Git passes the remote and URL as arguments and the pushed refs on stdin (within 10 seconds and 1 MiB); a non-zero exit from any routine blocks the push. In a pinned brain the user created or reviewed, start the line with `exec uv run --project "$HOME/brain" --locked bf` so the routines' own `bf` calls use the pin too.

## Assistants and channels

An assistant runs the owner's harness over `prompts/NAME.md`; its records are model output, not verified facts. Declare it under `assistants:` with `prompt`, argv `command`, optional JSON Pointer `text`, and reviewed tool permissions. No schedule is implicit. Add named `schedules` only within recurring-work authority; inputs, outputs and triggers are separate assistant-level fields. Follow the [assistant guide](https://fmind.github.io/brain-framework/docs/prompts/) for the complete configuration.

1. Test with `command: [cat]` and no `text`, then switch to the approved harness, such as `[claude, --print, --output-format, json, --tools, "Read,Grep,Glob,Bash", --allowedTools, "Bash(bf read *),Bash(bf search *)", --permission-mode, dontAsk]` with `text: /result`, and run `bf run NAME --stdin` once. `--dry-run` still executes the harness; it skips records and delivery, not tool effects or provider cost.
1. Put provider code in brain-owned `channels/` scripts. Declare only supported `send`, `read` and `stream` capabilities. A stream owns push/long-poll/fast-poll transport and acknowledges its provider only after BF's durable ACK. Batch reads are complete bounded windows, never partial pagination. [Channel contracts](https://fmind.github.io/brain-framework/docs/channels/) own envelopes and limits.
1. Authenticate and admit senders in the adapter, preserve their identity and constrain destinations. Prompt text must not grant extra authority to arbitrary remote input. Outputs deliver the saved answer; remove model-owned duplicate sends.
1. Use `outputs: [{reply: {}}]` for replies to the origin; every such input needs send support. Use named output objects for independent destinations and `when` filters. Notifications are send-only channels. Include proactive destinations as inputs if later replies must reach the assistant.
1. Run one `bf watch` owner for persistent streams; finite `bf update` does not start them. Stop competing legacy consumers before cutover and preserve their unresolved accepted work. Restart watch after subscription/transport changes.
1. Inspect `bf queue` and the exact job/delivery before retrying uncertain effects. A delivery retry sends the saved answer; a job without a saved answer can repeat model tool effects. Only idempotent assistant deliveries retry by themselves, up to 20 times; a standalone `bf channel send` waits for `bf queue ID --retry`. Preserve the durable local runtime queue, including after configuration removal; it is not the `.bf/` cache. Resolve its work before moving or renaming a brain: a drained queue follows at the next `bf queue`, while unresolved work refuses execution until the brain is restored.

## Verify and hand off

Keep fake-provider tests beside the program and questions with expected refs in `evals/`. Run `bf validate` and `bf eval`, then read the evidence for the original question. A fake-provider test does not prove live access: report configured, tested and collected sources separately. Ongoing refresh follows [operations](operations.md).
