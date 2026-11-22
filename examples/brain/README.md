# Runnable example brain

A fictional 3x3 brain with notes (`concepts/`, `projects/`, `actions/` with `index.md` bundles), convention loops (`sensors/`, `routines/`, `agents/`), and JSONL records (`memories/`, `usages/`, `logs/`), plus required per-source JSON Schemas (`schemas/*.json`), reusable `prompts/`, and a runnable Python eval (`evals/smoke.py`) without credentials or network access.

Run on a disposable copy from the repository checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
bf_demo=$(mktemp -d)
cp -R examples/brain "$bf_demo/brain"
cd "$bf_demo/brain"
bf() { uv run --project "$bf_checkout" bf "$@"; }

BF_START=2026-09-18T09:00:00Z BF_END=2026-09-18T10:00:00Z python3 sensors/demo.py | bf ingest demo
bf run usage-demo
bf build
bf check
bf review
bf run weekly-review
```

`bf build` writes flat JSONL files (`memories/{concepts,projects,actions}.jsonl`) alongside `memories/demo.jsonl` and `usages/usage-demo.jsonl`, and read-only `bf check` returns `"ok":true` (`6` notes, `1` memory record, `1` usage record).

## Query JSONL records (`bf query`, `rg`, `jq`)

```bash
# Full-text search across all JSONL records via rg
bf query "upstream content can disappear"

# Query heading attr_list metadata (## Intention {#intention owner=maintainer state=waiting}) via jq
bf query --jq '. as $r | .headings[]? | select(.meta.state == "waiting") | {ref: $r.ref, id, title, meta}'

# Find notes citing demo:retention with relation metadata via jq
bf query --jq 'select(.kind == "note") | .ref as $n | .links[]? | select(.to == "demo:retention") | {from_note: $n, rel: .meta.rel, to: .to}'
```

## Share a draft concept with an isolated team brain

`actions/retention-review/outputs/shared-procedure.md` is a self-contained draft concept without private source-brain citations. Copy it into a fresh brain, compile, and validate:

```bash
bf init "$bf_demo/team" --name example-team
cp actions/retention-review/outputs/shared-procedure.md "$bf_demo/team/concepts/selected-evidence.md"
bf build --brain "$bf_demo/team"
bf check --brain "$bf_demo/team"
```

Clean up when finished:

```bash
cd "$bf_checkout"
unset -f bf
rm -rf -- "$bf_demo"
```
