# Sensors

A sensor turns a selected source into searchable evidence. This walkthrough collects one fictional product brief for the New website project, then links it to the decision it supports. No provider credentials are needed.

The script prints one JSON array of [records](brain.md#records); Brain Framework validates and saves them by id. A sensor must fail if collection is incomplete: Brain Framework cannot detect pages the script silently omitted.

## Your first sensor

Complete [Getting started](getting-started.md), then work inside `~/brain`. Create a dedicated input directory:

```bash
mkdir -p sensors inputs/website-demo
```

Save this text as `inputs/website-demo/brief.txt`:

```text
The fictional New website brief asks for a clear product explanation before visitors sign up.
```

Save the [local documents sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py) as `sensors/local-documents.py` and review it with your agent. The package does not install example sensors. This script scans only the directory passed to it and bounds file sizes and output.

## Configure a sensor

Add the following to your existing `bf.yaml`. Keep its `version`, `name`, schema and other settings. If `sensors:` already exists, add only the `local-documents` entry beneath it; do not create a duplicate YAML key.

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  local-documents:
    command:
      - uv
      - run
      - --no-project
      - --python
      - "3.14"
      - sensors/local-documents.py
      - website-demo
      - "{{brain}}/inputs/website-demo"
    mode: snapshot
    refresh: 0
```

`website-demo` is the stable label used in record ids. The final argument selects exactly the demo directory. `uv` supplies the interpreter independently of the host's `python3`; it must be on PATH and may obtain Python if it is not already installed. The text-file collection itself needs no provider or network access. `refresh: 0` keeps this sensor manual.

Review the script and selected input directory before executing it. Collection runs with your account permissions; registration is not required.

## Collect and read

```bash
bf collect local-documents --dry-run
bf collect local-documents
bf search "product explanation" --scope memories/local-documents
bf read local-documents:website-demo/brief.txt
bf validate
```

The preview executes the script without saving its output. The real collection stores one record in `memories/local-documents/snapshot.jsonl`. Search returns `ref: local-documents:website-demo/brief.txt`; the exact read includes these selected record fields:

```json
{
  "id": "website-demo/brief.txt",
  "title": "brief.txt",
  "text": "The fictional New website brief asks for a clear product explanation before visitors sign up.\n"
}
```

Link the record from `projects/new-website.md`, beneath the decision:

```markdown
Evidence: [Product brief](local-documents:website-demo/brief.txt).
```

Run `bf validate` to check that the link resolves. If you edit the input file and collect again, the same id updates the saved record; the project link continues to work.

## Sensor settings

The executable is a command on PATH or a `sensors/` executable. Arguments pass directly, without a shell. The placeholders `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}` are replaced once. Directly executed Python examples need an executable bit and a suitable `python3`; the walkthrough uses `uv` instead.

| Setting     | Default  | Meaning                                                         |
| ----------- | -------- | --------------------------------------------------------------- |
| `command`   | required | Executable and arguments.                                       |
| `fields`    | `{}`     | [Schema mappings](schema.md#shared-fields-and-sensor-mappings). |
| `enabled`   | `true`   | Disabled sensors keep their existing records searchable.        |
| `mode`      | `window` | Update returned items, or replace a complete `snapshot`.        |
| `refresh`   | `0`      | Seconds between automatic runs; zero keeps the sensor manual.   |
| `lookback`  | `86400`  | Seconds covered by the first run, or each snapshot run.         |
| `overlap`   | `300`    | Seconds re-read before the previous window's end.               |
| `timeout`   | `300`    | Maximum runtime in seconds.                                     |
| `max_bytes` | 64 MiB   | Maximum stdout size.                                            |

## Any source you can script

A CLI, API, database query or readable export can become a sensor. The script owns authentication, pagination, rate limits and field selection. Prefer provider CLIs for credentials; never print secrets.

For example, a feedback sensor could emit this fictional record linking an observation to the same project:

```json
[{
  "id": "feedback-42",
  "title": "New website feedback",
  "text": "Visitors asked what the product does before opening the signup form.",
  "time": "2026-09-27T12:00:00Z",
  "url": "https://example.com/feedback/42",
  "links": ["bf://brain/projects/new-website.md"]
}]
```

Finish all source pages before printing the array, and fail if any page is missing. Test that failure with a fake provider so partial evidence cannot appear to be a successful collection.

The four [reviewed examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors) cover local documents, local Git history, Google Calendar and Drive folders. Provider integrations need their CLI, authentication and a deliberately selected scope. Keep your copied sensor under review and test it with a fake provider before scheduling it.

## Collect and update

After reviewing the configured programs, preview due work and run an update:

```bash
bf update --dry-run  # list due work; execute nothing
bf update           # run due sensors, then routines
bf status
```

The demo sensor is not due because its `refresh` is zero. Set a nonzero interval only when you want it included in updates.

A sensor is due when it is enabled, `refresh` is nonzero and that interval has elapsed since its last success. Updates follow the [brain selection rules](commands.md) and never execute referenced brains. Failures leave the sensor due, allow other programs to continue and make the command exit 1.

Window sensors resume from their previous window minus `overlap`, catching up at most 30 days. Run state stays in `~/.local/state/bf/`; without it, the next run uses `lookback`. A [CI collector](team.md#collect-in-ci) should retain that state or use a longer lookback.

### Backfills and coverage

`bf status` separates indexed totals from `last_run` counts: added, updated, unchanged and removed. Backfilling an older window does not claim a fresh collection or fill a gap between windows. A window touching existing coverage extends it; a future `--until` never claims coverage beyond the run time.

Runs of the same sensor serialize. Different sensors may run concurrently; record commits and state updates lock briefly. See [process safeguards](limits.md#processes-and-logs) for timeouts, logs and cancellation.

## Define the scope before adding a sensor

Keep these choices beside the script: accounts and folders, stable ids, event and modification times, selected fields, size limits, deletion behavior and fake-provider tests.

| Mode       | Use for                                                                              | What disappears from stored records?              |
| ---------- | ------------------------------------------------------------------------------------ | ------------------------------------------------- |
| `window`   | History: commits, messages, meetings.                                                | Nothing automatically; returned ids are updated.  |
| `snapshot` | A bounded current catalog: folders, contacts, selected documents or a future agenda. | Items absent from a successful complete snapshot. |

An empty snapshot cannot replace a non-empty catalog: a wrong account or missing folder could also look empty. Clear `memories/SOURCE/` only as a deliberate deletion. For mutable window sources, document how far back revisions are re-read.

## Good records

- Keep ids stable across edits and source URLs available for verification.
- Put searchable facts in `title` and `text`; use `attributes` for exact-read details.
- Use event time for `time`, upstream modification time for `attributes.updated`, and `attributes.partial: true` for deliberately incomplete text. Brain Framework supplies `attributes.observed`.
- Link explicit identities; do not infer relationships from similar names.
- Skip noise such as trash, promotions, bots and test runs.

## Routines

Prepare review actions with the [routine guide](routines.md).

## Schedule it

Run updates automatically with [Schedule updates](schedule.md).
