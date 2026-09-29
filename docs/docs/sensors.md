---
description: Collect a selected local document, configure sensors and understand update and replacement rules.
---

# Add a sensor

A sensor turns a selected source into searchable evidence. This walkthrough replaces the tutorial sensor from Getting started with a reviewed one that collects the same fictional product brief, then keeps the decision's evidence link current. No provider credentials are needed.

The script prints one JSON array of [records](brain.md#records); Brain Framework validates and saves them by id. A sensor must fail if collection is incomplete: Brain Framework cannot detect pages the script silently omitted.

## Your first sensor

Complete [Getting started](getting-started.md), including its optional [collection exercise](getting-started.md#collect-your-first-source), then work inside `~/brain`. The tutorial `brief` sensor shows the record format; the reviewed [local documents sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py) scans only the directory passed to it and bounds file sizes and output. Move the brief into a dedicated input directory and copy the script from the release matching your installation:

```bash
mkdir -p inputs/website-demo
mv inputs/brief.txt inputs/website-demo/brief.txt
curl -fsSLo sensors/local-documents.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors/local-documents.py"
```

Review the script with your agent before running it. The package does not install example sensors.

## Configure a sensor

In `bf.yaml`, replace the `brief` entry under `sensors:` with this one. Keep `version`, `name`, the `kind` schema field and other settings:

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
    fields:
      kind: { value: document }
```

`website-demo` is the stable label used in record ids. The final argument selects exactly the demo directory. `uv` supplies the interpreter independently of the host's `python3`; it must be on PATH and may obtain Python if it is not already installed. The text-file collection itself needs no provider or network access. `refresh: 0` keeps this sensor manual.

Collection runs with your account permissions; registration is not required.

## Collect and read

```bash
bf collect local-documents --dry-run
bf collect local-documents
bf search "product explanation" --scope memories/local-documents
bf read local-documents:website-demo/brief.txt
```

The preview executes the script without saving its output. The real collection stores one record under `memories/local-documents/`, in a JSON file keyed by the SHA-256 of its id. Search returns `ref: local-documents:website-demo/brief.txt`; the exact read includes these selected record fields:

```json
{
  "ref": "local-documents:website-demo/brief.txt",
  "record": {
    "id": "website-demo/brief.txt",
    "title": "brief.txt",
    "text": "Visitors need a clear product explanation before signing up.\n",
    "fields": { "kind": "document" }
  }
}
```

In `projects/new-website.md`, point the Evidence line beneath the decision at the new record:

```markdown
Evidence: [Product brief](local-documents:website-demo/brief.txt).
```

The tutorial records stay searchable, and `bf status` lists their source as `historical`, until you delete them. Remove them and the tutorial script while no other BF command runs, then validate:

```bash
rm -r memories/brief sensors/brief.py
bf validate
```

Validation reports `"records":1` and `"valid":true`. If you edit the input file and collect again, the same id updates the saved record; the project link continues to work.

## Selected highlights

To collect a passage with its page or section and a separate annotation, follow [Collect selected highlights](highlights.md). The guide includes a fictional export, complete configuration, expected output and replacement rules.

## Sensor settings

The executable is a command on PATH or a `sensors/` executable. Arguments pass directly, without a shell. The placeholders `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}` are replaced once. Directly executed Python examples need an executable bit and a suitable `python3`; the walkthrough uses `uv` instead.

| Setting     | Default  | Meaning                                                                               |
| ----------- | -------- | ------------------------------------------------------------------------------------- |
| `command`   | required | Executable and up to 127 arguments of at most 16,384 characters each.                 |
| `fields`    | `{}`     | [Schema mappings](schema.md#shared-fields-and-sensor-mappings).                       |
| `enabled`   | `true`   | Disabled sensors keep their existing records searchable.                              |
| `mode`      | `window` | Update returned items, or replace a complete `snapshot`.                              |
| `refresh`   | `0`      | Seconds between automatic runs, up to 365 days; zero keeps the sensor manual.         |
| `lookback`  | `86400`  | Seconds covered by the first run, or each snapshot run; up to 365 days.               |
| `overlap`   | `300`    | Seconds re-read before the previous window's end; up to 365 days.                     |
| `reconcile` | omitted  | Optional periodic wider window; requires `refresh` and `lookback` in seconds.         |
| `timeout`   | `300`    | Maximum runtime in seconds, from 1 to 3,600.                                          |
| `max_bytes` | 64 MiB   | Maximum stdout size; configurable up to 256 MiB.                                      |
| `priority`  | `normal` | `low` [quiets a high-volume source](#quiet-a-high-volume-source) in pages and search. |

## Quiet a high-volume source

A feed that collects many records a day, such as news headlines, can crowd period pages and search results. Mark it `low` to keep it as background evidence. For a reviewed feed sensor accepting `START END`, add `priority` beside its other settings:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  news:
    command: [sensors/news.py, "{{start}}", "{{end}}"]
    refresh: 3600
    priority: low
```

After the next collection, `bf read today` lists the other sources' items, while `news` appears only under `sources`, in an entry such as `{"source":"news","records":120,"page":"memories/news/today","priority":"low"}`. Read that page to list its records.

| Where                                                | Low-priority records                                       |
| ---------------------------------------------------- | ---------------------------------------------------------- |
| Period pages (`today`, `7d`, `2026-09`…) and home    | Counted in `sources`/`activity`, not listed in items.      |
| Word search                                          | Rank at half weight; `--scope memories/news` is unchanged. |
| Source pages, exact reads, identity and tag searches | Unchanged.                                                 |

Search and read apply the setting when they run: changing it needs no `bf build`. The [retrieval example](https://github.com/fmind/brain-framework/tree/main/examples/retrieval) marks a document catalog `low`: its `2026-09-11` page lists the launch plan document and counts the catalog entry.

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

The [reviewed examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors) cover local documents, selected highlights, local Git history, [GitHub commits and issues/PRs](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md), Google Calendar and Drive folders. Copy them from the tag matching `bf --version`, as in [Your first sensor](#your-first-sensor). Provider integrations need their CLI, authentication and a deliberately selected scope. Keep your copied sensor under review and test it with a fake provider before scheduling it.

## From meeting notes to GitHub issues

The [Google Calendar sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) uses [`gws`](https://github.com/googleworkspace/cli) to collect events, including notes saved in their descriptions. After [initializing a brain](getting-started.md), create `sensors/` if needed and save the reviewed script as `sensors/google-calendar.py`. Add this `sensors:` entry to `bf.yaml`, or add only `calendar` beneath an existing mapping, keeping the other settings:

```yaml
# https://fmind.github.io/brain-framework/docs/configuration/
sensors:
  calendar:
    command: [python3, sensors/google-calendar.py, primary, "{{start}}", "{{end}}"]
    refresh: 3600 # Refresh hourly with bf watch.
```

With Python 3.11 or later as `python3`, `gws`, [Claude Code](https://code.claude.com/docs/en/cli-reference) and `gh` authenticated, and Claude allowed to run `bf` and create issues in your chosen repository:

```bash
cd ~/brain
bf collect calendar --since 1d
claude -p 'Use bf to turn the meeting notes of today into GitHub issues in OWNER/REPO with gh. Cite each event.'
```

Replace `OWNER/REPO` with your repository. Expected result: issues grounded in the collected notes, with links returned by the agent. Check that each issue matches a collected event; agent output alone does not verify the source or successful issue creation.

## Collect and update

`bf collect SENSOR` runs one sensor now; `bf update` runs the due ones. Preview and check updates with [Check before scheduling](schedule.md#check-before-scheduling). The walkthrough sensor is never due because its `refresh` is zero.

A sensor is due when it is enabled, `refresh` is nonzero and that interval has elapsed since its last success. Updates act on one [selected brain](configuration.md#select-a-brain) and never execute referenced brains. A failure lets other programs continue and makes the command exit 1. The failed sensor retries 1 minute later, then after 2, 4, 8… minutes for each consecutive failure, never waiting longer than its `refresh`; a success resets the delay. Updates check this only when they run, so the actual retry also waits for the next watch or timer cycle. `bf collect` always runs immediately.

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

The first scheduled run requests seven days. After a success at 10:00, the 11:00 run requests 09:55–11:00. Once a day, the next due run widens the window to seven days, updating the same source and record IDs. A longer catch-up window stays longer. Failed collection does not advance reconciliation; the next due update, after the failure backoff, retries it. Manual collection and dry runs do not acknowledge scheduled reconciliation.

Both reconciliation values are required positive integers, bounded to 365 days. The setting is allowed only for `mode: window`; snapshots already replace their complete selected catalog. Reconciliation is checked when the sensor is due, so its actual cadence cannot be faster than `refresh`. Without local state, it runs again. Removing the setting restores ordinary incremental windows.

`bf update --dry-run` lists the requested `start`, `end` and `reconcile` flag without executing anything. Sensors should honor these bounds rather than silently widening every request. Catalog sensors may deliberately ignore time bounds; document their actual scope. A finite reconciliation horizon cannot discover every older edit or disappearance. Provider change cursors need explicit recovery and commit semantics; they are not provided by these time windows.

### Backfills and coverage

For selected GitHub repositories, the [history walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md) backfills one year of commits reachable from `main` and all available open/closed issues and PRs, then refreshes incrementally. The year bounds initial commit collection, not retention; issues and PRs are selected by modification time. Large backfills use adjacent windows with a checklist of completed intervals. Comments, reviews and historical revisions are separate scope.

`bf status` separates indexed totals from `last_run` counts: added, updated, unchanged and removed. Backfilling an older window does not claim a fresh collection or fill a gap between windows. A window touching existing coverage extends it; a future `--until` never claims coverage beyond the run time.

For a scheduled window sensor, a later window separated from the coverage by a gap does not move the resume point either. For example, after a scheduled calendar run on Monday and a five-day pause, `bf collect calendar --since 1d` saves Saturday's events, but the next update still requests Monday through Saturday, then extends the coverage to the present. A gap older than the 30-day catch-up limit is not requested again; backfill it explicitly with `--since` and `--until`. A manual sensor (`refresh: 0`) has no update to fill the gap, so its latest run becomes `last_collected` and its `window`; backfill any gap explicitly.

Successful collection replies and `bf status`'s `last_run` also report `requested_start`, `requested_end`, `reconcile`, `elapsed_seconds` and `output_bytes`. Duration covers sensor execution, validation and evidence persistence, excluding the wait for another run of that sensor and the later index refresh. Bytes measure sensor stdout, not provider network transfer. Saved counters describe the most recent successful collection; a later failure retains them with an error. `reconciled` records the last successful scheduled reconciliation separately from cumulative source coverage.

Runs of the same sensor serialize. Different sensors may run concurrently; record commits and state updates lock briefly. See [process safeguards](limits.md#processes-and-logs) for timeouts, logs and cancellation.

## Define the scope before adding a sensor

Keep these choices beside the script: accounts and folders, stable ids, event and modification times, selected fields, size limits, deletion behavior and fake-provider tests.

| Mode       | Use for                                                                              | What disappears from stored records?              |
| ---------- | ------------------------------------------------------------------------------------ | ------------------------------------------------- |
| `window`   | History: commits, messages, meetings.                                                | Nothing automatically; returned ids are updated.  |
| `snapshot` | A bounded current catalog: folders, contacts, selected documents or a future agenda. | Items absent from a successful complete snapshot. |

A wrong account, a missing folder or a truncated listing can make a catalog look empty or smaller. A snapshot therefore fails, without changing saved records, when it returns nothing for a non-empty catalog, or would remove more than half of the existing records and more than 10 of them. The failure appears in `bf status` and the watch dashboard like any other. Smaller changes, such as a few past agenda items, succeed.

```bash
bf collect folders --dry-run          # check the returned scope first
bf collect folders --allow-removal    # accept the removal for this run only
```

Expected result: the second command succeeds and reports the `removed` count; scheduled updates keep the guard. A catalog that legitimately loses most of its items every cycle needs `--allow-removal` each time, so consider a `window` sensor for it. For mutable window sources, document how far back revisions are re-read.

## Good records

- Keep ids stable across edits and source URLs available for verification.
- Put searchable facts in `title` and `text`; use `attributes` for exact-read details.
- Use event time for `time`, upstream modification time for `attributes.updated`, and `attributes.partial: true` for deliberately incomplete text. Brain Framework supplies `attributes.observed`. These three attribute keys are reserved: map provider fields to them explicitly, such as Drive's `modifiedTime` to `updated`, rather than passing a raw payload.
- Link explicit identities and emit only namespaced `scheme:value` aliases; do not infer relationships from similar names.
- Respect the [envelope limits](schema.md#mapping-rules): drop a URL longer than 8,192 characters or containing control characters, keep ids within 4,096 characters and 7,988 once percent-encoded, titles within 4,096 characters, text within 4,194,304 characters and each record within 1,000 links and 1,000 aliases, and skip or re-encode filenames that are not valid UTF-8.
- Skip noise such as trash, promotions, bots and test runs.

## Routines

Prepare review actions with the [routine guide](routines.md).

## Schedule it

Run updates automatically with [Watch and schedule updates](schedule.md).
