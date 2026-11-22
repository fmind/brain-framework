# Example sensors

Five standalone Python 3.11+ standard-library sensors showcasing popular CLI tools and circular harness telemetry. Copy the scripts you need into your brain's `sensors/` directory, add a required `schemas/<source>.json` file, and declare them under `loops:` in `bf.yaml` (or pipe stdout directly into `bf ingest SOURCE`).

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
mkdir -p sensors schemas
cp /path/to/brain-framework/examples/sensors/agent-telemetry.py sensors/
printf '{"type":"object"}\n' > schemas/agent-telemetry.json

# Preview counts without writing files (ingest telemetry into usages/ or memories/)
python3 sensors/agent-telemetry.py claude ~/.claude/projects 2026-11-01T00:00:00Z 2026-11-15T23:59:59Z \
  | bf ingest --to usages agent-telemetry --dry-run

# Or schedule directly in bf.yaml (BF_START/BF_END are injected automatically):
# loops:
#   agent-telemetry:
#     when: 1h
#     run: [python3, sensors/agent-telemetry.py, claude, ~/.claude/projects]
#     ingest:
#       target: usages
```

Scripts placed under `sensors/<name>.py` automatically imply `ingest: upsert` into `memories/<name>.jsonl` (override with `ingest: snapshot` for complete catalogs like `local-documents.py`). See [Operations](../../docs/operations.md) and [Reference](../../docs/reference.md).

## Required per-sensor JSON Schemas

Every ingested record is validated against the bundled `record` schema (`bf schema record`) plus the required `schemas/<source>.json` file and stored in `memories/<source>.jsonl`. Tighten `schemas/<source>.json` to enforce domain fields, types, or enums before `bf ingest` writes records.

## Provider safety and failure rules

- **Run without a shell**: every example invokes its provider CLI (`git`, `gh`, `gws`, `pdftotext`) with an explicit argument list from `/` and reaps child process groups on timeout.
- **All-or-nothing batches**: on pagination overflow, malformed provider JSON, symlinks (`local-documents.py`, `agent-telemetry.py`), or CLI errors, sensors exit nonzero with empty stdout so `bf ingest` leaves existing `memories/<source>.jsonl` untouched.
- **Snapshot removal guard**: if a `--mode snapshot` run would empty a non-empty source or remove more than half of 10+ saved records, `bf ingest` aborts unless `--allow-removal` is passed explicitly.
