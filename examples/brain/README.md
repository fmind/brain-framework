# Runnable example brain

A fictional project, OKF concepts, resumable actions, `attr_list` metadata, user JSON Schemas (`schemas/*.json`), deterministic evals (`evals/smoke.yaml`), `/bf` skill (`skills/bf/SKILL.md`), and local `sensors/`, `routines/`, `channels/`, `prompts/`, `channels:`, and `services:` entries demonstrate the end-to-end workflow without credentials or network access.

Run on a disposable copy from the repository checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
bf_demo=$(mktemp -d)
cp -R examples/brain "$bf_demo/brain"
cd "$bf_demo/brain"
bf() { uv run --project "$bf_checkout" bf "$@"; }

python3 sensors/demo.py 2026-09-18T09:00:00Z 2026-09-18T10:00:00Z | bf ingest demo
bf build
bf validate
bf eval
bf review
bf run weekly-review
```

`bf validate` returns `"ok":true` (`6` OKF notes, `1` memory record), and `bf build` writes individual JSON files under `memories/{concepts,projects,actions}/*.json` alongside `memories/demo/<sha256>.json`.

## Query pure JSON records (`bf query`, `rg`, `jq`)

```bash
# Full-text search across all JSON records via rg
bf query "upstream content can disappear"

# Query heading attr_list metadata (## Intention {#intention owner=maintainer state=waiting}) via jq
bf query --jq '. as $r | .headings[]? | select(.meta.state == "waiting") | {ref: $r.ref, id, title, meta}'

# Query keyed attr_list map directly
bf query --jq 'select(.attrs.intention != null) | {ref, intention: .attrs.intention}'

# Find notes citing memories/demo/8e220124401da6e1a0288a86b48eb316d254cd2a9b9399cfc9a07a2fbc0253e8.json with relation metadata via jq
bf query --jq 'select(.kind != "memory") | .ref as $n | .links[]? | select(.to == "memories/demo/8e220124401da6e1a0288a86b48eb316d254cd2a9b9399cfc9a07a2fbc0253e8.json") | {from_note: $n, rel: .meta.rel, to: .to}'
```

## Share a draft concept with an isolated team brain

`actions/2026-09-25_retention-review/outputs/shared-procedure.md` is a self-contained draft concept without private source-brain citations. Copy it into a fresh brain and validate:

```bash
bf init "$bf_demo/team" --name example-team
cp actions/2026-09-25_retention-review/outputs/shared-procedure.md "$bf_demo/team/concepts/selected-evidence.md"
bf build --brain "$bf_demo/team"
bf validate --brain "$bf_demo/team"
```

Clean up when finished:

```bash
cd "$bf_checkout"
unset -f bf
rm -rf -- "$bf_demo"
```
