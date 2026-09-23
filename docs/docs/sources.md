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

Collectors run from the base root with your environment minus loader-injection variables, stdin closed, and stderr captured to a private per-source log of at most 256 KiB under `~/.local/state/fkf/`. Errors name the log, never its content. The [example collectors](https://github.com/fmind/fkf/tree/main/examples/sources) show local Git history, a windowed Google Calendar and a complete Drive folder snapshot; copy them into `sources/` and adapt them with tests.

## Collect and update

```bash
fkf collect git-commits --since 30d --dry-run   # run and show three samples, write nothing
fkf collect git-commits --since 30d             # backfill a month
fkf update --dry-run                            # which sources are due, and their windows
fkf update                                      # run them all, then refresh search
```

`update` runs every enabled source whose `refresh` elapsed since its last success, in every base trusted on this machine. A window source resumes from its last collected window minus `overlap`, catching up at most 30 days after a long pause; upserts make repeated items harmless. A failed source stays due and never blocks the others; the command exits 1 when any failed. Run state lives in `~/.local/state/fkf/`, so losing it only means the next run uses `lookback`.

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
Description=Collect due FKF sources hourly

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
```

Enable it with `systemctl --user enable --now fkf-update.timer` and read runs with `journalctl --user -u fkf-update`. On macOS, a launchd agent with `StartInterval` 3600 runs the same command. Give the job the PATH its collectors need (`gh`, `gws`, `git`). `fkf status --check` exits 1 when a scheduled source has not succeeded within twice its `refresh`, which suits a monitoring check.

## Good records

- Put the facts someone will ask about in `title` and `text`: subject, outcome, people, rationale. `attributes` are for exact reads, not search.
- Keep `id` stable across edits so a changed item replaces its line.
- Use event time, never collection time, for `time`.
- Link explicit identities only: `person:email/<lowercase address>`, `repo:github.com/<owner>/<name>`, provider URLs. Never infer relationships from similar names.
- Skip noise at the source: trash, promotions, bots, test runs. A smaller, relevant corpus answers better.
- Fail before printing anything when pagination is incomplete or the provider errors, and test the collector with a fake provider.
