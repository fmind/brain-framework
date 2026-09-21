# Example source scripts

Standalone examples for common providers. Once copied, scripts belong to the base: adapt, test and version them there. FKF does not synchronize or enforce equality with these examples. Copy the ones you need into your base's `sources/`, declare them in `fkf.yaml`, and review them like any other code you run. They are distributed from this repository like `skills/`; the Python wheel neither bundles nor installs them.

| Adapter                   | Arguments            | Records                                                                                                  |
| ------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------- |
| `google-drive-folders.py` | `[START END]`        | Complete user-visible folder catalog with parent identities; window is ignored.                          |
| `git-history.py`          | `ROOT START END`     | Commits, author email identities and recognized GitHub repository links, one or two levels below `ROOT`. |
| `google-calendar.py`      | `CALENDAR START END` | Events of one calendar, including cancellations, all-day boundaries and participant email identities.    |

```yaml
# https://fmind.github.io/fkf/
version: 1
id: aabbccddeeff00112233445566778899
name: knowledge
sources:
  git-commits:
    command: [sources/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    timeout: 300
  google-calendar-events:
    command: [sources/google-calendar.py, primary, "{{start}}", "{{end}}"]
  drive-folders:
    command: [sources/google-drive-folders.py, "{{start}}", "{{end}}"]
    mode: snapshot
```

## Contract

Every adapter follows the same rules, and `tests/test_adapters_*.py` checks them with fake provider executables.

- Python 3.14 standard library only, `#!/usr/bin/env python3`, executable bit set, no shell, no third-party import.
- Provider access goes through the provider CLI on the sanitized PATH (`gws`, `gh`, `git`) with explicit argv; the CLI owns credentials, and adapters never read credential files.
- Arguments are positional. `{{start}}` and `{{end}}` are timezone-aware ISO 8601 timestamps bounding a half-open window; other arguments are literal.
- Stdout is exactly one JSON array of records: `id` stable per source across edits, a meaningful `title`, searchable facts in `text`, event `time` with a timezone or empty when the provider gives none, `url`, explicit identities in `links` (`repo:github.com/owner/name`, `person:email/address`, provider URLs), alternate exact identities in `aliases`, and selected structured details in `attributes`.
- Completeness before output: finite pagination with a page ceiling, repeated-token detection, 16 MiB per provider page and 16 MiB of normalized output. A limit or provider failure exits 1 with nothing on stdout.
- Stderr carries one generic sentence; provider output, tokens and addresses never reach an error message.
- Only reviewed fields are projected; raw payloads never land in `text`.

## Three patterns

Git history demonstrates local collection, Calendar demonstrates a windowed provider, and Drive folders demonstrate complete snapshots with explicit parent identities. Tests cover their projections and failure boundaries using temporary repositories and fake providers. Other integrations belong to the bases that use them, together with their tests.

Configure Drive folders with `mode: snapshot`: a complete empty catalog retires earlier items from ordinary retrieval. Use the default `mode: window` for Git and Calendar. Container records use `kind: container`; `parents` contains explicit identities such as `drive:<id>`. The core exports those relationships to `indexes/structures.json`.

For a credentials-free walkthrough, copy the [runnable example base](../base/README.md).
