# FKF adapters

Reviewed, standalone collectors for common providers. Copy the ones you need into your base's `sources/`, declare them in `fkf.yaml`, and review them like any other code you run. They are distributed from this repository like `skills/`; the Python wheel neither bundles nor installs them.

| Adapter                   | Arguments                       | Records                                                                                                            |
| ------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `task-folders.py`         | `TASKS_ROOT START END`          | Full TASK.md bodies and checksummed input/output metadata for updated task folders.                                |
| `chrome-bookmarks.py`     | `PROFILE BOOKMARKS [START END]` | One explicitly selected bookmark file, including empty folders and parent identities.                              |
| `google-drive-folders.py` | `[START END]`                   | Complete user-visible folder catalog with parent identities; window is ignored.                                    |
| `google-gmail-labels.py`  | `[START END]`                   | Complete label catalog; window is ignored and label names do not imply parent edges.                               |
| `git-history.py`          | `ROOT START END`                | Commits of every Git checkout one or two levels below `ROOT`.                                                      |
| `google-calendar.py`      | `CALENDAR START END`            | Events of one calendar, including cancellations and all-day boundaries.                                            |
| `google-gmail.py`         | `START END [QUERY...]`          | Mail headers and snippets in the window; extra words extend the Gmail search query.                                |
| `google-tasks.py`         | `START END`                     | Tasks of every list updated in the window, including completed ones.                                               |
| `google-contacts.py`      | `START END`                     | The owner's contacts; the window is ignored because contacts are a list.                                           |
| `google-meeting-notes.py` | `START END [NAME]`              | Drive documents named like meeting notes, with their exported text.                                                |
| `google-chat.py`          | `START END`                     | Chat messages posted in the window across the spaces you belong to.                                                |
| `github-search.py`        | `issues\|prs ROLE START END`    | Issues or pull requests updated in the window; `ROLE` is assignee, author, involves, mentions or review-requested. |
| `github-notifications.py` | `START END`                     | Notifications updated in the window.                                                                               |
| `agent-sessions.py`       | `START END [ROOT]`              | Coding-agent sessions with a user turn in the window, with your prompts.                                           |

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
  github-pull-requests:
    command: [sources/github-search.py, prs, involves, "{{start}}", "{{end}}"]
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

## Source structure and tasks

Configure folder, label and bookmark catalogs with `mode: snapshot`; their bounded collectors must finish completely before output. A complete empty catalog retires earlier items from ordinary retrieval. Keep windowed task, event and message sources in the default `mode: window`.

Container records use `kind: container`. `parents` holds explicit container identities; Gmail messages reference `gmail-label:<id>`, meeting notes reference `drive:<id>`, and bookmarks reference their selected profile and folder id. The core exports this structure to `indexes/structures.json` during build.

Task capture reads `TASK.md` verbatim into `text`, records its artifact paths, sizes and SHA-256 checksums in `attributes.files`, and leaves task inputs and outputs in their authored folders. Artifact bodies are covered by the base recovery policy, not embedded in the task record. Chrome captures bookmark metadata and URLs, never the pages behind those URLs. Sources remain responsible for their declared body projection; Gmail captures headers and snippets, meeting notes capture exported text, and Tasks capture task notes.
