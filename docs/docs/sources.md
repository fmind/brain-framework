# Sources are commands

The core expects one JSON array of normalized records on stdout. Adapters own provider access, finite pagination, field projection and completeness. They should fail before printing partial results. Credentials belong to the provider CLI and must never enter base configuration or evidence.

```json
[{
  "id": "decision-42",
  "title": "Keep historical evidence",
  "text": "The team selected durable local files.",
  "time": "2026-09-01T12:00:00Z",
  "links": ["repo:example/project"]
}]
```

The repository's `examples/sources/` provides three reviewed patterns: local Git history, windowed Calendar events and complete Drive folder snapshots; copy the ones you need into `sources/`. Declare a bare external executable or a helper path beginning with sources/. Arguments are direct argv, never shell-reparsed. Exact placeholders are `{{base}}`, `{{home}}`, `{{start}}`, and `{{end}}`. The executable itself is literal. Use a helper with an explicit shebang for pipelines or provider-specific logic.

```bash
# Review fkf.yaml and the selected adapter before running it.
fkf collect activity 2026-09-01T00:00:00Z 2026-09-02T00:00:00Z \
  --base ~/knowledge --preview
```

Preview executes the currently configured command. Successful collection validates the whole array, requires unique ids and meaningful titles, and writes one immutable content-addressed capture. It does not automatically rebuild the index; invoke `fkf build` after a collection batch.

The default timeout is 120 seconds; configured limits are 1–3600 seconds and 1–16 MiB of output per stream. Commands execute from `/` with startup/loader variables removed and PATH/home/config roots sanitized. Timeout, cancellation, nonzero exit or excessive output leaves no partial capture and terminates descendants.

Configuration and adapter edits take effect on the next collection. There is no separate approval step or change-detection registry, and adapters run with your user permissions. Scheduling and source health probes belong to the base's maintenance commands.

## Useful record projections

Adapters should emit a descriptive title and factual text that lets a reader understand the record without opening the provider. Put the relevant subject, outcome or status, and source-provided rationale in searchable text. Preserve provider timestamps without substituting collection time for an unknown event time. Avoid generic titles that contain only a run or event id when the provider supplies a useful subject.

`attributes` retains selected structured details for exact reads; it is not indexed. Deliberately project facts needed for retrieval into `title` and `text`. Do not dump arbitrary attributes, credentials, or unnecessary personal data into text. Preserve supplied URLs and explicit repository, document or event identities in `links` and `aliases`; never infer relationships from names or similar prose.

Use a stable `id` within each source across edits and cancellations. Captures of that source/id are observations of the same record, so ordinary retrieval uses the latest known capture and `--history` exposes older ones. Append a new capture after an authorized collection; never rewrite existing records to improve their projection. Historical evidence remains exact and independently readable.

Test projections with synthetic provider data: a useful subject and body, missing optional fields, cancellation or changed status, event time versus modification time, explicit links, and bounded pagination failures. Assert that important facts reach text, not just attributes.

## Incremental refresh

`fkf update --base PATH --dry-run` plans without running commands or writing an index. `fkf update --base PATH` runs only enabled sources with a positive `refresh` interval whose latest successful automatic capture is due, then builds if stale. Sources default to `refresh: 0` (manual only). The interval, `lookback` (first window, default 86400) and `overlap` (default 300) are seconds, validated in `fkf.yaml`; source-only local overrides have the same precedence as command settings.

```yaml
# https://fmind.github.io/fkf/
version: 1
id: aabbccddeeff00112233445566778899
name: knowledge
sources:
  activity:
    command: [sources/activity.py, "{{start}}", "{{end}}"]
    refresh: 3600
    lookback: 86400
    overlap: 300
```

A windowed source resumes at its last successful automatic window end minus overlap; a snapshot source requests its configured lookback each time and must return the complete catalog. Failed collections do not advance checkpoints. Other due sources may succeed, and the command exits 1 if any collection failed. No transaction spans a whole batch. Concurrent writers fail rather than corrupting evidence; configure the scheduler to avoid overlapping runs. Captures produced by `update` carry `automatic: true`; manual `collect` captures default to false and never advance or delay automatic progress. Durable automatic captures supply checkpoints, so deleting `.fkf/` loses no progress. Existing unmarked captures remain unchanged and do not seed automatic progress: the first automatic run uses the configured lookback. A collection records an observation even when its records are unchanged. A run with no due sources and a current index writes nothing.

Overlap catches only late arrivals inside the requested window. Provider adapters that query event time cannot promise complete edit/deletion detection; use an explicit wider `collect` window for reconciliation. A long outage can exceed provider limits: reduce the source scope or adjust the adapter to handle the outstanding window before resuming. Manual backfills retain evidence but do not declare an automatic gap covered. Configure a suitable lookback for the first run and review the dry-run before enabling automation.

A base-owned cron, systemd timer, launchd job or team scheduler invokes `fkf update --base /absolute/base`. FKF installs no schedule and upgrades no software. For example, an hourly cron job can call a reviewed `scripts/update.sh` containing `exec /absolute/path/to/fkf update --base /absolute/base`. Give the job the provider CLI environment it needs. Use the scheduler's logs or redirect compact receipts into rotated, private `logs/`; do not record provider output. Prefer one scheduler invocation at a time.

## Shared identities

Use the same literal identity across adapters: `person:email/<lowercase-address>` (UTF-8 percent-encoded, preserving `/ : @ +`, with `~` encoded), `repo:github.com/<lowercase-owner>/<lowercase-repo>`, and provider item ids. Mail, contacts, calendar participants, Git authors, chat senders and document owners can therefore share an email edge. Only source-provided fields establish relationships; no name matching or guessed email-to-GitHub-account mapping occurs. Query an identity directly with `fkf find 'person:email/person@example.invalid'`. The SQLite `edges`, `aliases` and `memberships` tables are derived views of this evidence, not a separate authoritative graph.
