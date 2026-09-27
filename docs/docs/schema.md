# Record schema

<span id="configuration-schema"></span>

Use this page to define shared record fields and sensor mappings. The [link reference](link-reference.md) owns address syntax and relationship links. Start with [Linking knowledge](links.md) for a worked example, or [Configuration](configuration.md) to select brains.

The [generated JSON Schema](../bf.schema.json) describes `bf.yaml`. To inspect the schema for your installed version:

```bash
bf schema
```

The loader uses the same strict models. Contributors regenerate the checked-in schema with `mise run generate:schema`.

A minimal configuration declares a format version and stable brain name:

```yaml
# https://fmind.github.io/brain-framework/
version: 6
name: brain
```

Add [sensors](sensors.md), [routines](routines.md), shared `schema` fields and related `brains` only as needed. Unknown keys, duplicate keys, anchors and aliases are rejected. Sensor and routine names must be distinct; routine names are action slugs: lowercase letters and digits separated by single hyphens, starting with a letter.

A reference such as `brains: {team-brain: {path: ../team-brain}}` selects a sibling directory whose configuration says `name: team-brain`. Reference keys must match the target name and cannot repeat this brain's own name. Up to 32 direct references are allowed; paths are relative to `bf.yaml`, with absolute and home-relative paths also accepted. Optional machine registration stores only names and paths; see [configuration](configuration.md).

## Record revisions and provenance

A source keeps one record per id. Collecting the same id again replaces that record; its SHA-256 filename stays the same even if its event month changes. History belongs in Git or backups, not duplicate records.

Three reserved attributes describe the revision:

| Field                 | Meaning                                          | Example                                         |
| --------------------- | ------------------------------------------------ | ----------------------------------------------- |
| `time`                | When the event happened.                         | A message was sent at `2026-09-25T09:00:00Z`.   |
| `attributes.updated`  | When the provider last modified it.              | Its author edited it at `2026-09-27T10:00:00Z`. |
| `attributes.observed` | When BF first collected this revision.           | BF received the edit at `2026-09-27T10:15:00Z`. |
| `attributes.partial`  | `true` when content is intentionally incomplete. | The sensor retained only an excerpt.            |

These timestamps require a timezone. The example message stays in its event period and appears in the modification period's `changed` list. Without `updated`, that list falls back to event time. Collection preserves `observed` when content has not changed; other attributes remain provider-specific.

When both revisions declare `updated`, an older revision cannot replace a newer one. The exception is a stored revision claiming an update after BF first observed it: that impossible ordering reveals an unreliable clock.

If files already contain duplicate ids, collection stops before changing that source. Run `bf validate`, preserve the conflicting revisions and reconcile them deliberately; collection never chooses which evidence to discard.

## Shared fields and sensor mappings

This is BF's ingestion ontology: you declare shared meanings and mappings; collection automatically validates and populates them and builds the declared relationships. The [four-tool example](context-hub.md#see-the-automatic-normalization) maps different project fields from Google Workspace, Jira, GitHub and Gcloud adapter outputs to one explicit identity. The framework does not learn a schema or infer identities from prose.

A schema gives provider-specific fields a shared meaning. For example, map a Git record's explicit author identities to `author`, and label its kind as `commit`:

```yaml
# https://fmind.github.io/brain-framework/
version: 6
name: brain
schema:
  author:
    description: Account explicitly credited as the author.
    type: identity
    cardinality: many
    relation: true
    examples: [[person:email/alice@example.test]]
  kind:
    description: Common kind of source item.
    type: string
    cardinality: one
    examples: [commit, message, document]
sensors:
  git-commits:
    command: [sensors/git-history.py, "{{home}}", "{{start}}", "{{end}}"]
    fields:
      author: { path: /attributes/author_refs }
      kind: { value: commit }
```

This example requires the reviewed [Git history sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py) to be installed in the brain. Given a sensor record containing this attribute:

```json
{ "attributes": { "author_refs": ["person:email/alice@example.test"] } }
```

BF adds these normalized fields to the stored record (both snippets omit other record fields):

```json
{
  "fields": {
    "author": ["person:email/alice@example.test"],
    "kind": "commit"
  }
}
```

`author` also creates a relationship from the record to Alice's explicit identity. No name matching or prose interpretation is involved.

### Types and cardinality

Every field needs a description and scalar `type`. Defaults are `cardinality: optional`, `relation: false` and `examples: []`. Field names begin with a lowercase letter and contain lowercase letters, digits or hyphens, up to 64 characters.

Types are `string`, `integer`, `number`, `boolean`, `timestamp` and `identity`. A timestamp requires a timezone and is normalized to UTC. An identity is an explicit, case-sensitive `scheme:value`. A relationship requires `type: identity`.

Types are strict: `"42"` is a string, not a number; `true` is a boolean, not an integer. Strings must be nonempty, without control characters, and at most 8,192 characters. Sensors handle provider-specific identity normalization, such as lowercasing an email address before emitting a person identity.

| Cardinality | Mapped value                                                                                                                                             |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `one`       | One scalar; missing or null fails the entire collection.                                                                                                 |
| `optional`  | One scalar when present; missing or null omits the field.                                                                                                |
| `many`      | Up to 1,000 scalars in a list; missing or null omits the field. An empty list explicitly records no values. Exact duplicates are removed in input order. |

Each schema example is a complete field value, validated against its type and cardinality. For `many`, examples contain lists, as `author` does above. Examples document meaning; they do not supply defaults.

### Mapping rules

Each sensor's `fields` maps a schema name to exactly one `path` or literal `value`. A path is a JSON Pointer into the sensor's record output:

| Pointer                   | Reads                                |
| ------------------------- | ------------------------------------ |
| `/attributes/author_refs` | The `author_refs` attribute.         |
| `/links`                  | The record's link list.              |
| `/attributes/people/0`    | The first item in the `people` list. |

Escape `/` in a key as `~1`, and `~` as `~0`. Missing members are absent; traversing a scalar or using an invalid array index fails. There are no wildcards, executable expressions or implicit transformations. Unknown fields, invalid constants, examples, pointers or relationship definitions are configuration errors.

Sensors emit the fixed record envelope: `id`, `title`, `text`, `time`, `url`, `links`, `aliases` and `attributes`. BF validates it, evaluates mappings and writes the resulting `fields`. Sensors cannot supply precomputed `fields`; extraction and normalization belong in their tested code.

Only mapped fields apply to a sensor. For example, requiring `kind` in the Git sensor does not require it in an unrelated mail source. `bf validate` checks stored fields against the current schema, including sources no longer configured. Unmapped fields are unknown, not evidence that a relationship does not exist.

## Graph and evidence

After collecting a record with the example author identity, read its linked evidence:

```bash
bf read person:email/alice@example.test
bf search "website" --scope person:email/alice@example.test
```

The read groups incoming links by role, such as `author` or `sender`, with their supporting claims. Follow the returned refs to inspect the original records or notes. The search looks within evidence linked to that identity. CLI, MCP and retrieval cases use the same reads and scopes.

A `relation: true` field creates directed edges from the record ref to its identity values, labeled with the field name. Each edge retains the record as its source, with the upstream URL and available `updated`, `observed` and `partial` provenance. Field values are searchable words; field names are not. `attributes` remain exact-read details. Generic `links` remain untyped.

The graph is a disposable SQLite projection of files. Replacing or deleting a record removes its old edges; the next rebuild reconstructs a deleted `.bf/` cache. Schema changes invalidate the cache.

Changing a sensor mapping affects future collections. To add fields to older evidence, explicitly backfill from retained structured evidence or recollect a chosen window. Never infer missing roles from flattened links or similar names. Notes state relationships with [typed links](link-reference.md#relationship-links); sensor mappings populate record fields.
