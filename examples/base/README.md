# Runnable example base

A fictional project, OKF concept, resumable task and local collector demonstrate the complete workflow without credentials or network access. Run this from the FKF checkout after `uv sync --locked`:

```bash
fkf_checkout=$PWD
fkf_example=$(mktemp -d)
cp -R examples/base/. "$fkf_example"
cd "$fkf_example"
fkf() { uv run --project "$fkf_checkout" fkf "$@"; }  # or use an installed fkf
fkf register . --collect
fkf update --dry-run
fkf update
fkf search retention
fkf read demo:retention
fkf validate
fkf eval
```

`register --collect` trusts this copy to run `sources/demo.py` on your machine; a copied base never collects until you trust it. A second update within the hour runs nothing. The fake collector emits one fictional event inside each requested window, always with the same id, so repeated runs update one line in `records/demo/`.

Continue the task under `tasks/2026-09-19_retention/`: write the answer with the record ref `demo:retention`, update TASK.md, then validate and evaluate. For real data, create your own base with `fkf init PATH` and copy the patterns you need. See the [base layout](../../docs/docs/base.md).
