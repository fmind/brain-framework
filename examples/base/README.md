# Runnable example base

A fictional project, OKF concept, resumable task and local collector demonstrate the complete workflow without credentials or network access. Run these commands from the FKF checkout; `uv sync --locked` prepares the framework first.

```bash
fkf_example=$(mktemp -d)
cp -R examples/base/. "$fkf_example"
uv run fkf update --base "$fkf_example" --dry-run
uv run fkf update --base "$fkf_example"
uv run fkf validate --base "$fkf_example"
uv run fkf context retention --base "$fkf_example"
uv run fkf read demo:retention --base "$fkf_example"
uv run fkf eval --base "$fkf_example"
```

Use a fresh destination for each independent demo. A second update inside the hour runs no collector and keeps a ready index. The fake collector emits one fictional event inside each requested window, with the same stable identity. It does not represent a live service.

Continue the task under `tasks/2026-09-19_retention/`. Write its answer with the exact captured `ref`, update TASK.md, then rebuild and evaluate. Knowledge edits require an explicit build before search; direct Markdown and capture-file reads remain available for recovery.

This checked-in id identifies the fictional example. For real personal or team data, create a separate identity with `fkf init PATH` and copy the patterns you need. The framework tests this example from a disposable copy through the public CLI. In your own base, put collector and maintenance tests with synthetic fixtures under `tests/`; `fkf init` creates that directory. These tests are durable base code and never enter retrieval. See the [base layout](../../docs/docs/base.md) for the complete convention.
