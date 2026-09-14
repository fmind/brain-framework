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

The repository's `adapters/` catalog on GitHub provides reviewed Python collectors for common providers; copy the ones you need into `sources/`. Declare a bare external executable or a helper path beginning with sources/. Arguments are direct argv, never shell-reparsed. Exact placeholders are `{{base}}`, `{{home}}`, `{{start}}`, and `{{end}}`. The executable itself is literal. Use a helper with an explicit shebang for pipelines or provider-specific logic.

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
