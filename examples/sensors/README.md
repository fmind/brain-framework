# Example sensors

Standalone sensors for common providers. Copy the ones you need into a brain's `sensors/`, declare them in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Sensor                    | Arguments                              | Records                                                                                                                                                                                                                                   |
| ------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `git-history.py`          | `ROOT START END [--skip REPO]...`      | Commits, separate author/repository references for schema mapping, author email identities and recognized GitHub repository links, one or two levels below `ROOT`; skips hidden repositories, named repositories and bot or test authors. |
| `local-documents.py`      | `LABEL ROOT [--exclude GLOB]...`       | Complete scoped snapshot of text, Markdown, HTML, PDF (requires `pdftotext`), Word, Excel and PowerPoint; bounded text carries a partial marker.                                                                                          |
| `google-calendar.py`      | `CALENDAR START END [--agenda-days N]` | Events of one calendar, separate organizer and invited-attendee references for schema mapping; optional separate agenda snapshot spans two days before END through N days after it, with distinct agenda identities.                      |
| `google-drive-folders.py` | `[START END]`                          | The complete folder catalog with a validated Drive response kind, each folder linked to its parents; the window is ignored.                                                                                                               |

```yaml
# https://fmind.github.io/brain-framework/
version: 5
name: knowledge
sensors:
  git-commits:
    command: [sensors/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    trust: owner # only for repositories whose commit messages you write; keep external otherwise
    refresh: 3600
  google-calendar-events:
    command: [sensors/google-calendar.py, primary, "{{start}}", "{{end}}"]
    refresh: 3600
  local-documents:
    command: [sensors/local-documents.py, work, "{{home}}/Documents/knowledge"]
    mode: snapshot
    trust: owner # only if you wrote the selected folder yourself
    enabled: false # select the intended folder before enabling
  drive-folders:
    command: [sensors/google-drive-folders.py]
    mode: snapshot
    refresh: 86400
```

## Contract

`tests/test_adapters_*.py` checks every example with fake provider executables.

- Python 3.14 standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Provider access goes through the provider CLI (`gws`, `gh`, `git`) with explicit argv; the CLI owns credentials and sensors never read credential files.
- `{{start}}` and `{{end}}` are timezone-aware ISO 8601 timestamps bounding a half-open window.
- Stdout is exactly one JSON array of records with a stable `id`, a meaningful `title`, searchable facts in `text`, event `time`, `url`, explicit identities in `links` (`repo:github.com/owner/name`, `person:email/address`) and `aliases`, and selected `attributes`.
- Provider stdout is bounded while the process runs; timeouts and overflow stop and reap the child. Pagination is finite and complete before output; a limit or provider failure exits 1 with nothing on stdout and one generic sentence on stderr.
- Local documents skip hidden files and common dependency folders, refuse symlinks and special files, bound Office archive expansion, and process PDF input in a private temporary directory. The snapshot covers only supported extensions; deleted files disappear at the next successful run. Keep the scope small; this is not a filesystem crawler or OCR service.
- Only reviewed fields are projected; raw payloads never land in `text`.

Git history shows local collection, Calendar a windowed provider and Drive folders a complete snapshot: declare it with `mode: snapshot` so a folder missing from the catalog disappears from search. For a credential-free walkthrough, copy the [runnable example brain](../brain/README.md).

Map Git `author` from `/attributes/author_refs` and `repository` from `/attributes/repository_refs`. Map Calendar `organizer`, `attendee` and `participant` from the corresponding `/attributes/*_refs` arrays. Declare these as `type: identity`, `cardinality: many`, `relation: true` under `schema`; see the [schema guide](../../docs/docs/schema.md). An attendee entry means the person was listed, not that they attended.
