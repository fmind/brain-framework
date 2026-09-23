# Collectors

A collector is any executable that prints one JSON array of [records](base.md#records) on stdout. It owns provider access, pagination and projection; the provider's CLI owns credentials. FKF runs it, validates the whole array, and upserts the records. A failure, a timeout, invalid output or excessive output writes nothing.

```json
[{
  "id": "decision-42",
  "title": "Keep historical evidence",
  "text": "The team selected durable local files because providers delete content.",
  "time": "2026-09-01T12:00:00Z",
  "url": "https://mail.example.com/decision-42",
  "links": ["repo:github.com/team/archive", "person:email/owner@example.com"]
}]
```

Declare collectors in `fkf.yaml`. The executable is a bare command on PATH or a `sources/` path; arguments are passed directly, never through a shell. The placeholders `{{base}}`, `{{home}}`, `{{start}}` and `{{end}}` are replaced once.

```yaml
# https://fmind.github.io/fkf/
version: 2
name: brain
sources:
  git-commits:
    command: [sources/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    refresh: 3600
  drive-folders:
    command: [sources/google-drive-folders.py]
    mode: snapshot
    refresh: 86400
```

| Setting     | Default  | Meaning                                                                                      |
| ----------- | -------- | -------------------------------------------------------------------------------------------- |
| `command`   | required | Direct argv.                                                                                 |
| `enabled`   | `true`   | Disabled sources never run; their records stay searchable.                                   |
| `mode`      | `window` | `window` upserts what the collector returns; `snapshot` replaces the source's whole catalog. |
| `refresh`   | `0`      | Seconds between automatic runs; `0` keeps the source manual.                                 |
| `lookback`  | `86400`  | Seconds covered by a first run, or by every snapshot run.                                    |
| `overlap`   | `300`    | Seconds re-read before the last window's end, for late arrivals.                             |
| `timeout`   | `300`    | Seconds before the process group is killed.                                                  |
| `max_bytes` | 64 MiB   | Maximum stdout size.                                                                         |

Collectors run from the base root with your environment minus loader-injection variables, stdin closed, and stderr captured to a private per-source log of at most 256 KiB under `~/.local/state/fkf/`. Errors name the log, never its content. The [example collectors](https://github.com/fmind/fkf/tree/main/examples/sources) show local Git history, Google Calendar, a complete Drive folder snapshot and scoped local documents; copy them into `sources/` and adapt them with tests.

## Collect and update

```bash
fkf collect git-commits --since 30d --dry-run   # run and show three samples, write nothing
fkf collect git-commits --since 30d             # backfill a month
fkf update --dry-run                            # which sources are due, and their windows
fkf update                                      # run them all, then refresh search
```

`update` runs every enabled source whose `refresh` elapsed since its last success, in every base trusted on this machine. A window source resumes from its last collected window minus `overlap`, catching up at most 30 days after a long pause; upserts make repeated items harmless. A failed source stays due and never blocks the others; the command exits 1 when any failed. Run state lives in `~/.local/state/fkf/`, so losing it only means the next run uses `lookback`.

Runs of the same source are serialized. Other sources can collect concurrently; record commits and run-state updates serialize briefly. `status` separates indexed totals from `last_run` counts (added, updated, unchanged and removed), and reports disabled and historical evidence separately from enabled sources. A manual backfill does not claim coverage across an uncollected gap.

## Define the scope before adding a source

Keep each collector's contract beside its code: selected account/folders/channels, stable identity, event and modification time, projected fields, size limits, deletion behavior and fake-provider tests. Prefer an explicit folder, repository or channel list over whole-account ingestion. Preserve source URLs and explicit identities so teammates can verify evidence.

Use snapshots for bounded current catalogs (contacts, folders, a rolling future agenda, selected local documents). Use windows for history (commits, messages, meetings), with a documented reconciliation horizon for mutable records. Window collection does not remove disappeared items; a snapshot does. A past-event feed cannot answer tomorrow's agenda: collect a separate future snapshot. Mark truncated content with `attributes.partial`, retain upstream modification time as `attributes.updated`, and let FKF stamp `attributes.observed`.

Start with records that answer recurring questions. Add richer mail bodies, comments, document text or curated feeds only when useful, with explicit scope. Reuse provider CLIs and native file formats; keep model inference, crawling, scheduling and credential storage outside collectors and the core.

## Schedule it

Run `fkf update` from a native timer. On Linux, a systemd user timer:

```ini
# ~/.config/systemd/user/fkf-update.service
[Unit]
Description=Collect due FKF sources

[Service]
Type=oneshot
ExecStart=%h/.local/bin/fkf update
TimeoutStartSec=45min
```

```ini
# ~/.config/systemd/user/fkf-update.timer
[Unit]
Description=Check for due FKF sources every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=1min

[Install]
WantedBy=timers.target
```

Run `systemctl --user daemon-reload`, enable it with `systemctl --user enable --now fkf-update.timer`, and read runs with `journalctl --user -u fkf-update`. On macOS, a launchd agent with `StartInterval` 900 checks for due sources every 15 minutes. Give the job the PATH its collectors need (`gh`, `gws`, `git`). `fkf status --check` exits 1 when a scheduled source has not succeeded within twice its `refresh`, which suits a monitoring check.

Choose a timer interval comfortably shorter than the smallest nonzero `refresh`. An hourly timer with random delay can run just before an hourly source is due and skip it until the following hour. A 15-minute check avoids that extra hour of delay; it still collects only due sources. `Persistent=true` coalesces missed calendar triggers when the user manager returns; it does not keep a sleeping laptop running. Cache and provider failures make `update` exit 1.

## Good records

- Put the facts someone will ask about in `title` and `text`: subject, outcome, people, rationale. `attributes` are for exact reads, not search.
- Keep `id` stable across edits so a changed item replaces its line.
- Use event time, never collection time, for `time`.
- Link explicit identities only: `person:email/<lowercase address>`, `repo:github.com/<owner>/<name>`, provider URLs. Never infer relationships from similar names.
- Skip noise at the source: trash, promotions, bots, test runs. A smaller, relevant corpus answers better.
- Fail before printing anything when pagination is incomplete or the provider errors, and test the collector with a fake provider.
