# Example sensors

Five standalone Python 3.12+ standard-library sensors showcasing popular CLI tools and circular harness telemetry. Copy the scripts you need into your brain's `sensors/` directory, optionally add `schemas/<source>.json` when you want stricter domain validation, and declare them under `loops:` in `bf.yaml` (or pipe stdout directly into `bf ingest SOURCE`).

| Sensor                                           | Arguments                                    | Saves                                                                              |
| ------------------------------------------------ | -------------------------------------------- | ---------------------------------------------------------------------------------- |
| `git-history.py`                                 | `ROOT [START END] [--skip RELATIVE_REPO]...` | Commits on branches, tags, and remote branches across local `git` checkouts.       |
| `github-issues.py`                               | `OWNER/REPO [START END]`                     | GitHub issues updated in `[START, END)` via `gh`, with best-effort token redaction |
| `gmail-headers.py`                               | `[START END] [--query=SEARCH]`               | Gmail message headers (`From`, `To`, `Cc`, `Subject`, labels) via `gws`.           |
| `local-documents.py`                             | `LABEL ROOT [--exclude GLOB]...`             | Snapshot of text, Markdown, CSV, HTML, PDF (`pdftotext`), and Office documents.    |
| `agent-telemetry.py`                             | `HARNESS ROOT [START END]`                   | Circular telemetry sensor turning local coding-agent JSON/JSONL logs into records. |
| [Four-tool demo](../context-hub/sensors/demo.py) | `TOOL END`                                   | Offline fixture records for the [four-tool walkthrough](../context-hub/README.md). |

## Run manually or schedule as a `bf.yaml` loop

All four windowed sensors read `BF_START`/`BF_END` automatically when `START`/`END` are omitted:

```bash
mkdir -p sensors
cp /path/to/brain-framework/examples/sensors/agent-telemetry.py sensors/

# Preview counts without writing files (stores in memories/agent-telemetry.jsonl)
python3 sensors/agent-telemetry.py claude ~/.claude/projects 2026-11-01T00:00:00Z 2026-11-15T23:59:59Z \
  | bf ingest agent-telemetry --dry-run

# Or schedule directly in bf.yaml (BF_START/BF_END are injected automatically):
# loops:
#   agent-telemetry:
#     when: 1h
#     run: [python3, sensors/agent-telemetry.py, claude, ~/.claude/projects]
#     ingest: upsert
#     # Or add a shared domain schema after creating schemas/usages.json:
#     # ingest: { source: agent-telemetry, schema: usages }
```

Omit `run:` when a script lives at `<dir>/<name>`, `<dir>/<name>.sh`, or `<dir>/<name>.py` under `sensors/`, `routines/`, or `agents/` (folder conventions locate the script only). Declare `ingest:` (`upsert`, `snapshot`, or `{source?, mode?, schema?}`) and `timeout:` explicitly in `bf.yaml` so every loop contract is visible at a glance. See [Operations](../../docs/operations.md) and [Reference](../../docs/reference.md).

## Optional domain JSON Schemas

Every ingested record is validated unconditionally against the bundled `record` schema (`bf schema record`), plus `schemas/<source>.json` (or explicit `--schema` / `ingest.schema:`) when that file exists. Add a domain schema only when you want to enforce extra fields, types, or enums.

## Provider safety and failure rules

- **Run without a shell**: every example invokes its provider CLI (`git`, `gh`, `gws`, `pdftotext`) with an explicit argument list from `/` and reaps child process groups on timeout.
- **Least-privilege subprocess env**: local converters (`pdftotext`, `git`) run with minimal environment variables and disabled git hooks/external diffs.
- **All-or-nothing batches**: on pagination overflow, malformed provider JSON, symlinks (`local-documents.py`, `agent-telemetry.py`), or CLI errors, sensors exit nonzero with empty stdout so `bf ingest` leaves existing `memories/<source>.jsonl` untouched.
- **Snapshot removal guard**: if a `--mode snapshot` run would empty a non-empty source or remove more than half of 10+ saved records, `bf ingest` aborts unless `--allow-removal` is passed explicitly.
