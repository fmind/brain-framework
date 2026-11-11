---
icon: lucide/list-checks
description: Validate saved files and test that recurring questions still retrieve the expected evidence.
---

# Check your brain

Run three offline checks inside your brain to verify structure, retrieval quality and local runtime health:

| Command             | Verifies                                                                              |
| ------------------- | ------------------------------------------------------------------------------------- |
| `bf validate`       | OKF notes, links, relations, identities, record files and configured program paths.   |
| `bf eval`           | Saved `evals/*.yaml` questions still retrieve expected refs and text.                 |
| `bf status --check` | Cache, scheduled sensors/routines, assistants and channels need no local `attention`. |

## Validate

`bf validate` returns `"valid":true` or up to 200 `problems` (`{file, error}`, then `"problems_truncated":true`), plus cross-brain `unresolved` targets (which do not fail validation; [across brains](link-reference.md#across-brains)):

- **Notes and actions**: OKF frontmatter (`type`, `status`, `updated`), valid Markdown/URI syntax, no merge conflict markers, and `actions/YYYY-MM-DD_topic/ACTION.md` folder naming.
- **Links and relations**: local targets, `#section` anchors, case-sensitive `SOURCE:ID` refs, declared `?rel=` relations on `bf://` URIs, `fields:` placement and `targets` prefix bounds.
- **[Identities](link-reference.md#identity-rules)**: own-brain namespace, whole-subject rule (rejecting `#section`, `.md` paths, pages, `SOURCE:ID` and `bf://NAME/actions/SLUG` action folders), no duplicate owners (`ambiguous identity`) and no invisible format characters.
- **Records and filesystem**: `<sha256-id>.json` filenames, record schemas, current `fields:` types, and rejection of symlinks, special files or invalid UTF-8 names.
- **Enabled programs**: executable scripts (or existing `sensors/`/`routines/`/`channels/` script arguments), `{{lines:...}}` files (<= 128 total args) and assistant `prompt` files (<= 120,000 bytes).
- **Optional `validate:` policy in `bf.yaml`**: `max_stale_days`, `warn_stale_days`, `index_note` (e.g. `concepts/index.md`), `required_status` and `forbid_patterns` (up to 64 regexes).

## Warnings

`warnings` (up to 200, then `"warnings_truncated":true`) never fail `bf validate`:

| Warning                                                    | Action                                                                                                    |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `record links are not namespaced identities or URLs`       | Fix the sensor's `links` or `url` and re-collect ([backfill](sensors.md#backfills-and-coverage) windows). |
| `an interrupted write left this temporary file; delete it` | Remove the leftover `.write-*` file.                                                                      |
| `stale_after is within N days`                             | Review `file` and advance `stale_after` (emitted when `validate.warn_stale_days` is set).                 |
| `identities differ only by letter case`                    | Pick one spelling (lowercase in reviewed sensors) and update notes or re-collect.                         |

## Retrieval cases

`bf init` writes `evals/retrieval.yaml` (`"score":"3/3"`). Add project suites beside it, such as `evals/new-website.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/checks/
version: 8
cases:
  - name: find-the-reason
    query: visitors clear explanation
    expect: [projects/new-website.md#decision]
    text: [visitors need a clear explanation before signing up]
  - name: find-the-next-action
    query: Draft product page
    expect: [projects/new-website.md#next-actions]
    text: [Draft the product page]
  - name: read-the-reason
    read: projects/new-website.md#decision
    text: [visitors need a clear explanation before signing up]
  - name: avoid-an-unrelated-answer
    query: bfabsentevidence9c4f2a7d
    empty: true
```

```bash
bf eval --path evals/new-website.yaml
```

See the [retrieval example](https://github.com/fmind/brain-framework/tree/main/examples/retrieval) for scoped, tagged and competing-answer suites.

## Track ranking changes

Search cases with `expect` report each ref's 1-based `rank` (within the top 50) and suite `mrr`. Compare against a saved reply before reorganizing notes or upgrading:

```bash
bf eval --path evals/new-website.yaml > ~/new-website-eval.json
bf eval --path evals/new-website.yaml --baseline ~/new-website-eval.json
```

`--baseline` (up to 16 MiB) lists `regressions` and `improvements` matched by suite and case `name`; `bf eval` exits 1 only when a case fails, not on rank shifts alone.

## Suite reference

Each `.yaml`/`.yml` suite (up to 100 suites of 200 cases; `bf schema --kind eval`) sets `version: 8` and `cases`:

| Field               | Rule                                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------------------- |
| `name`              | Unique nonblank identifier within the suite.                                                          |
| `query`             | Search terms, with optional `scope` (whole notes/folders/periods/identities/tags) and `limit` (1–50). |
| `read`              | Exact ref or page (`""` reads Home); mutually exclusive with `query`.                                 |
| `expect` / `forbid` | Refs or `bf://NAME/...` URIs required or forbidden (whole-note `expect` matches any section of it).   |
| `text`              | Case-insensitive substrings required in titles/excerpts (`query`) or assembled text (`read`).         |
| `empty: true`       | Asserts zero returned items (use a single distinctive token for negative search cases).               |

Incomplete replies (`problems`, `stale`) fail every case, including `empty: true`.
