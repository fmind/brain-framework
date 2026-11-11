---
icon: lucide/radar
description: Collect a selected local document or highlight, configure sensors and understand update and replacement rules.
---

# Add a sensor

A sensor turns a selected source into searchable evidence. It prints a JSON array of [records](brain.md#records), JSON Lines (one JSON record object per line) or a [partial-coverage](#report-partial-coverage) object; BF validates them and saves each by id. A sensor must fail or report what it missed when collection is incomplete: BF cannot detect pages a script silently skipped.

## Your first sensor

Complete [Getting started](getting-started.md), including its [collection exercise](getting-started.md#collect-your-first-source), then work inside `~/brain`. The reviewed [local documents sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py) uses [`_sensor.py`](https://github.com/fmind/brain-framework/blob/main/examples/sensors/_sensor.py), reads only the folder passed to it and bounds file sizes and output. Move the brief into its own folder, then copy both files from the release tag matching `bf --version`:

```bash
mkdir -p inputs/website-demo
mv inputs/brief.txt inputs/website-demo/brief.txt
curl -fsSLo sensors/_sensor.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors/_sensor.py"
curl -fsSLo sensors/local-documents.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors/local-documents.py"
chmod +x sensors/local-documents.py
```

Review the downloaded scripts before running them; `chmod +x` restores the executable bit BF needs to run a script directly.

## Configure a sensor

In `bf.yaml`, replace the `brief` entry under `sensors:` with this one. Keep `version`, `name`, the `fields:` entries and other settings:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  local-documents:
    command: [sensors/local-documents.py, website-demo, "{{brain}}/inputs/website-demo"]
    mode: snapshot
    refresh: 0
    fields:
      kind: { value: document }
```

`website-demo` is the stable label used in record ids, and the last argument selects the demo folder. The script needs Python 3.11 or later as `python3` ([sensor settings](#sensor-settings)).

## Collect and read

```bash
bf collect local-documents --dry-run
bf collect local-documents
bf search "product explanation" --scope memories/local-documents
bf read local-documents:website-demo/brief.txt
```

The dry run executes the script and previews up to three records (`added`, `updated`, `unchanged`, `removed`) without saving them. The real collection stores one record under `memories/local-documents/`, and the exact read includes:

```json
{
  "ref": "local-documents:website-demo/brief.txt",
  "record": {
    "id": "website-demo/brief.txt",
    "title": "brief.txt",
    "text": "Visitors need a clear product explanation and pricing before signing up.\n",
    "fields": { "kind": "document" }
  }
}
```

In `projects/new-website.md`, point the Evidence line beneath the decision at the new record:

```markdown
Evidence: [Product brief](local-documents:website-demo/brief.txt).
```

This edit clears the review flag on the project. Remove the retired `brief` records and tutorial script, then validate:

```bash
rm -r memories/brief sensors/brief.py
bf validate
```

Validation reports `"records":1` and `"valid":true`.

## Selected highlights

The reviewed [highlights sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py) keeps an exact passage, its locator and your separate annotation from a portable JSON export. Copy it (and `_sensor.py` if you skipped the section above) with the [fictional export](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.json):

```bash
examples="https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors"
curl -fsSLo sensors/_sensor.py "$examples/_sensor.py"
curl -fsSLo sensors/highlights.py "$examples/highlights.py"
curl -fsSLo inputs/highlights.json "$examples/highlights.json"
chmod +x sensors/highlights.py
```

Add `highlights` under `sensors:` and `source-document` under `fields:`, keeping existing entries:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  highlights:
    command: [sensors/highlights.py, reading, "{{brain}}/inputs/highlights.json"]
    mode: snapshot
    refresh: 0
    fields:
      source-document: { path: /url }
fields:
  source-document:
    description: The document containing this selected passage.
    type: identity
    cardinality: one
    relation: true
```

```bash
bf collect highlights
bf read highlights:reading/brief-clarity
```

`record.text` holds the selection, `record.url` and `record.fields.source-document` name the document, `record.attributes.locator` gives the page or section, and `record.attributes.annotation` keeps your note out of full-text search so your commentary never passes as source text. See the [sensor's README](https://github.com/fmind/brain-framework/blob/main/examples/sensors/README.md#highlights-export) for the export format.

## Sensor settings

The command is a program on PATH or in `sensors/`, followed by its arguments, run without a shell. The sensor's name becomes the source in `SOURCE:ID`.

| Setting          | Default  | Meaning                                                                                                                                                                                                                 |
| ---------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `command`        | required | Program and arguments; optional only with `enabled: false`.                                                                                                                                                             |
| `fields`         | `{}`     | [Field mappings](schema.md#shared-fields-and-sensor-mappings).                                                                                                                                                          |
| `enabled`        | `true`   | Disabled sensors never run; their records stay searchable.                                                                                                                                                              |
| `mode`           | `window` | `window` updates returned records; `snapshot` replaces the complete list.                                                                                                                                               |
| `refresh`        | `0`      | Seconds between automatic runs; `0` keeps the sensor manual.                                                                                                                                                            |
| `lookback`       | `86400`  | Seconds covered by the first run, each snapshot run and `bf collect` without `--since`.                                                                                                                                 |
| `overlap`        | `300`    | Seconds re-read before the previous window's end.                                                                                                                                                                       |
| `reconcile`      | none     | Occasional wider window `{refresh, lookback}` for `window` sensors; see [reconciliation](#frequent-updates-and-periodic-reconciliation).                                                                                |
| `timeout`        | `300`    | Maximum runtime in seconds.                                                                                                                                                                                             |
| `max_bytes`      | 64 MiB   | Maximum stdout bytes.                                                                                                                                                                                                   |
| `max_text_bytes` | none     | Per-record UTF-8 byte ceiling (1–4,194,304): cuts longer `record.text` on a UTF-8 boundary and sets `attributes.partial: true`; unset, text above 4 MiB fails (`set max_text_bytes on this sensor to cut longer text`). |
| `priority`       | `normal` | `low` [quiets a high-volume source](#quiet-a-high-volume-source).                                                                                                                                                       |
| `silent_after`   | none     | Seconds without a new or changed record before a scheduled source [falls silent](schedule.md#silent-sources).                                                                                                           |
| `expire_after`   | none     | Seconds of [retention](#keep-forget-and-expire-records): older records leave at the next collection.                                                                                                                    |

Command rules ([limits](limits.md#size-bounds)):

- **Placeholders**: `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}` expand once; `{{lines:relative/path.txt}}` splices non-blank, non-`#` lines (up to 4 MiB, 16,384 chars/line) into argv (max 128 items total). `{{home}}` fails visibly when no home folder resolves.
- **Interpreter**: direct scripts (`[sensors/local-documents.py, ...]`) need `+x` and Python 3.11+ as `python3`. With an older `python3`, prepend uv: `[uv, run, --no-project, --python, "3.14", sensors/local-documents.py, ...]`.
- **Name**: a sensor cannot be named `bf` (`bf:ID` would collide with BF addresses); see [program names](schema.md#program-names).

### What a program receives

Sensors and routines run from the brain root with stdin closed (except `bf run --stdin` or hooks) and loader-injection variables stripped, plus:

| Variable       | Value                                                                                                                         |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `BF_BRAIN`     | Brain root, so nested `bf` calls read this brain.                                                                             |
| `BF_PROGRAM`   | Program name in `bf.yaml`.                                                                                                    |
| `BF_START`     | Window start (`{{start}}`), such as `2026-09-01T00:00:00.000000Z`.                                                            |
| `BF_END`       | Window end (`{{end}}`).                                                                                                       |
| `BF_STATE_DIR` | Private machine directory (mode `700`) outside the brain for cursors; deleting it makes the next run fall back to `lookback`. |

#### Credentials {#credentials}

Prefer credentials in this order:

1. **Provider CLI login** (`gh auth login`, `gws`, `gcloud`) — the CLI manages tokens outside the script.
2. **Private file outside the brain** (mode `600`, passed as `"{{home}}/.config/acme/token"` and read with `_sensor.secret()`) — works under systemd and launchd services that inherit only `PATH`.
3. **OS keyring** — interactive sessions only (often locked under unattended user services).

Never put secrets in `bf.yaml`, command arguments, the brain or service files. To pass non-secret profile variables (such as `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`) to one program, prefix `command` with `env`: `[env, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR={{home}}/.config/gws-personal", sensors/gmail-headers.py, "{{start}}", "{{end}}"]` (or `"{{lines:settings/google.env}}"` with literal paths). Never re-add `LD_*`, `PYTHON*` or `NODE_OPTIONS`.

## Quiet a high-volume source

Set `priority: low` on high-volume feeds so they do not crowd period pages or word search:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  news:
    command: [sensors/news.py, "{{start}}", "{{end}}"]
    refresh: 3600
    priority: low
```

`bf read today` then summarizes `news` under `sources` (`{"source":"news","records":120,"page":"memories/news/today","priority":"low"}`), and word search ranks its records at half weight. Source pages, exact reads, identity/tag searches and `--scope memories/news` treat them normally.

## Any source you can script

Any CLI, API, database query or export can become a sensor. Fetch every page before printing output, fail on incomplete fetches and never print secrets. A sensor prints a JSON array or JSON Lines (one JSON object per line):

```json
{
  "id": "feedback-42",
  "title": "New website feedback",
  "text": "Visitors asked what the product does before signup.",
  "time": "2026-09-27T12:00:00Z",
  "url": "https://example.com/feedback/42",
  "links": ["bf://brain/projects/new-website.md"]
}
```

- **Reviewed examples**: see [`examples/sensors/`](https://github.com/fmind/brain-framework/tree/main/examples/sensors) for local documents, highlights, Git history, [GitHub commits, issues and pull requests](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md), Google Calendar, Gmail headers and Drive folders.
- **Pinned external packages**: when running a shared sensor via `uvx --from git+https://...@COMMIT`, pin a full 40-character commit SHA (or PEP 723 `exclude-newer`) and review that commit first; sensors run with your user permissions.

## Collect and update

`bf collect SENSOR` runs one sensor now. `bf update` and `bf watch` run due sensors (up to four concurrently) in one [selected brain](configuration.md#select-a-brain), appending stderr and summaries to `logs/SENSOR.log`. A second concurrent `bf collect` of the same sensor fails (`SENSOR is already running for this brain; retry later`), while `bf update` reports it `skipped`. Window sensors resume from their previous window minus `overlap`, catching up at most 30 days.

### Frequent updates and periodic reconciliation

Combine a short `refresh` window with an occasional wider `reconcile` window (`mode: window` only) to catch upstream edits:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  messages:
    command: [sensors/messages.py, "{{start}}", "{{end}}"]
    refresh: 3600
    reconcile: { refresh: 86400, lookback: 604800 }
```

Once a day, the due run requests seven days instead of one hour. Failed runs, manual collections and `--dry-run` do not count as reconciliations.

### Backfills and coverage

Backfill historical windows explicitly:

```bash
bf collect git-commits --since 2026-09-01 --until 2026-10-01
```

Only a collection reaching the moment it runs counts as fresh (`bf status` stays `never` after historical backfills alone, while recording `last_run` counts). Updates resume from the end of contiguous coverage: an older backfill or a window separated by a gap leaves the resume cursor unchanged so the next update fills gaps up to 30 days back ([GitHub history walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md)).

## Report partial coverage {#report-partial-coverage}

When one feed or account among several fails, a sensor can keep the rest by printing a partial-coverage object instead of an array or JSONL stream:

```json
{ "records": [{ "id": "post-1", "title": "Release notes" }], "incomplete": { "missed": 1, "of": 3 } }
```

- `1 <= missed <= of` (no extra keys); write what failed to stderr (`logs/SENSOR.log`).
- Saves returned records without removing absent snapshot items (even with `--allow-removal`) or expiring undated items (`forgotten` tombstones still apply).
- Marks the source `"degraded": {"runs": N, "missed": 1, "of": 3}` in `bf status`, `memories` pages and search coverage; after three consecutive incomplete runs, a scheduled source joins `attention` and fails `bf status --check` until a complete run clears it.

## Build a sensor with the kit

[`_sensor.py`](https://github.com/fmind/brain-framework/blob/main/examples/sensors/_sensor.py) is a standard-library helper file you copy into `sensors/` and own:

- `main(collect, failure=...)` and `cli(fn, prefix=..., hint=...)` wrap entry points without leaking provider output; `window()` and `state_dir()` read `BF_START`/`BF_END` and `BF_STATE_DIR`.
- `load_state(name, default=...)` and `save_state(name, data)` persist JSON cursors atomically in `BF_STATE_DIR`.
- `secret(path)` reads mode-`600` credential files; `run(*argv, timeout=..., max_bytes=...)` and `fetch(url, timeout=..., max_bytes=...)` bound subprocess and HTTPS calls; `git_remote_identity(url)` normalizes repository identities.
- `redact(text)` masks credential shapes before building records; `record(...)` normalizes UTF-8 text and URLs; `emit(records, missed=..., of=...)` prints validated output within BF's bounds.

Emit attributes from the [standard vocabulary](schema.md#standard-vocabulary) (`author_refs`, `parent_refs`, `label_refs`, `kind`, `status`) so `bf init` brains map them automatically.

### Check saved output

Validate saved JSON array or JSONL output against the brain's current `fields:` mappings without running the sensor or saving records:

```bash
python3 sensors/github-issues.py > /tmp/issues.jsonl
bf collect github-issues --input /tmp/issues.jsonl
```

The reply matches `--dry-run` (change counts plus up to three projected samples with `fields`), works even when `enabled: false`, and accepts `--input -` from stdin.

## Keep, forget and expire records

- **Drop specific records permanently**: `bf forget mail:ID` deletes the record and writes the SHA-256 of its id (never the raw id) to `memories/mail/.forgotten` so future collections skip it (`bf forget --dry-run` previews).
- **Retain a rolling window**: `expire_after: 31536000` removes records whose `time` (or last-changed timestamp when undated and absent) is older than a year at each complete collection. Purge Git history and backups separately when content must vanish everywhere.

## Define the scope before adding a sensor

Choose `window` for append/update histories (commits, mail, meetings) and `snapshot` for bounded current lists (folders, contacts, selected docs). A complete snapshot refuses to empty a non-empty source or remove more than half of it (>10 records) unless confirmed for one manual run:

```bash
bf collect folders --dry-run          # inspect removed count and removal_refused
bf collect folders --allow-removal    # accept removal for this run only
```

## Good records

- Keep `id` stable across edits, include `url` for verification, put searchable prose in `title`/`text` and keep structured details in `attributes`.
- Use `time` for the event instant, `attributes.updated` for upstream edits and `attributes.partial: true` for truncated text; BF stamps `attributes.observed`. Linking between a note and a record whose `time` or `updated` is newer than the note's edit date raises `newer_evidence` in `bf read review` (`observed` never does).
- Emit lowercase namespaced identities (`person:email/alice@example.com`, `repo:github.com/acme/web`) in `links` and `aliases`.

See [Watch and schedule updates](schedule.md) and [Routines](routines.md).
