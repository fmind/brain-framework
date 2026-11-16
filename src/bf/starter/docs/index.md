# Brain Documentation

This folder holds your brain's own user documentation (compatible with [Zensical](https://zensical.org/)).

## Quick Commands

- `bf review` and `bf status` — inspect stale notes, open tasks, jobs, and servers.
- `bf query "terms"`, `bf query --jq '...'`, `bf query --sql "..."` — search and analyze `memories/**/*.json` via `rg`, `jq`, and `duckdb`.
- `bf read "bf:///concepts/welcome.md#conventions"` — resolve a `bf://` link, section anchor, or `#<jq_filter>` fragment.
- `bf action "topic"` — create or reuse `actions/YYYY-MM-DD_topic/ACTION.md`.
- `bf build && bf validate && bf eval` — compile Markdown notes into `memories/`, validate schemas and links, and run deterministic evals.

For framework documentation and schemas, see [Brain Framework](https://fmind.github.io/brain-framework/).
