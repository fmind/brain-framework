# Example sensors

Standalone sensors for common providers. Copy the ones you need into a brain's `sensors/`, declare them in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Sensor                                         | Arguments                                                     | Saves                                                                                 |
| ---------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `git-history.py`                               | `ROOT START END [--skip REPO]...`                             | Commits on branches, tags and remote branches, with author and repository identities. |
| [`github-history.py`](github-history.md)       | `OWNER/REPO commits\|issues\|pulls START END [--branch NAME]` | GitHub branch commits and all-age issues/PRs, with incremental refresh.               |
| `local-documents.py`                           | `LABEL ROOT [--exclude GLOB]...`                              | A snapshot of supported documents in the chosen folder.                               |
| `highlights.py`                                | `LABEL EXPORT.json`                                           | Selected passages, annotations and precise source locations.                          |
| `google-calendar.py`                           | `CALENDAR START END [--agenda-days N]`                        | Calendar events, organizer/invitee identities and an optional agenda.                 |
| `google-drive-folders.py`                      | `[START END]`                                                 | My Drive and shared-with-me folders with parent links; shared drives are excluded.    |
| [Context demo](../context-hub/sensors/demo.py) | `workspace`, `jira`, `github` or `gcloud`                     | Fictional adapter records for the shared-schema walkthrough; no provider access.      |

Start with the [one-file local walkthrough](../../docs/docs/getting-started.md#collect-your-first-source), which [Add a sensor](../../docs/docs/sensors.md#your-first-sensor) then replaces with `local-documents.py`, or the [credential-free example brain](../brain/README.md). For other sources, copy the selected script and merge its configuration below into `bf.yaml`. Review paths and account scope first; do not copy all sources unless you intend to run them.

The [four-tool example](../context-hub/README.md) shows how differently named fields become shared project relationships automatically during ingestion. Its fixtures are not live integrations. Use `gh`, `gws`, `acli` or `gcloud` to implement selected provider access in a brain-owned script, following the contract below; the [integration table](../../docs/docs/context-hub.md#use-it-on-your-work) distinguishes reviewed examples from adapters you write.

```yaml
# https://fmind.github.io/brain-framework/
version: 6
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

- Git history scans repositories one or two levels below `ROOT`, so select the folder holding your checkouts, such as `{{home}}/code`. It reads branches, tags, remote branches and a detached `HEAD`, never stashes or notes, and skips symlinked directories, hidden or named repositories, and bot/test authors. Folders it cannot list, or whose names are not UTF-8 or contain control characters, are skipped with a count on stderr; window mode keeps records already saved. A repository where Git fails or times out, such as a stale worktree, damaged objects, another owner's checkout or a stalled mount, fails the run and is named on stderr; repair it or add `--skip RELATIVE_REPO`. GitHub remotes become lowercase `repo:github.com/owner/name` links and author emails lowercase `person:email/` links: identities are case-sensitive, so write note aliases the same way. It includes commits within `START <= time < END` even when commit dates are out of order. Limits: 200 repositories, 10,000 commits per window and 16 MiB of output.
- Local documents support text, Markdown, HTML, PDF (with `pdftotext`), Word, Excel and PowerPoint. Unsupported, hidden and common dependency files are outside the snapshot. It refuses symlinks and special files, bounds Office expansion, and converts PDFs in a private temporary directory. Text beyond 64 KiB, or not valid UTF-8 (such as a legacy CSV, kept with replacement characters), sets `attributes.partial`; there is no OCR. Names that are not UTF-8, contain control characters or exceed the record id limit are skipped with a count on stderr. An unreadable or damaged document instead fails the snapshot and names its root-relative path, so its saved record is not silently removed; fix or exclude it. `ROOT` and every parent folder must be real directories: when `{{home}}` or a parent such as `Documents` is a link, the run fails saying so; configure the path `realpath` prints. Limits: 10,000 entries, 20 folder levels, 16 MiB per file and 64 MiB in total.
- Highlights read one complete portable JSON export, not a provider-specific download. The [fictional export](highlights.json) and [walkthrough](../../docs/docs/sensors.md#selected-highlights) show the format and provenance mapping. Source text stays in `text`; annotations stay in `attributes.annotation`. The sensor never fetches a URL or reads credentials. It refuses duplicate ids/keys, unknown fields, invalid dates/locations, symlinks in any path component and special files. Input is at most 8 MiB and 1000 highlights; each selection or annotation is at most 64 KiB, and output at most 16 MiB. Any failure emits no records; nothing is silently truncated.
- Calendar selects events overlapping the requested interval: [Google's `timeMin` bounds event end, while `timeMax` bounds event start](https://developers.google.com/workspace/calendar/api/v3/reference/events/list). It is not a modification-time feed; reconcile past windows to revisit changed events. Cancellations stay, titled `Cancelled: SUMMARY`, so they overwrite records collected earlier. Its optional agenda snapshot runs from two days before END through N days after it, using separate agenda identities; replacement removes events that leave that range. Configure it as a separate `mode: snapshot` source. An agenda that becomes empty, or loses most of its events at once, trips the [removal guard](../../docs/docs/sensors.md#define-the-scope-before-adding-a-sensor): check it, then run `bf collect AGENDA --allow-removal`. An invitee entry means the person was listed, not that they attended. Participant emails become lowercase `person:email/` links. An event with more than about 1,000 participants keeps its organizer, source, attachment and agenda links, then invitees in email order up to BF's 1,000 links, and sets `attributes.participants_truncated`; `attributes.participants` keeps every address. Limits: 10,000 events and 20 pages.
- Drive folders lists every My Drive and shared-with-me folder (`corpora: user`); shared drives are excluded. Its time-window arguments are ignored. `mode: snapshot` removes missing folders after a successful nonempty replacement. A folder's `time` is its creation and `attributes.updated` its last modification. Limits: 10,000 folders and 20 pages.

## Contract

Each script uses the Python 3.11+ standard library, has an executable `#!/usr/bin/env python3` entry point and invokes provider CLIs with argument arrays, without a shell. Provider CLIs own credentials; sensors do not read credential files.

Stdout is one JSON array in the [record envelope](../../docs/docs/schema.md#mapping-rules). Each record has a stable `id`, meaningful `title`, searchable `text` and optional time, URL, identities and structured attributes. Keep raw provider payloads out of text and attributes: map selected fields explicitly. `updated`, `observed` and `partial` are [reserved attribute keys](../../docs/docs/schema.md#record-revisions-and-provenance); for example, Drive's `modifiedTime` maps to `updated`. `{{start}}` and `{{end}}` are timezone-aware timestamps for a half-open window.

One invalid record fails a whole collection, so the Git, document, Calendar and Drive sensors keep each record within BF's bounds: titles become one line of at most 4,096 characters, URLs and links with control characters or over 8,192 characters are dropped, a record keeps at most 1,000 links, aliases are namespaced `scheme:value` identities with percent-encoded paths, and ids stay within 7,988 characters once percent-encoded. Highlights instead refuses an export that breaks them.

Provider output, time and pagination are bounded. Failure, overflow or incomplete pagination stops the child and exits nonzero with no stdout and one stderr line: a content-free reason written by the sensor, such as the limit reached or a document's root-relative path, or a generic sentence for provider errors. Calendar and Drive reject malformed continuation tokens; Calendar also rejects malformed event lists and duplicate event identities, so a partial catalog cannot masquerade as a complete replacement. `tests/test_adapters_*.py` checks these boundaries and preservation of saved records with fake providers.

For typed relationships, map Git's `/attributes/author_refs` and `/attributes/repository_refs`, or Calendar's `/attributes/organizer_refs`, `/attributes/attendee_refs` and `/attributes/participant_refs`. Declare each target field with `type: identity`, `cardinality: many` and `relation: true`; see the [schema example](../../docs/docs/schema.md#shared-fields-and-sensor-mappings).
