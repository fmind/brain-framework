# GitHub history for selected repositories

Keep one year of commits reachable from `main`, and all available issues and pull requests regardless of age or state. Backfill once, then collect changes. This policy bounds the initial commit history; it does not delete records when they turn one year old.

## Configure one repository

Install and authenticate [GitHub CLI](https://cli.github.com/manual/) for the intended account. Copy `github-history.py` from the release tag matching `bf --version` into your brain's `sensors/` and make it executable, as [Example sensors](README.md) shows. It uses only Python 3.11's standard library and `gh`; the package does not install it. Run the following commands inside that brain.

Replace the fictional `example/project` in these entries with the selected repository, then merge them into `bf.yaml` under its existing `sensors` mapping:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  github-commits:
    command: [sensors/github-history.py, example/project, commits, "{{start}}", "{{end}}"]
    mode: window
    lookback: 31536000 # 365 days for initial collection
    refresh: 3600
    reconcile: { refresh: 604800, lookback: 31536000 }
  github-issues:
    command: [sensors/github-history.py, example/project, issues, "{{start}}", "{{end}}"]
    mode: window
    refresh: 3600
    reconcile: { refresh: 86400, lookback: 604800 }
  github-pulls:
    command: [sensors/github-history.py, example/project, pulls, "{{start}}", "{{end}}"]
    mode: window
    refresh: 3600
    reconcile: { refresh: 86400, lookback: 604800 }
```

Each source belongs to one repository. Use distinct sensor names for additional repositories. The branch defaults to literal `main`, including commits reachable through merged branches; it is not a first-parent-only log. Append `--branch, master` to the commit command for a repository whose selected branch is `master`. A missing branch fails visibly rather than falling back to another branch.

## Backfill, then refresh

Preview the scope before saving. These previews contact GitHub but do not change collected records:

```bash
bf collect github-commits --since 365d --dry-run
bf collect github-issues --since 1970-01-01 --dry-run
bf collect github-pulls --since 1970-01-01 --dry-run
```

Expected result: JSON with `records`, `samples`, requested dates, `output_bytes` and `elapsed_seconds`. Commit samples belong to `main` and the requested year; issues and PRs include old and closed objects when present. Counts depend on the repository. Previews do not establish a historical version of an object: the API returns its current content.

Collect the same scopes:

```bash
bf collect github-commits --since 365d
bf collect github-issues --since 1970-01-01
bf collect github-pulls --since 1970-01-01
bf status
bf validate
bf read memories/github-issues
```

Expected result: collection reports added, updated and unchanged counts; validation reports no problems; the read lists saved issue refs. Read a listed ref to verify its complete description. An empty source has no listed refs. A successful replay uses the same IDs and does not duplicate records.

For a large repository, split the requested interval into adjacent monthly windows with `--since` and `--until`. Keep the commit start within the selected 365-day backfill. For issues and PRs, start at the earliest desired date without a one-year cutoff. For example:

```bash
bf collect github-issues --since 2020-01-01 --until 2020-02-01
bf collect github-issues --since 2020-02-01 --until 2020-03-01
```

Expected result: each successful window adds or updates issues whose **latest modification** falls in that interval. An issue created in 2020 but edited in 2026 appears in the 2026 window instead. Complete adjacent windows through the present before claiming the backfill finished: windows that end before now extend the source's coverage without making it fresh, so `bf status` reports it `never` or `overdue` until a collection reaches the present. Finish with a window that starts where the previous one ended and has no `--until`. Keep a private checklist of the source, repository, branch, fixed interval endpoints and successful replies; resume the first unfinished interval. On failure, retry that interval or split it further. Do not skip it or raise limits to hide incomplete pagination. Earlier successful intervals remain saved.

Existing `bf watch` or `bf update` runs then collect incremental windows. The weekly commit reconciliation revisits the year to catch older commits newly merged into `main`; daily issue/PR reconciliation revisits seven days of modifications. Run `bf update --dry-run` to inspect planned work without contacting GitHub. Keep one collection owner per source: machine-local run state does not travel with synced records, and automatic catch-up is capped at 30 days. Backfill longer gaps explicitly.

## Evidence and limits

- Commits retain their message, SHA, author name, committer date and repository link. No diffs or file contents are copied; bot commits are included when reachable from the selected branch.
- Issues and PRs retain title, description, creation and modification dates, state, canonical URL and repository link; PR state distinguishes merged from closed. All ages and states are eligible. Comments, reviews, attachments, patches, CI logs and timeline events are outside this projection.
- The [repository issues endpoint](https://docs.github.com/en/rest/issues/issues#list-repository-issues) includes PRs with their merge time, so one request lists 100 issues and PRs. The adapter separates them and selects by modification time. The [commits endpoint](https://docs.github.com/en/rest/commits/commits#list-commits) uses the explicit branch and dates. Output windows are half-open: `START <= time < END`, using modification time for issues/PRs.
- There is one current record per upstream identity, not a revision archive. An old record can be updated later. Missing/deleted/private-inaccessible objects cannot be reconstructed, and window mode does not remove previously saved records. Pagination is not a transaction: each page of issues and PRs restarts after the last modification time read, so an object modified during a run moves to a later window instead of hiding an unread one. When more than 100 objects share two seconds of modification time, those are paged by number instead, and a concurrent change can still hide one of them from that run.
- Each run allows at most 100 API pages of 100 items, so at most 10,000 commits or 10,000 listed issues and PRs, 10,000 output records and 16 MiB of JSON; each provider call allows 60 seconds and 16 MiB. BF's configured `timeout`, 300 seconds by default, also bounds the whole run: a manual backfill of a busy repository that fails on its timeout can instead be split into shorter windows, or run with a larger `timeout` (at most 3600) for that sensor. Issue and PR runs each scan the combined issue/PR endpoint, so the page bound includes both kinds.
- Limits, malformed pages or pagination, repeated identities and provider errors fail without printing a partial array or changing evidence. Narrow the interval after overflow; fix account access or rate limits before retrying a provider failure. An object listed again inside the window with another modification time changed during the run: retry that window. A listed PR without its merge time (`pull_request.merged_at`) fails rather than being saved as closed.
- A scheduled run beyond these limits keeps failing. BF records a reconciliation only when it succeeds, so every later due run requests the whole `reconcile.lookback` again, and the regular hourly refresh collects nothing until one succeeds; `bf status` reports the failure. The weekly commit reconciliation above revisits 365 days: when the branch receives more than about 10,000 commits a year, shorten `reconcile.lookback` and the initial `lookback`, for example to 90 days (`7776000`), and backfill older months with explicit windows.
- `lookback: 31536000` sets the first scheduled commit window; an explicit manual `--since` can request more. Retention is separate: this example never prunes old records. Measure saved file count, disk usage, cache build time and retrieval usefulness before widening scope.

## Test without GitHub

From the framework checkout:

```bash
uv run --locked pytest -q tests/test_adapters_github.py tests/test_adapters_limits.py
```

Expected result: passing tests with fake GitHub responses, including an old closed issue updated today, merged and closed PRs, an issue modified during collection that hides no other, branch selection, pagination and a failed second page that leaves saved evidence unchanged. These are fictional fixtures, not live account or completeness proof.
