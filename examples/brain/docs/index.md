# Brain Documentation

This folder holds your brain's own user documentation (compatible with [Zensical](https://zensical.org/)).

## Quick Commands

- `bf status` and `bf review` — inspect loop health, folder counts, stale notes, and open tasks.
- `bf query "terms"` and `bf query --jq '...'` — search and filter `memories/*.jsonl` via `rg` and `jq`.
- `bf read "bf:///concepts/retention.md"` — resolve a `bf://` link, section anchor, or `source:id` record (pipe JSON output into `jq`).
- `bf build && bf check` — compile Markdown notes into `memories/<dir>.jsonl`, then validate `bf.yaml`, required `schemas/*.json`, records, and links (`python3 evals/smoke.py` runs user retrieval evals).

For framework documentation and schemas, see [Brain Framework](https://fmind.github.io/brain-framework/).
