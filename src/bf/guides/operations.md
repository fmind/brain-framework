# Operations — status, checks, cache, recovery, limits, and privacy

Keep one brain healthy, verified, and fast without weakening safety boundaries or losing evidence.

## Inspect health and prerequisites (`bf status`, `bf doctor`)

```bash
bf status
bf status --check
bf doctor
```

- `bf status` and `bf doctor` run offline without starting configured jobs.
- `bf status --check` exits `1` whenever `problems`, cache issues, attention entries (`failed`, `overdue`, `never` once due, `silent`, or 3 consecutive incomplete `degraded` runs), or unresolved `queue` failures/uncertain runs/active work exist.
- `bf doctor` verifies executables in `run`/`harness`/`send`/`gate`, binaries in `requires.bins`, presence of env vars in `requires.env`, and `prompt:` files.

## Validate notes and evaluate search (`bf validate`, `bf eval`)

```bash
bf validate
bf eval --baseline baseline.json
```

- `bf validate` checks `bf.yaml` (`version: 11`), OKF frontmatter (`type`, `status`), Markdown links and conflict markers, identities, `relations`, and optional `validate:` rules (`max_stale_days`, `warn_stale_days`, `index_note`, `required_status`, `forbid_patterns`).
- `bf eval` runs offline suites in `evals/*.yaml` (`version: 11`) checking `query`, `read`, `scope`, `limit`, `expect`, `forbid`, `text`, `empty`, MRR, and optional `--baseline` regression comparisons.

## Cache, `.bf/runtime.sqlite3`, and `memories/` backup & recovery

`.bf/cache.sqlite3` is a **disposable** index, whereas `memories/` (evidence) and `.bf/runtime.sqlite3` (durable runs, event cursors, wakes, deliveries, and failure incidents) are **authoritative/durable local state**:

- **Rebuild disposable search cache (`bf build`)**: if `bf status` reports `"pending_transaction": true` or cache corruption, stop writers and run `bf build && bf validate && bf status --check`. On `memories/.pending/` manifest errors, move the corrupt stray file out of the brain, rebuild, and re-collect that source. Never edit `memories/` files manually without running `bf build`.
- **Back up `memories/` + `.bf/runtime.sqlite3` cleanly**: stop active `bf watch` / updates first (or take a consistent SQLite backup before archiving `memories/` and `bf.yaml`):

```bash
mkdir -p ~/brain-backups/snapshot
sqlite3 ~/brain/.bf/runtime.sqlite3 ".backup '$HOME/brain-backups/snapshot/runtime.sqlite3'"
tar --exclude='.bf' -czf ~/brain-backups/snapshot/brain-files.tar.gz -C ~/brain .
```

- **Recover `.bf/runtime.sqlite3` (never delete on corruption)**:
  - Keep `.bf/runtime.sqlite3` and `.bf/runtime.sqlite3-journal` paired and run `bf queue` to roll back an interrupted write.
  - Restore `.bf/runtime.sqlite3` from your most recent consistent backup (with `bf watch` stopped) rather than deleting or recreating it empty.

### Quoted queue and runtime diagnostics

| Diagnostic                                                                                      | Recovery                                                                                                                             |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `runtime queue has no unresolved-work slots; clear pending work first`                          | Inspect and `--retry` or `--cancel` unresolved items in `bf queue`.                                                                  |
| `runtime queue reached its size limit; resolve or clear pending work`                           | Drain pending/failed deliveries (`MAX_PENDING` / 96 MiB reserve ceiling).                                                            |
| `runtime queue holds unresolved work under a previous path or name; resolve via bf queue first` | Restore the prior brain path/name, resolve `bf queue`, then rename or move again.                                                    |
| `runtime database predates this release; preserve it and follow upgrade steps in the changelog` | Preserve `.bf/runtime.sqlite3` and run manual steps in [CHANGELOG](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md). |
| `unexpected runtime database schema; preserve it and repair it`                                 | Preserve `.bf/runtime.sqlite3` and inspect/restore with the matching BF release.                                                     |
| `runtime database holds an interrupted write; run bf queue to recover it`                       | Keep `.bf/runtime.sqlite3` and `.bf/runtime.sqlite3-journal` together and run `bf queue`.                                            |

## Limits and safeguards

| Resource                         | Limit                                                                                                     |
| -------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `bf.yaml`, registry, eval suites | `1 MiB` each; YAML depth `<= 32`, `20,000` events                                                         |
| Authored note                    | `4 MiB`; path `<= 7,988` percent-encoded chars; `aliases`/`links`/`tags` `<= 1,000` each                  |
| Record file & `text`             | `16 MiB` file; metadata JSON `<= 2 MiB`; `text` `<= 4 MiB` (unless `max_text_bytes` cuts with `partial`)  |
| Directory scan & problem caps    | `100,000` entries/folder (`bf status` warns at `80,000`); `200` skipped files/problems reported per reply |
| Search & exact read pages        | Query `<= 4,096` chars / 32 terms; `limit` `1`–`50`; soft page cut `~32 KiB`, hard reply cap `4 MiB`      |
| Jobs, triggers & `requires`      | `<= 128` jobs/brain; `<= 32` `on` rules/job; `<= 64` `bins` / `<= 64` `env`; `timeout` `1`–`3,600s`       |
| Prompts, stdin, stdout & streams | Prompt `<= 120,000` bytes; `--stdin` `<= 1 MiB` in `10s`; stdout default `64 MiB` (max `256 MiB`)         |
| Runtime DB, wakes & retention    | DB `256 MiB`; `10,000` pending cap (`96 MiB`); `<= 16` wakes, depth `<= 8`; completed state kept `35d`    |
| Logs & usage telemetry           | `logs/NAME.log` `<= 1 MiB`; `.bf/usage.sqlite3` `<= 1 MiB` (30d daily aggregates, never queries/refs)     |

## Privacy and security boundaries

- **Local by default**: BF collects zero telemetry and runs no server. Connecting cloud model harnesses (`claude`, `codex`, etc.) sends prompt inputs to that provider.
- **No symlink or shell escapes**: `storage.Store` uses `O_NOFOLLOW` to reject symlinks and special files inside brains and `.bf/` (mode `700`).
- **Untrusted content & audience separation**: keep private and team brains in separate Git repos, authenticate inbound message senders, review pulled `bf.yaml` and script edits before running jobs, and keep raw `logs/` and `.bf/runtime.sqlite3` private. Use `uv run --locked bf` strictly on registered brains (`registered: true`).
- **Reporting issues**: report bugs with synthetic reproductions at [GitHub issues](https://github.com/fmind/brain-framework/issues) and security issues privately via [SECURITY.md](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
