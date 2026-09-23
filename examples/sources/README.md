# Example collectors

Standalone collectors for common providers. Copy the ones you need into a base's `sources/`, declare them in `fkf.yaml`, and adapt and test them there; they then belong to the base. The Python package neither bundles nor installs them.

| Collector                 | Arguments            | Records                                                                                                  |
| ------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------- |
| `git-history.py`          | `ROOT START END`     | Commits, author email identities and recognized GitHub repository links, one or two levels below `ROOT`. |
| `google-calendar.py`      | `CALENDAR START END` | Events of one calendar, including cancellations, all-day boundaries and participant email identities.    |
| `google-drive-folders.py` | `[START END]`        | The complete folder catalog, each folder linked to its parents; the window is ignored.                   |

```yaml
# https://fmind.github.io/fkf/
version: 2
name: knowledge
sources:
  git-commits:
    command: [sources/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    refresh: 3600
  google-calendar-events:
    command: [sources/google-calendar.py, primary, "{{start}}", "{{end}}"]
    refresh: 3600
  drive-folders:
    command: [sources/google-drive-folders.py]
    mode: snapshot
    refresh: 86400
```

## Contract

`tests/test_adapters_*.py` checks every example with fake provider executables.

- Python 3.14 standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Provider access goes through the provider CLI (`gws`, `gh`, `git`) with explicit argv; the CLI owns credentials and collectors never read credential files.
- `{{start}}` and `{{end}}` are timezone-aware ISO 8601 timestamps bounding a half-open window.
- Stdout is exactly one JSON array of records with a stable `id`, a meaningful `title`, searchable facts in `text`, event `time`, `url`, explicit identities in `links` (`repo:github.com/owner/name`, `person:email/address`) and `aliases`, and selected `attributes`.
- Pagination is finite and complete before output; a limit or provider failure exits 1 with nothing on stdout and one generic sentence on stderr.
- Only reviewed fields are projected; raw payloads never land in `text`.

Git history shows local collection, Calendar a windowed provider and Drive folders a complete snapshot: declare it with `mode: snapshot` so a folder missing from the catalog disappears from search. For a credential-free walkthrough, copy the [runnable example base](../base/README.md).
