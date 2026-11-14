# Jobs, records, ingest, maps, and routines

Brain Framework configures every executable job under top-level `jobs:` in `bf.yaml` (`defaults:` sets shared `harness`, `timezone`, and `post`). Jobs run only via explicit execution commands on one selected brain.

## Your first sensor

Complete [Getting started](getting-started.md), including its [collection exercise](getting-started.md#collect-your-first-source), then work inside `~/brain`. The reviewed [local documents sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py) reads only the folder passed to it and bounds file sizes and output. Move the brief into its own folder, then copy the script from the release tag matching `bf --version`:

```bash
mkdir -p inputs/website-demo
mv inputs/brief.txt inputs/website-demo/brief.txt
curl -fsSLo sensors/local-documents.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors/local-documents.py"
chmod +x sensors/local-documents.py
```

Review the downloaded script before running it; `chmod +x` restores the executable bit BF needs to run a script directly.

### Configure a sensor {#configure-a-sensor}

In `bf.yaml`, replace the `brief` entry under `jobs:` with this one. Keep `version`, `name` and other settings:

```yaml
# https://fmind.github.io/brain-framework/docs/jobs/
jobs:
  local-documents:
    run: [sensors/local-documents.py, website-demo, "{{brain}}/inputs/website-demo"]
    emit: records
    mode: snapshot
```

`website-demo` is the stable label used in record ids, and the last argument selects the demo folder. The script needs Python 3.11 or later as `python3` ([Job settings](#job-settings)).

### Collect and read {#collect-and-read}

```bash
bf collect local-documents --dry-run
bf collect local-documents
bf search "product explanation" --scope memories/local-documents
bf read local-documents:website-demo/brief.txt
```

The dry run executes the script and previews up to three records (`added`, `updated`, `unchanged`, `removed`) without saving them. The real collection stores one record under `memories/local-documents/`, and the exact read includes:

```json
{
  "ref": "local-documents:website-demo/brief.txt",
  "record": {
    "id": "website-demo/brief.txt",
    "title": "brief.txt",
    "text": "Visitors need a clear product explanation and pricing before signing up.\n",
    "attributes": { "scope": "website-demo" }
  }
}
```

In `projects/new-website.md`, point the Evidence line beneath the decision at the new record:

```markdown
Evidence: [Product brief](local-documents:website-demo/brief.txt).
```

This edit clears the review flag on the project. Remove the retired `brief` records and tutorial script, then validate:

```bash
rm -r memories/brief sensors/brief.py
bf validate
```

Validation reports `"records":1` and `"valid":true`.

## Selected highlights

Use [`examples/sensors/highlights.py`](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py) to ingest curated passages (`@type: "Highlight"`, `url`, `relations.source-document`, `attributes.locator`, and non-FTS `attributes.annotation`) from portable highlight exports; see [`examples/sensors/README.md`](https://github.com/fmind/brain-framework/blob/main/examples/sensors/README.md) for local documents, highlights, Git history, GitHub history/issues/PRs, Google Calendar, Gmail headers, and Google Drive folders.

## Job settings

Each entry under `jobs:` names `[a-z][a-z0-9-]*` (never `bf`) and declares `run:`, `prompt:`, or `send:` (optional only when `enabled: false`):

| Field            | Default  | Role                                                                                                                |
| ---------------- | -------- | ------------------------------------------------------------------------------------------------------------------- |
| `emit`           | `log`    | Output sink: `records`, `messages`, `message`, `action`, or `log`.                                                  |
| `source`         | job name | `SOURCE` in `SOURCE:ID` and `memories/SOURCE/` (`emit: records` only; must be unique).                              |
| `mode`           | `window` | `window` (overlap/reconcile), `cursor` (`{{cursor}}`), or `snapshot` (complete catalog; `records`/`messages` only). |
| `lookback`       | `86400`  | Initial window or snapshot span before `--until` (`1s`–`365d`).                                                     |
| `overlap`        | `300`    | Seconds re-read before the previous `window` end (`0s`–`365d`).                                                     |
| `reconcile`      | none     | Periodic wider backfill `{ every, lookback }` on `mode: window` record jobs.                                        |
| `priority`       | `normal` | `low` summarizes high-volume record sources under `sources` on period pages and halves search weight.               |
| `silent_after`   | none     | Elapsed seconds without new/changed records before a scheduled source sets `"silent": true`.                        |
| `max_text_bytes` | none     | Truncates `record.text` on a UTF-8 boundary (`1`–`4,194,304`) with `attributes.partial: true`; unset fails > 4 MiB. |
| `expire_after`   | none     | Retention age (`1h`–`100y`) pruning old records during complete collections.                                        |
| `keep`           | none     | Keeps newest N records or unedited generated actions (`1`–`50,000`).                                                |
| `requires`       | `{}`     | `{ bins, env }` checked by `bf doctor` and host environment variables forwarded to the job.                         |
| `timeout`        | `300`    | Process timeout in seconds (`1`–`3,600`); kills process group on expiration.                                        |
| `max_bytes`      | 64 MiB   | stdout byte cap (`1` B–`256` MiB).                                                                                  |

- **Command paths**: argv index `0` must be a bare binary name on `PATH` or a relative path under `sensors/`, `routines/`, or `channels/` (executable bit set). Use `cwd` for a brain-relative working directory.
- **Placeholders**: `{{brain}}`, `{{home}}`, `{{state}}` (`.bf/state/JOB/`, mode `700`), `{{run}}`, `{{start}}`, `{{end}}`, `{{cursor}}`, `{{context}}`, plus `{{lines:relative/path.txt}}` (expands non-blank, non-`#` lines as separate arguments, up to 128 argv entries).

### Credentials and isolation

Subprocesses run with no shell (`shell=False`), stripped loader/startup-injection variables (`LD_*`, `PYTHON*`, `BASH_ENV`, `NODE_OPTIONS`, etc.), bounded logs (`logs/NAME.log`), and clean environment forwarding (`BF_BRAIN` + `requires.env`). Prefer credentials in order: (1) provider CLI logins (`gh`, `gws`, `gcloud`), (2) private mode-`600` files outside the brain (`"{{home}}/.config/service/token"`), (3) interactive OS keyrings. Never put secrets in `bf.yaml` or argv.

## Collect, backfill, partial coverage, and `bf forget`

```bash
bf collect JOB --since 7d --until now --dry-run
bf collect JOB --allow-removal
bf collect JOB --input sample.jsonl
bf forget SOURCE:ID --dry-run
```

- **Atomic writes & removal guard**: failed runs never change `memories/`. A `mode: snapshot` run that empties a catalog or drops more than half (>10 records) aborts unless `--allow-removal` is passed once.
- **Partial coverage**: printing `{"records": [...], "incomplete": {"missed": 1, "of": 3}, "cursor": "..."}` saves returned records without deleting missing snapshot items; 3 consecutive incomplete runs flag `"degraded"` in `attention`.
- **`bf forget`**: deletes records and records SHA-256 tombstones in `memories/SOURCE/.forgotten`.
- **Decoupled runtime**: record collection never opens `.bf/runtime.sqlite3`; committed changes touch `.bf/wakeup` so `bf watch` / runtime picks them up cleanly.

## Direct ingestion (`bf ingest`)

Push JSON arrays, JSON Lines, or `{records, incomplete, cursor}` payloads from stdin or files directly into `memories/SOURCE/` without a scheduled `run:` command:

```bash
python3 exporter.py | bf ingest alerts --dry-run
bf ingest alerts --input /tmp/alerts.jsonl --snapshot
```

`bf ingest` validates records, applies `map:`, `expire_after`, and `keep` if a matching `emit: records` job targets that `source`, enforces `--allow-removal` when `--snapshot` shrinks a catalog, pings `.bf/wakeup`, and never opens `.bf/runtime.sqlite3`.

## Declarative `map:` and reprojection

When a CLI already outputs clean JSON or JSONL, declare RFC 6901 JSON Pointers under `map:` (`items`, `cursor`, `id`, `@type`, `title`, `text`/`body`, `url`, `created`/`time`, `updated`, `relations`, `attributes`, `{path, prefix, lower}` transforms, and `/*/` array expansion) instead of writing a Python script:

```yaml
jobs:
  github-issues:
    run: [gh, issue, list, --repo, OWNER/REPO, --json, number, title, body, state, url, updatedAt]
    emit: records
    mode: snapshot
    map:
      id: { path: /number, prefix: "OWNER/REPO/issues/" }
      "@type": Issue
      title: /title
      text: /body
      url: /url
      updated: /updatedAt
```

Re-validate already-stored records after updating `bf.yaml` `fields:` rules via `bf build --reproject JOB [--dry-run]`.

## Deterministic routines (`bf run`)

Routines use `run:` with `emit: log` (default) or `emit: action` (printing validated `draft` OKF `ACTION.md`):

```bash
bf run weekly-review -- --flag
bf run --hook pre-commit
```

Pass `--` before routine arguments starting with `-`. Copy reviewed routines from [`examples/routines/`](https://github.com/fmind/brain-framework/tree/main/examples/routines): `weekly-review.py`, `evidence-health.py` (reports broken links, stale `sources`, and source health), and `bf validate` wired to `on: { hook: pre-commit }`.
