# Example sensors

Standalone sensors for common providers. Copy the ones you need into a brain's `sensors/`, declare them in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Sensor                                           | Arguments                                                             | Saves                                                                                  |
| ------------------------------------------------ | --------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `git-history.py`                                 | `ROOT START END [--skip RELATIVE_REPO]...`                            | Commits on branches, tags and remote branches, with author and repository identities.  |
| [`github-history.py`](github-history.md)         | `OWNER/REPO commits\|issues\|pulls START END [--branch NAME]`         | GitHub branch commits and all-age issues/PRs, with incremental refresh.                |
| `local-documents.py`                             | `LABEL ROOT [--exclude GLOB]...`                                      | A snapshot of supported documents in the chosen folder.                                |
| `highlights.py`                                  | `LABEL EXPORT.json`                                                   | Selected passages, annotations and precise source locations.                           |
| `google-calendar.py`                             | `CALENDAR START END [--agenda-days N]`                                | Calendar events, organizer/invitee identities and an optional agenda.                  |
| `google-drive-folders.py`                        | `[START END]`                                                         | My Drive and shared-with-me folders with parent links; shared drives are excluded.     |
| `gmail-headers.py`                               | `START END [--query=SEARCH]`                                          | Gmail senders, recipients, subjects and dates with thread links; never message bodies. |
| [Four-tool demo](../context-hub/sensors/demo.py) | `TOOL END`, where `TOOL` is `workspace`, `jira`, `github` or `gcloud` | Fictional adapter records for the four-tool walkthrough; no provider access.           |

Start with the [one-file local walkthrough](../../docs/docs/getting-started.md#collect-your-first-source), which [Add a sensor](../../docs/docs/sensors.md#your-first-sensor) then replaces with `local-documents.py`, or the [credential-free example brain](../brain/README.md). For other sources, copy the selected script from the release tag matching `bf --version` and make it executable, since the configuration below runs each script as a command and a download does not keep the executable bit:

```bash
mkdir -p sensors
curl -fsSLo sensors/git-history.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors/git-history.py"
chmod +x sensors/git-history.py
```

Then merge its configuration below into `bf.yaml`. Review paths and account scope first; do not copy all sources unless you intend to run them.

The [four-tool walkthrough](../context-hub/README.md) shows how differently named fields become one shared project relation during collection. Its fixtures are not live integrations. Use `gh`, `gws`, `acli` or `gcloud` to implement selected provider access in a brain-owned script, as [adapt a sensor](#adapt-a-sensor) describes; the [integration table](../../docs/docs/context-hub.md#use-it-on-your-work) distinguishes reviewed examples from adapters you write.

```yaml
# https://fmind.github.io/brain-framework/
version: 8
name: brain
sensors:
  git-commits:
    command: [sensors/git-history.py, "{{home}}/code", "{{start}}", "{{end}}"]
    refresh: 3600
  google-calendar-events:
    command: [sensors/google-calendar.py, primary, "{{start}}", "{{end}}"]
    refresh: 3600
  local-documents:
    command: [sensors/local-documents.py, work, "{{home}}/Documents/knowledge"]
    mode: snapshot
    enabled: false # select the intended folder before enabling
  highlights:
    command: [sensors/highlights.py, reading, "{{brain}}/inputs/highlights.json"]
    mode: snapshot
    enabled: false # select a complete export before enabling
  drive-folders:
    command: [sensors/google-drive-folders.py]
    mode: snapshot
    refresh: 86400
  gmail:
    command: [sensors/gmail-headers.py, "{{start}}", "{{end}}", "--query=-category:promotions"]
    refresh: 3600
    enabled: false # review the query before enabling
```

For a bounded GitHub backfill, follow [GitHub history](github-history.md): one year of `main` commits, all available issues and PRs, then incremental refresh. This is separate from the broader local Git sensor.

## Preview one source

After reviewing and configuring `git-commits`, preview a week's records from your brain directory:

```bash
bf collect git-commits --since 7d --dry-run
```

Inspect the returned samples and counts. A preview runs the sensor and may contact its provider, but does not save records. When the scope and output are correct, omit `--dry-run` to collect. Run `bf read memories/git-commits`, then pass a listed record ref to `bf read`.

For scheduled window sensors, `refresh` controls the regular cadence and `overlap` protects the resume boundary. Add `reconcile: {refresh: 86400, lookback: 604800}` to revisit seven days once daily on the same source. Preview requested windows with `bf update --dry-run`; compare output bytes, elapsed seconds and changed records with `bf status`. Snapshot catalogs already replace their complete selection and cannot use `reconcile`.

## Scope and limits

GitHub history documents its scope and limits in [its guide](github-history.md#evidence-and-limits).

The limits below also bound the time of each call to a provider or converter, while BF's [`timeout`](../../docs/docs/sensors.md#sensor-settings), 300 seconds by default and at most 3,600, bounds the whole run. When a large `ROOT`, many PDFs or a busy inbox fails with `program timed out after 300s`, narrow the folder or the window, or raise that sensor's `timeout`.

### Git history

- Requires Git 2.37 or later, for `git log --since-as-filter`. With an older or missing `git` on PATH, the run fails before reading any repository: `Git 2.37 or later is required`.
- Scans checkouts one or two levels below `ROOT`: select the folder holding your checkouts, such as `{{home}}/code`.
- Collects each repository once, however many of its worktrees are there, under one name: its main checkout's path, else the path of its own folder below `ROOT` (such as a bare `project.git` or a hidden checkout), else its first worktree's in name order. Only that last name changes as worktrees come and go: later windows save their commits under the new name, so commits they revisit appear under both.
- Reads branches, tags, remote branches and one checkout's `HEAD` (the main checkout, else the first worktree), never stashes or notes. It includes commits with `START <= time < END`, even when commit dates are out of order.
- Skips symlinked directories, hidden repositories without a worktree in scope, bot or test authors and repositories named with `--skip`: the repository's name or any of its worktrees' paths skips it whole.
- Skips folders it cannot read, or whose names are not UTF-8 or contain control characters, with a count on stderr. Window mode keeps records already saved.
- Fails the run and names the repository when Git fails or times out there, such as a worktree whose repository moved, damaged objects, another owner's checkout or a stalled mount. Repair it or add `--skip RELATIVE_REPO`.
- Links GitHub remotes as lowercase `repo:github.com/owner/name` and author emails as lowercase `person:email/` identities. Identities are case-sensitive: write note aliases the same way.
- Limits: 200 repositories, 10,000 commits per window and 16 MiB of output; 30 seconds per `git log` and 10 per remote lookup.

### Local documents

- Supports text (`.txt`, `.md`, `.rst`, `.org`, `.csv`, `.tsv`), HTML, PDF (with `pdftotext`), Word (`.docx`), Excel (`.xlsx`) and PowerPoint (`.pptx`), without OCR; other files, such as legacy `.doc`, `.xls` and `.ppt`, stay outside the snapshot. So do hidden files and folders, common dependency folders and the owner files (`~$name.docx`) Office writes beside an open document.
- Fails the snapshot on any other symbolic link or special file, even one with an unsupported suffix, and names its root-relative path: exclude it with `--exclude`. Bounds Office archive expansion and converts PDFs in a private temporary directory.
- `ROOT` and every parent folder must be real directories. When `{{home}}` or a parent such as `Documents` is a link, the run fails saying so: configure the path `realpath` prints.
- Keeps at most 64 KiB of text per document and sets `attributes.partial` when it cuts the text.
- Also sets `attributes.partial` for text that is not valid UTF-8, such as a legacy CSV kept with replacement characters, and for an Office XML part beyond 100,000 elements or 64 nesting levels: parsing stops at that bound and keeps the text read so far.
- Skips names that are not UTF-8, contain control characters or exceed the record id limit, with a count on stderr.
- Fails the snapshot on an unreadable or damaged document and names its root-relative path, so its saved record is not silently removed: fix or exclude it.
- Limits: 10,000 entries, 20 folder levels, 16 MiB per file and 64 MiB in total; 60 seconds per PDF conversion.

### Highlights

- Reads one complete export in the [portable format](#highlights-export), not a provider-specific download. The [fictional export](highlights.json) and [walkthrough](../../docs/docs/sensors.md#selected-highlights) show the provenance mapping.
- Keeps source text in `text` and annotations in `attributes.annotation`. It never fetches a URL or reads credentials.
- Refuses duplicate ids or keys, unknown fields, invalid dates or locations, symlinks in any path component and special files.
- Limits: 8 MiB and 1,000 highlights of input, 64 KiB per selection or annotation and 16 MiB of output. Any failure emits no records; nothing is silently truncated.

### Google Calendar

- Selects events overlapping the requested interval: [Google's `timeMin` bounds event end, while `timeMax` bounds event start](https://developers.google.com/workspace/calendar/api/v3/reference/events/list). It is not a modification-time feed: reconcile past windows to revisit changed events.
- Keeps cancellations, titled `Cancelled: SUMMARY`, so they overwrite records collected earlier.
- An invitee entry means the person was listed, not that they attended. Participant emails become lowercase `person:email/` links.
- Keeps at most BF's 1,000 links per event: organizer, source, attachment and agenda links first, then invitees in email order. A cut sets `attributes.participants_truncated`; `attributes.participants` keeps every address.
- Limits: 10,000 events and 20 pages; 60 seconds per `gws` call.

`--agenda-days N`, from 1 to 366, turns a run into an agenda of the events from two days before `END` through N days after it; `START` must still precede `END` but does not bound the agenda. Add it as a separate `mode: snapshot` source beside `google-calendar-events`:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  google-calendar-agenda:
    command: [sensors/google-calendar.py, primary, "{{start}}", "{{end}}", --agenda-days, "14"]
    mode: snapshot
    refresh: 3600
```

Preview it with `bf collect google-calendar-agenda --dry-run`. Expected result: each sample has an alias such as `agenda:primary/EVENT_ID` and links `calendar:primary/EVENT_ID`, the same event's identity in `google-calendar-events`. Each replacement removes events that left the range. An agenda that becomes empty, or loses more than half of its events (and more than 10) at once, for example after a long pause, trips the [removal guard](../../docs/docs/sensors.md#define-the-scope-before-adding-a-sensor) and keeps its saved records. Check the preview, then accept that one run with `bf collect google-calendar-agenda --allow-removal`.

### Google Drive folders

- Lists every My Drive and shared-with-me folder (`corpora: user`); shared drives are excluded. It ignores the time-window arguments.
- As a `mode: snapshot` source, it removes missing folders after a successful nonempty replacement.
- A folder's `time` is its creation and `attributes.updated` its last modification.
- Limits: 10,000 folders and 20 pages; 120 seconds per `gws` call.

### Gmail headers

- Saves each message's `From`, `To` and `Cc` addresses, subject, label ids (`INBOX`, `IMPORTANT`, or `Label_N` for your own labels) and receipt time, never its body or attachments: it requests Gmail's `metadata` format only. A subject can still hold private text; keep the brain private.
- Selects messages received in `[START, END)`; `--query=SEARCH` adds a [Gmail search](https://support.google.com/mail/answer/7190), such as `--query=-category:promotions` or `--query=label:work`. Write it with `=` so a leading `-` stays its value.
- Authorize gws with `gmail.readonly` and without `gmail.metadata`: the window is a Gmail search (`q`), which Gmail rejects for any token that holds the `gmail.metadata` scope, so every run would fail. The sensor still requests only the `metadata` format.
- Links each address as a lowercase `person:email/` identity and every message of a conversation to `gmail-thread:THREAD`, so `bf read gmail-thread:THREAD` lists the thread. Its alias `mid:MESSAGE-ID` ([RFC 2392](https://www.rfc-editor.org/rfc/rfc2392)) lets another tool's record name the same message.
- `url` opens the thread in the browser's default Gmail account.
- Limits: 1,000 messages and 10 pages per window, one `gws` call per message and 60 seconds per call: keep `refresh` short so windows stay small. A message's text names every address; its `links` and each of `from_refs`, `to_refs` and `cc_refs` keep at most 1,000, in address order.

Preview it with `bf collect gmail --since 1d --dry-run`. Expected result: each sample's `text` starts with `Subject:`, then the `From:` and `To:` addresses, and its `links` hold `person:email/` and `gmail-thread:` identities.

## Highlights export

`highlights.py LABEL EXPORT.json` reads one complete export in this portable format. Convert exports from reading tools into it before collection:

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

| Field         | Rule                                                                                                     |
| ------------- | -------------------------------------------------------------------------------------------------------- |
| `id`          | Stable across edits; 1–128 letters, digits, dots, underscores or hyphens; starts with a letter or digit. |
| `title`       | Nonempty, single-line document title; at most 4,096 UTF-8 bytes.                                         |
| `selection`   | The exact selected text; at most 64 KiB. It becomes the record's `text`.                                 |
| `annotation`  | Optional interpretation, kept apart in `attributes.annotation`; at most 64 KiB.                          |
| `source_url`  | HTTPS document URL without credentials; at most 8,192 bytes. It becomes the record's `url`.              |
| `locator`     | A positive `page` up to 1,000,000, a nonempty `section` up to 4,096 bytes, or both.                      |
| `captured_at` | Timestamp with seconds and a timezone; it becomes the record's `time`.                                   |
| `source_date` | Optional document date, `YYYY-MM-DD`.                                                                    |

`LABEL` is a stable, lowercase scope label of up to 64 characters, starting with a letter; it prefixes every record id, as in `highlights:reading/brief-clarity`. These fields are supplied provenance, not verified facts. Unknown fields and duplicate keys fail. As a snapshot, a new export replaces the whole source: supply the complete selection.

## Adapt a sensor

Each script needs only Python 3.11 or later and calls its provider CLI, which owns the credentials, without a shell and from `/`: `gws` loads a `.env` file from its working directory or any parent folder, so one in the brain or above it could change the account a sensor collects from. It prints one JSON array in the [record format](../../docs/docs/schema.md#mapping-rules), keeping each record within BF's limits, or, when anything fails or a page is missing, exits nonzero with nothing on stdout and one content-free line on stderr, so BF keeps the saved records. Keep these behaviors when you adapt a script, and test them with fake provider output. `{{start}}` and `{{end}}` are timezone-aware bounds of a half-open window. A sensor never prints `fields`: BF computes them from your mappings, and `updated`, `observed` and `partial` are [reserved attribute keys](../../docs/docs/schema.md#record-revisions-and-provenance), such as Drive's `modifiedTime` mapped to `updated`.

For typed relations, map Git's `/attributes/author_refs` and `/attributes/repository_refs`, Calendar's `/attributes/organizer_refs`, `/attributes/attendee_refs` and `/attributes/participant_refs`, or Gmail's `/attributes/from_refs`, `/attributes/to_refs` and `/attributes/cc_refs`. Declare each target field with `type: identity`, `cardinality: many` and `relation: true`; see the [field mappings](../../docs/docs/schema.md#shared-fields-and-sensor-mappings).
