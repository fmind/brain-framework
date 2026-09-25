# Sensors

A sensor is an executable collector that gathers records from a source. It can be any executable that prints one JSON array of [records](brain.md#records) on stdout. It owns provider access, pagination and projection; the provider's CLI owns credentials. Brain Framework runs it, validates the whole array and its [schema mappings](schema.md#shared-fields-and-sensor-mappings), and upserts the records. A failure, a timeout, invalid output or excessive output writes nothing.

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

Declare sensors in `bf.yaml`. The executable is a bare command on PATH or a `sensors/` path; arguments are passed directly, never through a shell. The placeholders `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}` are replaced once.

```yaml
# https://fmind.github.io/brain-framework/
version: 5
name: brain
sensors:
  git-commits:
    command: [sensors/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    refresh: 3600
  drive-folders:
    command: [sensors/google-drive-folders.py]
    mode: snapshot
    refresh: 86400
```

| Setting     | Default    | Meaning                                                                                     |
| ----------- | ---------- | ------------------------------------------------------------------------------------------- |
| `command`   | required   | Direct argv.                                                                                |
| `fields`    | `{}`       | Explicit schema-field mappings using JSON Pointer paths or literal values.                  |
| `enabled`   | `true`     | Disabled sensors never run; their records stay searchable.                                  |
| `mode`      | `window`   | `window` upserts what the sensor returns; `snapshot` replaces the source's whole catalog.   |
| `trust`     | `external` | `owner` for text you write yourself (your commits, notes, documents); `external` otherwise. |
| `refresh`   | `0`        | Seconds between automatic runs; `0` keeps the sensor manual.                                |
| `lookback`  | `86400`    | Seconds covered by a first run, or by every snapshot run.                                   |
| `overlap`   | `300`      | Seconds re-read before the last window's end, for late arrivals.                            |
| `timeout`   | `300`      | Seconds before the process group is killed.                                                 |
| `max_bytes` | 64 MiB     | Maximum stdout size.                                                                        |

Sensors run from the brain root with your environment minus loader-injection variables, stdin closed, and stderr captured to a private per-sensor log of at most 256 KiB under `~/.local/state/bf/`. Errors name the log, never its content. The [example sensors](https://github.com/fmind/brain-framework/tree/main/examples/sensors) show local Git history, Google Calendar, a complete Drive folder snapshot and scoped local documents; copy them into `sensors/` and adapt them with tests.

## Collect and update

Review the sensor code, then explicitly grant machine collection trust. New brains and referenced brains have no implicit execution permission.

```bash
bf register . --collect
bf collect git-commits --since 30d --dry-run   # run and show three samples, write nothing
bf collect git-commits --since 30d             # backfill a month
bf update --dry-run                            # which sensors are due, and their windows
bf update                                      # run them all, then refresh search
```

`update` follows the [brain selection rules](commands.md): it runs due sensors, then due [routines](#routines), in the selected brains that are trusted on this machine. A sensor is due when it is enabled, its `refresh` is nonzero, and that interval has elapsed since its last success. A window sensor resumes from its last collected window minus `overlap`, catching up at most 30 days after a long pause; upserts make repeated items harmless. A failed sensor stays due and never blocks the others; the command exits 1 when any failed. Run state lives in `~/.local/state/bf/`, so losing it means the next run uses `lookback` and previously recorded coverage is no longer available. A CI job keeps it between runs or uses a longer `lookback`; see [team brains](team.md#collect-in-ci).

Runs of the same sensor are serialized. Other sensors can collect concurrently; record commits and run-state updates serialize briefly. `status` separates indexed totals from `last_run` counts (added, updated, unchanged and removed), and reports disabled and historical evidence separately from enabled sources. A manual backfill does not claim coverage across an uncollected gap: one that ends before the recorded coverage keeps that coverage, the resume point and freshness unchanged, and one that touches it extends the coverage backwards without counting as a fresh success.

## Routines

A routine is a deterministic program whose output people review: a weekly review, a meeting preparation, a digest of selected feeds. Declare it under `routines:` in `bf.yaml`. Its name is the slug of the actions it writes (lowercase letters and digits separated by single hyphens) and must differ from every sensor name.

```yaml
# https://fmind.github.io/brain-framework/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
```

| Setting     | Default  | Meaning                                                                                  |
| ----------- | -------- | ---------------------------------------------------------------------------------------- |
| `command`   | required | Direct argv: a bare command on PATH or a `routines/` path, with the sensor placeholders. |
| `enabled`   | `true`   | Disabled routines never run.                                                             |
| `refresh`   | `0`      | Seconds between runs; `0` declares the routine without scheduling it.                    |
| `lookback`  | `86400`  | Seconds covered by the first run; later runs cover the time since the last success.      |
| `timeout`   | `300`    | Seconds before the process group is killed.                                              |
| `max_bytes` | 1 MiB    | Maximum stdout size, at most 4 MiB.                                                      |

`bf update` runs due routines after the sensors of each brain trusted on this machine, with the same process boundary: no shell, the brain root as working directory, loader-injection variables removed, stdin closed, a timeout and an output cap that kill the process group, and stderr in a private log of at most 256 KiB. `{{start}}` is the last success, or `lookback` before now on the first run, and `{{end}}` is now.

The routine prints one Markdown note, or nothing. Brain Framework validates it as an authored note before writing, then writes it to `actions/YYYY-MM-DD_NAME/ACTION.md` for the local date of the run. An existing folder for that day is never replaced, so a routine writes at most one action per day and never overwrites a person's edits: a later run the same day reports `skipped`. Empty output records a successful run without an action. A failure, a timeout, invalid Markdown or oversized output writes nothing, keeps the routine due, and is reported by `update`, `bf status` and the home page's `attention`.

Routines read the brain as an agent does, through `bf read` and `bf search` with literal argv. They never edit notes or contact providers, and Brain Framework runs no model: people and their agents review the action and update the owning notes. Copy an [example routine](https://github.com/fmind/brain-framework/tree/main/examples/routines) into `routines/` and test it with a fake `bf`. Preview a routine by running its script directly; `bf update --dry-run` shows which routines are due. Run state lives in `~/.local/state/bf/`; `bf status` reports each routine's last run, success, error, log and latest action.

## Define the scope before adding a sensor

Keep each sensor's contract beside its code: selected account/folders/channels, stable identity, event and modification time, projected fields, size limits, deletion behavior, trust and fake-provider tests. Declare `trust: owner` only for text the brain's owner writes, such as local Git history or your own documents; mail, chat, invitations, issues, feeds and shared catalogs stay `external`, the default. Pages show external records by title and ref without excerpts, searches and exact reads label them `external`, and sources no longer declared count as external. Prefer an explicit folder, repository or channel list over whole-account ingestion. Preserve source URLs and explicit identities so teammates can verify evidence.

Use snapshots for bounded current catalogs (contacts, folders, a rolling future agenda, selected local documents). Use windows for history (commits, messages, meetings), with a documented reconciliation horizon for mutable records. Window collection does not remove disappeared items; a snapshot does. A past-event feed cannot answer tomorrow's agenda: collect a separate future snapshot. Mark truncated content with `attributes.partial`, retain upstream modification time as `attributes.updated`, and let Brain Framework stamp `attributes.observed`.

Start with records that answer recurring questions. Add richer mail bodies, comments, document text or curated feeds only when useful, with explicit scope. Reuse provider CLIs and native file formats; keep model inference, crawling, scheduling and credential storage outside sensors and the core.

## Schedule it

Run `bf update` from a native timer. On Linux, create these two files; this example explicitly targets a registered brain named `brain`. Replace that name and the executable path if needed (`command -v bf` shows your installation):

```ini
# https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
# ~/.config/systemd/user/bf-update.service
[Unit]
Description=Collect due Brain Framework sensors

[Service]
Type=oneshot
ExecStart=%h/.local/bin/bf update --brain brain
TimeoutStartSec=45min
```

```ini
# https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html
# ~/.config/systemd/user/bf-update.timer
[Unit]
Description=Check for due Brain Framework sensors every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=1min

[Install]
WantedBy=timers.target
```

Run `systemctl --user daemon-reload`, enable it with `systemctl --user enable --now bf-update.timer`, and read runs with `journalctl --user -u bf-update`. On macOS, a launchd agent with `StartInterval` 900 checks for due sensors every 15 minutes. Give the job the PATH its sensors need (`gh`, `gws`, `git`). `bf status --check` exits 1 when a scheduled sensor has not succeeded within twice its `refresh`, which suits a monitoring check.

Check `systemctl --user list-timers bf-update.timer` for the next trigger and `systemctl --user show bf-update.service -p Result -p ExecMainStatus` after a run, then `bf status --check --brain brain` for source health. Enabling a timer alone does not prove collection succeeded. Disable future runs with `systemctl --user disable --now bf-update.timer`; stop an active collection separately with `systemctl --user stop bf-update.service`.

Choose a timer interval comfortably shorter than the smallest nonzero `refresh`. An hourly timer with random delay can run just before an hourly sensor is due and skip it until the following hour. A 15-minute check avoids that extra hour of delay; it still collects only due sensors. `Persistent=true` coalesces missed calendar triggers when the user manager returns; it does not keep a sleeping laptop running. Cache and provider failures make `update` exit 1.

## Good records

- Put the facts someone will ask about in `title` and `text`: subject, outcome, people, rationale. `attributes` are for exact reads; map shared facts into searchable `fields` through the schema.
- Keep `id` stable across edits so a changed item replaces its line.
- Use event time, never collection time, for `time`.
- Link explicit identities only: `person:email/<lowercase address>`, `repo:github.com/<owner>/<name>`, provider URLs. Never infer relationships from similar names.
- Skip noise at the source: trash, promotions, bots, test runs. A smaller, relevant corpus answers better.
- Fail before printing anything when pagination is incomplete or the provider errors, and test the sensor with a fake provider.
