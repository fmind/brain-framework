# Operations — status checks, validation, evals, cache, limits, and recovery

Keep one brain healthy, verified, and fast without weakening safety boundaries or sacrificing evidence.

## Inspect health (`bf status`)

Check registration, cache freshness, note/record counts, scheduled program health, unresolved queue work, logs, and usage stats:

```bash
bf status
bf status --check
```

- `bf status` reads local state offline and reports `"registered": true` or `false` alongside `"healthy"`.
- `bf status --check` exits `1` on problems, unavailable caches, overdue or repeatedly failing/silent scheduled jobs, sources degraded three consecutive runs, unresolved assistant/queue work, or failing channels.
- Remember the runtime boundary: run `uv run --locked bf ...` only when the brain has `pyproject.toml`/`uv.lock` AND `"registered": true`.

## Validate notes and config (`bf validate`)

Verify `bf.yaml` schema `version`, OKF frontmatter (`type`, `status`), Markdown syntax, explicit identities, `?rel=` links, `relations`, and undeclared or broken targets:

```bash
bf validate
```

Run `bf validate` after every authored note or config edit. Diagnostics print one `bf:` line per issue on stderr naming file and location without quoting content.

## Measure retrieval quality (`bf eval`)

Evaluate search recall and ranking against authored test suites in `evals/*.yaml` completely offline:

```bash
bf eval
```

Update `evals/retrieval.yaml` alongside new project or concept structure to catch regressions early.

## Rebuild cache and repair state (`bf build`)

`.bf/cache.sqlite3` is a disposable local index. If replies flag `"stale": true` or report cache problems:

```bash
bf build
```

Never delete `.bf/runtime.sqlite3` on corruption or errors — `.bf/runtime.sqlite3` stores durable assistant queue jobs and deliveries. Use `bf queue` to inspect and resolve queue issues cleanly.

## Remove records safely (`bf forget`)

Retract collected records (`SOURCE:ID` or `memories/SOURCE/SHA256.json`) from `memories/` and record tombstones so sensors do not re-import them:

```bash
bf forget SOURCE:ID --dry-run
bf forget SOURCE:ID
```

Always preview with `--dry-run` first. Note that Git history still retains prior commits unless repository history itself is rewritten.

## Limits and troubleshooting quick checks

- **Incomplete search/read results**: inspect `problems`, `skipped`, `stale`, and `next_offset`; pass `--offset` for remainder slices and run `bf validate` to find skipped files.
- **Permission or symlink errors**: BF enforces confinement via `O_NOFOLLOW` and rejects symlinks, special files, control/format characters, and path escapes inside the brain root.
- **Process/input bounds**: CLI output is bounded compact JSON, search limits cap at `50` items per page (`limit` default `10`), and subprocesses kill their process group on timeout or excess output.
- **Help diagnostics**: pass `--help` to any command or run `bf guide reference` for exit codes and schemas.
