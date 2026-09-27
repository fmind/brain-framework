# Example sensors

Standalone sensors for common providers. Copy the ones you need into a brain's `sensors/`, declare them in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Sensor                    | Arguments                              | Saves                                                                 |
| ------------------------- | -------------------------------------- | --------------------------------------------------------------------- |
| `git-history.py`          | `ROOT START END [--skip REPO]...`      | Commits, with author and repository identities.                       |
| `local-documents.py`      | `LABEL ROOT [--exclude GLOB]...`       | A snapshot of supported documents in the chosen folder.               |
| `google-calendar.py`      | `CALENDAR START END [--agenda-days N]` | Calendar events, organizer/invitee identities and an optional agenda. |
| `google-drive-folders.py` | `[START END]`                          | The full Drive folder catalog and parent links.                       |

Start with the [one-file local walkthrough](../../docs/docs/sensors.md#your-first-sensor) or the [credential-free example brain](../brain/README.md). For other sources, copy the selected script and merge its configuration below into `bf.yaml`. Review paths and account scope first; do not copy all sources unless you intend to run them.

```yaml
# https://fmind.github.io/brain-framework/
version: 5
name: brain
sensors:
  git-commits:
    command: [sensors/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    refresh: 3600
  google-calendar-events:
    command: [sensors/google-calendar.py, primary, "{{start}}", "{{end}}"]
    refresh: 3600
  local-documents:
    command: [sensors/local-documents.py, work, "{{home}}/Documents/knowledge"]
    mode: snapshot
    enabled: false # select the intended folder before enabling
  drive-folders:
    command: [sensors/google-drive-folders.py]
    mode: snapshot
    refresh: 86400
```

## Preview one source

After reviewing and configuring `git-commits`, preview a week's records:

```bash
bf collect git-commits --since 7d --dry-run --brain ~/brain
```

Inspect the returned samples and counts. A preview runs the sensor and may contact its provider, but does not save records. When the scope and output are correct, omit `--dry-run` to collect. Run `bf read memories/git-commits --brain ~/brain`, then pass a listed record ref to `bf read`.

## Scope and limits

- Git history scans repositories one or two levels below `ROOT`. It skips symlinked directories, hidden or named repositories, and bot/test authors. It includes commits within `START <= time < END` even when commit dates are out of order.
- Local documents support text, Markdown, HTML, PDF (with `pdftotext`), Word, Excel and PowerPoint. Unsupported, hidden and common dependency files are outside the snapshot. It refuses symlinks and special files, bounds Office expansion, and converts PDFs in a private temporary directory. Bounded content carries a partial marker; there is no OCR.
- Calendar's optional agenda snapshot runs from two days before END through N days after it, using separate agenda identities. An invitee entry means the person was listed, not that they attended.
- Drive folders always emits a complete catalog; its time-window arguments are ignored. `mode: snapshot` removes missing folders after a successful nonempty replacement.

## Contract

Each script uses Python 3.14's standard library, has an executable `#!/usr/bin/env python3` entry point and invokes provider CLIs with argument arrays, without a shell. Provider CLIs own credentials; sensors do not read credential files.

Stdout is one JSON array. Each record has a stable `id`, meaningful `title`, searchable `text` and optional time, URL, identities and structured attributes. Keep raw provider payloads out of text. `{{start}}` and `{{end}}` are timezone-aware timestamps for a half-open window.

Provider output, time and pagination are bounded. Failure, overflow or incomplete pagination stops the child and exits nonzero with no stdout and a generic stderr message. `tests/test_adapters_*.py` checks these boundaries with fake providers.

For typed relationships, map Git's `/attributes/author_refs` and `/attributes/repository_refs`, or Calendar's `/attributes/organizer_refs`, `/attributes/attendee_refs` and `/attributes/participant_refs`. Declare each target field with `type: identity`, `cardinality: many` and `relation: true`; see the [schema example](../../docs/docs/schema.md#shared-fields-and-sensor-mappings).
