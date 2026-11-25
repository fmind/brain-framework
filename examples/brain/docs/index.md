# Brain Documentation

This folder holds your brain's own user documentation (compatible with [Zensical](https://zensical.org/)).

## Quick Commands

- `bf status` and `bf review`: inspect loop health, folder counts, stale notes, and open tasks.
- `bf query "terms"`: ranked BM25 full-text and structured search (`--kind`, `--scope`, `--type`, `--status`, `--tag`, `--since`) over `.bf/cache/index.sqlite3` and `memories/*.jsonl`.
- `bf read "bf:///concepts/retention.md"`: resolve a `bf://` link, section anchor, or `source:id` record (pipe JSON output into `jq`).
- `bf build && bf check`: compile Markdown notes into `memories/_notes.jsonl`, then validate `bf.yaml`, optional `schemas/*.json`, records, and links (`python3 evals/smoke.py` runs user retrieval evals).

For framework documentation and schemas, see [Brain Framework](https://fmind.github.io/brain-framework/).
