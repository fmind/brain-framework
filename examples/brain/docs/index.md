# Brain Documentation

This folder holds your brain's own user documentation (compatible with [Zensical](https://zensical.org/)).

## Quick Commands

- `bf status` and `bf review` — inspect unit health, folder counts, stale notes, and open tasks.
- `bf query "terms"` and `bf query --jq '...'` — search and filter `memories/**/*.json` via `rg` and `jq`.
- `bf read "bf:///concepts/retention.md"` — resolve a `bf://` link, section anchor, or `source:id` record (pipe JSON output into `jq`).
- `bf check` — compile Markdown notes into `memories/`, validate schemas and links, and run deterministic evals.

For framework documentation and schemas, see [Brain Framework](https://fmind.github.io/brain-framework/).
