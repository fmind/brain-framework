---
description: Define shared record fields and mappings, preserve provenance and validate configuration in an editor.
---

# Record schema

Use this page to define shared record fields and sensor mappings. The [link reference](link-reference.md) owns address syntax and relationship links. Start with [Linking knowledge](links.md) for a worked example, or [Configuration](configuration.md) to select brains.

The [generated JSON Schema](../bf.schema.json) describes `bf.yaml`. To inspect the schema for your installed version:

```bash
bf schema
```

The loader uses the same strict models. The editor schema checks names, types, limits and structural rules such as choosing exactly one mapping form. Runtime validation also checks meanings that depend on this brain: declared fields, constant/example values, distinct program names and valid identities. Passing editor validation does not authorize execution; use `bf validate` for saved evidence and `bf update --dry-run` to check configuration and preview due work without running programs. Contributors regenerate all checked-in schemas with `mise run generate:schema`.

A minimal configuration declares a format version and stable brain name:

```yaml
# https://fmind.github.io/brain-framework/
version: 6
name: brain
```

Add [sensors](sensors.md), [routines](routines.md), shared `schema` fields and related `brains` only as needed. Unknown keys, duplicate keys, anchors and aliases are rejected. Sensor and routine names must be distinct; routine names are action slugs: lowercase letters and digits separated by single hyphens, starting with a letter.

`version` is required. It identifies the brain storage format: `bf.yaml`, the `memories/<source>/<sha256-id>.json` layout and the record envelope, independent of the package version. A missing or different version fails before any other field with one diagnostic naming the supported format, such as `bf.yaml declares version 7; this release reads version: 6`; a referenced brain reports the same message under `problems`. Evaluation suites have their own required `version: 5`. `bf init` writes the version and the starter relationship definitions but omits empty collections and redundant defaults; omitted settings retain their documented behavior.

A program's `command` is direct argv: its executable is a bare command name found on `PATH`, or a normalized path below `sensors/` or `routines/`, such as `sensors/mail.py`. Absolute paths, other folders, `.` or `..` segments, backslashes and placeholders in the executable are configuration errors, reported by `bf validate`, `bf update --dry-run` and the editor schema before anything runs.

## Editor schemas

Print the schema for an installed format without selecting a brain, reading its files or contacting the network:

```bash
bf schema
bf schema --kind watch
bf schema --kind registry
bf schema --kind eval
```

Each command prints one JSON Schema object; their `title` values are `Config`, `Settings`, `UserConfig` and `Suite`, respectively. Unknown kinds fail with exit 2. The schemas contain their own definitions, so validation needs no external references. Save the relevant output and associate that local file with your editor's YAML validation for the installed version.

| Configuration                     | Published schema                    |
| --------------------------------- | ----------------------------------- |
| `bf.yaml`                         | [Brain](../bf.schema.json)          |
| `settings/watch.yaml`             | [Watch](../watch.schema.json)       |
| Machine registry `bf/config.yaml` | [Registry](../registry.schema.json) |
| `evals/*.yaml`                    | [Evaluation](../eval.schema.json)   |

YAML parsing remains an additional boundary: duplicate keys, aliases, unsafe tags, malformed scalar values and oversized or deeply nested documents fail with safe file diagnostics. Correct the named file; do not bypass validation or enable a sensor to diagnose parsing.

Configuration and note frontmatter follow the YAML 1.2 core schema, like editor tooling: only `true` and `false` are booleans, so `on`, `no`, `yes` and `off` are strings, including as keys; `017` is the decimal 17; `1:30` is text, not 90 seconds. Write `0o17` or `0x1f` for octal or hexadecimal integers.

The `brains` field declares up to 32 [related brains](configuration.md#related-brains) searched beside this one; a key cannot repeat this brain's own name. Optional machine registration stores only names and paths.

## Record revisions and provenance

A source keeps one record per id. Collecting the same id again replaces that record; its SHA-256 filename stays the same even if its event month changes. History belongs in Git or backups, not duplicate records.

The event time and three reserved attributes describe the revision:

| Field                 | Meaning                                          | Example                                         |
| --------------------- | ------------------------------------------------ | ----------------------------------------------- |
| `time`                | When the event happened.                         | A message was sent at `2026-09-25T09:00:00Z`.   |
| `attributes.updated`  | When the provider last modified it.              | Its author edited it at `2026-09-27T10:00:00Z`. |
| `attributes.observed` | When BF first collected this revision.           | BF received the edit at `2026-09-27T10:15:00Z`. |
| `attributes.partial`  | `true` when content is intentionally incomplete. | The sensor retained only an excerpt.            |

These timestamps require a timezone. The example message stays in its event period and appears in the modification period's `changed` list. Without `updated`, that list falls back to event time. Collection preserves `observed` when content has not changed; other attributes remain provider-specific.

`updated`, `observed` and `partial` are reserved attribute keys: BF validates their types and sets `observed` at collection. A provider payload passed through `attributes` must not use them with another meaning, or one mistyped value fails the whole collection. Map provider fields explicitly instead, for example Drive's `modifiedTime` to `updated`.

When both revisions declare `updated`, an older revision cannot replace a newer one. The exception is a stored revision claiming an update after BF first observed it: that impossible ordering reveals an unreliable clock.

Each record file is named by the SHA-256 of its id, so a source holds each id at most once. A file whose id does not match its name is invalid: `bf validate` reports it, search skips it and reports it under `problems`, and collection never replaces or removes it. Only `<sha256-id>.json` files are records; `bf validate` also reports any other visible file under `memories/`. Rename or reconcile such a file deliberately; collection never chooses which evidence to discard.

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
    command: [sensors/git-history.py, "{{home}}/code", "{{start}}", "{{end}}"]
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

Every field needs a description and scalar `type`. Defaults are `cardinality: optional`, `relation: false` and `examples: []`. Field names begin with a lowercase letter and contain lowercase letters, digits or hyphens, up to 64 characters. `tagged-with` is reserved for [tag membership](link-reference.md#tag-rules): `bf.yaml` cannot declare it and records cannot carry it.

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

Escape `/` in a key as `~1`, and `~` as `~0`. Missing members and out-of-range array indexes are absent; traversing a scalar or using a malformed array index, such as `01` or `-1`, fails. There are no wildcards, executable expressions or implicit transformations. Unknown fields, invalid constants, examples, pointers or relationship definitions are configuration errors.

Sensors emit the fixed record envelope: `id`, `title`, `text`, `time`, `url`, `links`, `aliases` and `attributes`. BF validates it, evaluates mappings and writes the resulting `fields`. Sensors cannot supply precomputed `fields`; extraction and normalization belong in their tested code.

| Envelope field | Rule                                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------------------------- |
| `id`           | 1 to 4,096 characters without control characters; at most 7,988 once percent-encoded, so it fits a BF address. |
| `title`        | 1 to 4,096 characters without control characters.                                                              |
| `text`         | Optional; at most 4,194,304 characters.                                                                        |
| `url`          | Optional; at most 8,192 characters without control characters.                                                 |
| `aliases`      | At most 1,000 namespaced `scheme:value` identities, such as `repo:github.com/owner/name`, never display names. |
| `links`        | At most 1,000, each at most 8,192 characters without control characters.                                       |
| `attributes`   | JSON values; `updated`, `observed` and `partial` are [reserved](#record-revisions-and-provenance).             |

Every text value must be valid Unicode. A lone surrogate escape in `links`, `aliases` or `attributes`, such as `"caf\udce9.txt"` from a non-UTF-8 filename, is rejected with the field named.

One invalid record fails the whole collection before anything is saved, naming the record position and field.

Collection replaces or removes only stored files that hold their own id. A stored file whose id matches its SHA-256 name but that breaks the current record rules, such as a display-name alias or an over-long title, is replaced when its sensor returns the record again, and removed when a snapshot no longer does; a window source keeps it, reported by `bf validate`, until a window covering its time collects it. A file holding another id, or no readable id, fails the collection instead.

Only mapped fields apply to a sensor. For example, requiring `kind` in the Git sensor does not require it in an unrelated mail source. `bf validate` checks stored fields against the current schema, including sources no longer configured. Unmapped fields are unknown, not evidence that a relationship does not exist.

## Graph and evidence

After collecting a record with the example author identity, read its linked evidence:

```bash
bf read person:email/alice@example.test
bf search "website" --scope person:email/alice@example.test
```

The read groups incoming links by role, such as `author` or `sender`, with their supporting claims. Follow the returned refs to inspect the original records or notes. The search covers the identity's owning note, if any, and the evidence linked to it. CLI, MCP and retrieval cases use the same reads and scopes.

A `relation: true` field creates directed edges from the record ref to its identity values, labeled with the field name. Each edge retains the record as its source, with the upstream URL and available `updated`, `observed` and `partial` provenance. Field values are searchable words; field names are not. `attributes` remain exact-read details. Generic `links` remain untyped.

The graph is a disposable SQLite projection of files. Replacing or deleting a record removes its old edges; the next rebuild reconstructs a deleted `.bf/` cache. Schema changes invalidate the cache.

Changing a sensor mapping affects future collections. A schema edit never hides older records: search and pages still return them. A field the schema no longer declares as a relation adds no graph edges; a declared relation keeps every stored value that is an identity, whatever cardinality or type it was collected with, so a value collected as plain text claims nothing. `bf validate` names stored values the current schema rejects until you recollect the source or restore the field. To add fields to older evidence, explicitly backfill from retained structured evidence or recollect a chosen window. Never infer missing roles from flattened links or similar names. Notes state relationships with [typed links](link-reference.md#relationship-links); sensor mappings populate record fields.
