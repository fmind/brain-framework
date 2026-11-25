# Runnable example brain

A fictional brain with notes (`concepts/`, `projects/`, `actions/` with `index.md` bundles), convention loops (`sensors/`, `routines/`, `agents/`), and flat JSONL records (`memories/`, `logs/`), plus optional domain JSON Schemas (`schemas/*.json`), reusable `prompts/`, and a runnable Python eval (`evals/smoke.py`) without credentials or network access.

Run on a disposable copy from the repository checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
bf_demo=$(mktemp -d)
cp -R examples/brain "$bf_demo/brain"
cd "$bf_demo/brain"
bf() { env -u BF_BRAIN uv run --project "$bf_checkout" bf "$@"; }

BF_START=2026-09-18T09:00:00Z BF_END=2026-09-18T10:00:00Z python3 sensors/demo.py | bf ingest demo
bf run usage-demo
bf build
bf check
bf review
bf run weekly-review
```

`bf build` writes `memories/_notes.jsonl` and refreshes the disposable `.bf/cache/index.sqlite3` FTS5 cache alongside `memories/demo.jsonl` and `memories/usage-demo.jsonl`, and read-only `bf check` returns `"ok":true` (`6` notes, `2` memory records).

## Query records (`bf query`, `jq`, `duckdb`)

```bash
# Ranked BM25 full-text search across notes and memories
bf query "upstream content can disappear"

# Find every note linking to a specific record (backlinks)
bf query --links-to demo:retention --format text

# Filter compiled notes with structured flags or inspect links via jq
bf query --kind note --tag retention --format text
jq 'select(.kind == "note") | .ref as $n | .links[]? | select(.to == "demo:retention") | {from_note: $n, rel: .meta.rel, to: .to}' memories/_notes.jsonl
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
