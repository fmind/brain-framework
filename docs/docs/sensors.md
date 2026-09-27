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

The preview executes the script without saving its output. The real collection stores one record under `memories/local-documents/`, in a JSON file keyed by the SHA-256 of its id. Search returns `ref: local-documents:website-demo/brief.txt`; the exact read includes these selected record fields:

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

## Selected highlights

Use the reviewed [highlights sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py) when a selected passage is more useful than importing a whole document. Copy it to `sensors/highlights.py` and the [fictional export](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.json) to `inputs/highlights.json`. This is an explicit portable format: convert exports from reading tools into it before collection. It does not directly parse a Readwise export or fetch the original document.

```json
{
  "version": 1,
  "highlights": [{
    "id": "brief-clarity",
    "title": "New website brief",
    "selection": "Visitors need a clear product explanation before signing up.",
    "annotation": "Review whether our first page explains who the product helps.",
    "source_url": "https://example.com/website-brief",
    "locator": { "page": 2, "section": "Audience" },
    "captured_at": "2026-09-27T12:00:00Z",
    "source_date": "2026-09-25"
  }]
}
```

All fields are required except `annotation` and `source_date`; unknown fields and duplicate JSON keys fail. Keep `id` stable when editing a highlight: 1–128 letters, digits, dots, underscores or hyphens, starting with a letter or digit. `title` is the source document's nonempty single-line title (at most 4096 UTF-8 bytes); `selection` is its exact selected text. `annotation` is your separate interpretation and may be empty. Each selection or annotation is at most 64 KiB. `source_url` is an HTTPS document URL without credentials (at most 8192 bytes); `locator` requires a positive `page` up to 1000000, a nonempty `section` up to 4096 bytes, or both. `captured_at` includes seconds and a timezone; optional `source_date` is the document's stated date in `YYYY-MM-DD`. These are supplied provenance, not independently verified facts.

Merge this configuration into the existing `sensors:` and `schema:` mappings:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  highlights:
    command: [
      uv,
      run,
      --no-project,
      --python,
      "3.14",
      sensors/highlights.py,
      reading,
      "{{brain}}/inputs/highlights.json",
    ]
    mode: snapshot
    refresh: 0
    fields:
      source-document: { path: /url }
schema:
  source-document:
    description: The document containing this selected passage.
    type: identity
    cardinality: one
    relation: true
```

`reading` is the stable export scope label; changing it changes every record id. Use a lowercase label starting with a letter, with only letters, digits or hyphens, up to 64 characters. The sensor reads exactly one regular UTF-8 file, refuses symlinks in the file or its ancestors, and bounds input to 8 MiB and 1000 highlights. It emits at most 16 MiB. Every item is checked before output, so malformed or oversized exports fail without partial records. No source text is truncated and no source URL is contacted.

```bash
bf collect highlights --dry-run
bf collect highlights
bf search "product explanation" --scope memories/highlights
bf read highlights:reading/brief-clarity
bf validate
```

The exact read returns the selection as `record.text`, the parent document as `record.url` and `record.fields.source-document`, and the page/section as `record.attributes.locator`. The capture timestamp becomes `record.time`; `record.attributes.source_date` remains distinct. `record.attributes.annotation` holds the interpretation and is not indexed as source text. Two highlights of the same document keep distinct ids. Link this record from the project decision, then read the passage and location before adopting the annotation as a conclusion.

Reimporting the same ids updates their records. Snapshot mode removes ids absent from a successful nonempty replacement, so always supply the complete selected catalog for this label. An empty export cannot erase an existing catalog. A malformed export preserves all previous records. A fresh import proves the selected local export was read, not that the source is current; retain an [evidence capture](agents.md#decision-workflows) before replacing a revision needed to explain a decision.

## Sensor settings

The executable is a command on PATH or a `sensors/` executable. Arguments pass directly, without a shell. The placeholders `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}` are replaced once. Directly executed Python examples need an executable bit and a suitable `python3`; the walkthrough uses `uv` instead.

| Setting     | Default  | Meaning                                                                       |
| ----------- | -------- | ----------------------------------------------------------------------------- |
| `command`   | required | Executable and arguments.                                                     |
| `fields`    | `{}`     | [Schema mappings](schema.md#shared-fields-and-sensor-mappings).               |
| `enabled`   | `true`   | Disabled sensors keep their existing records searchable.                      |
| `mode`      | `window` | Update returned items, or replace a complete `snapshot`.                      |
| `refresh`   | `0`      | Seconds between automatic runs; zero keeps the sensor manual.                 |
| `lookback`  | `86400`  | Seconds covered by the first run, or each snapshot run.                       |
| `overlap`   | `300`    | Seconds re-read before the previous window's end.                             |
| `reconcile` | omitted  | Optional periodic wider window; requires `refresh` and `lookback` in seconds. |
| `timeout`   | `300`    | Maximum runtime in seconds.                                                   |
| `max_bytes` | 64 MiB   | Maximum stdout size.                                                          |

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

The [reviewed examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors) cover local documents, selected highlights, local Git history, Google Calendar and Drive folders. Provider integrations need their CLI, authentication and a deliberately selected scope. Keep your copied sensor under review and test it with a fake provider before scheduling it.

## Collect and update

After reviewing the configured programs, preview due work and run an update:

```bash
bf update --dry-run  # list due work; execute nothing
bf update           # run due sensors, then routines
bf status
```

The demo sensor is not due because its `refresh` is zero. Set a nonzero interval only when you want it included in updates.

A sensor is due when it is enabled, `refresh` is nonzero and that interval has elapsed since its last success. Updates follow the [brain selection rules](commands.md) and never execute referenced brains. Failures leave the sensor due, allow other programs to continue and make the command exit 1.

Window sensors resume from their previous window minus `overlap`, catching up at most 30 days. Run state stays in `~/.local/state/bf/`; without it, the next run uses `lookback`. Keep that state on the [collecting laptop](team.md#collect-on-a-laptop).

### Frequent updates and periodic reconciliation

Use short incremental windows for frequent updates and a wider window less often to revisit mutable evidence. For a reviewed message sensor accepting `START END`:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  messages:
    command: [sensors/messages.py, "{{start}}", "{{end}}"]
    refresh: 3600
    overlap: 300
    reconcile: { refresh: 86400, lookback: 604800 }
```

The first scheduled run requests seven days. After a success at 10:00, the 11:00 run requests 09:55–11:00. Once a day, the next due run widens the window to seven days, updating the same source and record IDs. A longer catch-up window stays longer. Failed collection does not advance reconciliation; the next update retries it. Manual collection and dry runs do not acknowledge scheduled reconciliation.

Both reconciliation values are required positive integers, bounded to 365 days. The setting is allowed only for `mode: window`; snapshots already replace their complete selected catalog. Reconciliation is checked when the sensor is due, so its actual cadence cannot be faster than `refresh`. Without local state, it runs again. Removing the setting restores ordinary incremental windows.

`bf update --dry-run` lists the requested `start`, `end` and `reconcile` flag without executing anything. Sensors should honor these bounds rather than silently widening every request. Catalog sensors may deliberately ignore time bounds; document their actual scope. A finite reconciliation horizon cannot discover every older edit or disappearance. Provider change cursors need explicit recovery and commit semantics; they are not provided by these time windows.

### Backfills and coverage

`bf status` separates indexed totals from `last_run` counts: added, updated, unchanged and removed. Backfilling an older window does not claim a fresh collection or fill a gap between windows. A window touching existing coverage extends it; a future `--until` never claims coverage beyond the run time.

Successful collection replies and `bf status`'s `last_run` also report `requested_start`, `requested_end`, `reconcile`, `elapsed_seconds` and `output_bytes`. Duration covers sensor execution, validation and evidence persistence, excluding the wait for another run of that sensor and the later index refresh. Bytes measure sensor stdout, not provider network transfer. Saved counters describe the most recent successful collection; a later failure retains them with an error. `reconciled` records the last successful scheduled reconciliation separately from cumulative source coverage.

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
