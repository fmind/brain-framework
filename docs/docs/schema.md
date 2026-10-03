---
description: Exact formats of bf.yaml, notes, records, shared fields and sensor mappings, with editor validation.
---

# Notes, records and fields

This reference defines the files BF reads: `bf.yaml`, OKF notes, record files and the shared fields that connect them. The [link reference](link-reference.md) owns address syntax. For worked examples, start with [Linking knowledge](links.md).

A minimal `bf.yaml` declares the brain format and a stable name:

```yaml
# https://fmind.github.io/brain-framework/
version: 7
name: brain
```

`version` is the brain format of `bf.yaml`, `evals/*.yaml`, record files and the `memories/` layout; it is independent of the package version. A missing or different version fails before any other field, naming the supported one: `bf.yaml declares version 6; this release reads version: 7`. The changelog lists the upgrade steps of each format.

Add `fields`, [sensors](sensors.md#sensor-settings), [routines](routines.md#routine-settings), related `brains` and [watch preferences](schedule.md#watch-preferences) only as needed. `bf init` writes the version, the name and four starter relations. A program's `command` starts with a bare command name found on PATH, or a normalized path below `sensors/` or `routines/`; absolute paths, other folders, `..` and placeholders there are errors.

## Editor schemas

Print the JSON Schema of an installed format, without selecting a brain or using the network:

```bash
bf schema
bf schema --kind registry
bf schema --kind eval
```

Their `title` values are `Config`, `UserConfig` and `Suite`. `--kind search-reply` and `--kind read-reply` print the [reply schemas](retrieval.md#reply-schemas). Save the output and associate it with your editor's YAML validation.

| File                              | Published schema                    |
| --------------------------------- | ----------------------------------- |
| `bf.yaml`                         | [Brain](../bf.schema.json)          |
| Machine registry `bf/config.yaml` | [Registry](../registry.schema.json) |
| `evals/*.yaml`                    | [Evaluation](../eval.schema.json)   |

The schemas check names, types, limits and structure. BF also checks meanings that depend on the brain, such as declared fields, distinct program names and valid identities: run `bf validate` and `bf update --dry-run`. YAML parsing rejects duplicate keys, anchors, aliases, unsafe tags and oversized documents. Configuration and frontmatter follow the YAML 1.2 core schema: only `true` and `false` are booleans, so `on` and `no` are strings, and `1:30` is text.

## Note format

Projects, concepts and `ACTION.md` notes are OKF notes. [Write notes and actions](brain.md#notes) describes their fields; these rules settle edge cases:

- `type` must be nonempty; `status`, when set, must be `draft`, `stable` or `deprecated`, and omitted means `stable`. `bf validate` enforces both.
- Frontmatter must be valid YAML closed by a `---` line, and the title must be nonblank and at most 4,096 characters. A note breaking either rule is invalid and unsearchable, reported under `problems`.
- A frontmatter key with an invalid value, such as `tags` that is not a list or `updated` that is not a real `YYYY-MM-DD`, also makes the note invalid and unsearchable. So does a link that cannot be parsed, such as `http://[your-host]/` or a `bf://` link with malformed encoding or another query key; the problem names its `line N` or frontmatter key, such as `links`.
- `stale_after` is an ISO 8601 date-time with a timezone; a date alone is invalid.
- `aliases` are namespaced `scheme:value` identities, never display names. A URI in `resource` is an identity too. Entities, aliases and resources follow the [identity rules](link-reference.md#identity-rules); `aliases`, `links` and `tags` hold at most 1,000 items each.
- `fields` holds values of fields declared in `bf.yaml`, checked like a record's: an invalid value claims nothing and fails `bf validate`, while the note stays searchable. A declared relation written at the top level instead is reported, because OKF ignores unknown keys.
- `sources` entries need a nonempty `resource`, which may also describe a population; `verified` events need `by` and `at`.

In `projects/` and `concepts/`, `index.md` lists a folder's notes and permits only `okf_version` at a folder root; `log.md` groups changes under `YYYY-MM-DD` headings without frontmatter. Neither gets reminders or tasks.

Other Markdown, such as files in an action's `inputs/` and `outputs/`, stays ordinary: only valid `title`, `type`, `status` (any word), `updated`, `summary` and `description` apply. Identities, tags, fields, `sources` and `stale_after` apply only to OKF notes, so a copied document cannot claim an identity. Ordinary frontmatter that is not valid YAML is ignored and the file is searched without it, a block that never closes is searched as text, and a title longer than 4,096 characters, or blank, gives way to the file name. A link URL that cannot be parsed, such as `http://[your-host]/`, connects nothing while the file stays searchable; a malformed `bf://` link still makes it invalid.

In OKF and ordinary Markdown alike, Git conflict markers (`<<<<<<<`, `|||||||`, `>>>>>>>`) at the start of a line make the file invalid and unsearchable, even inside a code block: indent them by one space to show them. A leading byte order mark is ignored.

## Record revisions and provenance

A source keeps one record per id. Collecting the same id again replaces it; its file, named by the SHA-256 of the id, stays the same. History belongs in Git or backups.

| Field                 | Meaning                                          | Example                                    |
| --------------------- | ------------------------------------------------ | ------------------------------------------ |
| `time`                | When the event happened.                         | A message was sent on 2026-09-25 at 09:00. |
| `attributes.updated`  | When the provider last modified it.              | Its author edited it on 2026-09-27.        |
| `attributes.observed` | When BF first collected this revision.           | BF received the edit 15 minutes later.     |
| `attributes.partial`  | `true` when the text is deliberately incomplete. | The sensor kept only an excerpt.           |

These timestamps need a timezone. The edited message stays in its event period and appears in the `changed` list of the day it was modified. Like a new event, an upstream edit made after a linked note's last edit flags that note for [review](retrieval.md#ordering-and-review-signals); `observed` never does. Collection keeps `observed` while the content is unchanged. When both revisions declare `updated`, an older revision cannot replace a newer one.

`updated`, `observed` and `partial` are reserved: BF validates their types and sets `observed`. Map provider fields to them explicitly, such as Drive's `modifiedTime` to `updated`, rather than passing a raw payload.

Only `<sha256-id>.json` files under `memories/SOURCE/` are records. A file whose id does not match its name is invalid: `bf validate` reports it, search skips it and collection never replaces or removes it. `bf validate` also reports other visible files under `memories/`.

## Shared fields and sensor mappings

Fields give provider-specific values a shared meaning. You declare them under `fields:` in `bf.yaml`; each sensor's `fields` maps its output into them; collection validates the values and builds the declared relations. The [four-tool example](context-hub.md#how-the-four-tools-share-one-project) maps four differently named project fields to one identity. BF never learns fields or infers identities from prose.

For example, map a Git record's author identities to `author`, and label its kind:

```yaml
# https://fmind.github.io/brain-framework/
version: 7
name: brain
fields:
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

The reviewed [Git history sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py) prints `"attributes": {"author_refs": ["person:email/alice@example.test"]}`. BF stores `"fields": {"author": ["person:email/alice@example.test"], "kind": "commit"}` in the record, and `author` becomes a relation from the commit to Alice's identity.

### Types and cardinality

Every field needs a `description` and a `type`: `string`, `integer`, `number`, `boolean`, `timestamp` or `identity`. Defaults are `cardinality: optional`, `relation: false` and no `examples`. A relation needs `type: identity`. Field names start with a lowercase letter and hold lowercase letters, digits or hyphens, up to 64 characters. `tagged-with`, `links` and `cites` are reserved.

Types are strict: `"42"` is a string, not a number. Strings are nonempty, without control characters, up to 8,192 characters. A timestamp needs a timezone. An identity is an exact, case-sensitive `scheme:value` without invisible characters, as the [identity rules](link-reference.md#identity-rules) define; sensors normalize it, such as lowercasing an email address.

| Cardinality | Mapped value                                                                                            |
| ----------- | ------------------------------------------------------------------------------------------------------- |
| `one`       | One value; a missing or null value fails the whole collection.                                          |
| `optional`  | One value when present; missing or null omits the field.                                                |
| `many`      | A list of up to 1,000 values, duplicates removed; missing or null omits it, an empty list records none. |

A note's `fields` follow the same types and cardinality, which `bf validate` checks; only collection requires a `one` value, so a note may omit it. Each example is a complete field value checked against the type and cardinality; examples never supply defaults. Listings and backlink previews show single-value fields of up to 200 characters, so sources that disagree appear side by side.

### Narrower relations and allowed targets

A relation can name a `broader` relation and restrict its values to `targets`. For example, a calendar sensor maps organizers and attendees, and one relation page should list everyone who took part:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  participant:
    description: Person who explicitly took part in the event.
    type: identity
    cardinality: many
    relation: true
    targets: ["person:email/"]
  organizer:
    description: Person who explicitly organized the event.
    type: identity
    cardinality: many
    relation: true
    broader: participant
    targets: ["person:email/"]
```

`bf read person:email/alice@example.test --rel participant` then lists her events through both relations, each item with its own `relation`. Map `organizer` from the sensor, not `participant`, or each person is stored twice.

| Setting   | Rule                                                                                                                                                                                                                                                                                  |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `broader` | Another declared relation without its own `broader`. It changes relation pages at read time, never stored records.                                                                                                                                                                    |
| `targets` | 1 to 64 quoted prefixes, each a scheme, a colon and optionally the start of the value, such as `repo:github.com/`. Collected values and record links must start with one as written; a note's typed links and fields may also name a target whose owner declares a matching identity. |

With `targets`, a collection whose mapped value falls outside them fails before saving, naming the record's position and field, never the value: `record 3: field organizer: identity outside the declared targets`. `bf validate` reports stored records, note fields and typed links outside them.

### Mapping rules

Each sensor's `fields` maps a declared field to exactly one `path` or literal `value`. A path is a JSON Pointer into the sensor's record:

| Pointer                   | Reads                                |
| ------------------------- | ------------------------------------ |
| `/attributes/author_refs` | The `author_refs` attribute.         |
| `/links`                  | The record's link list.              |
| `/attributes/people/0`    | The first item in the `people` list. |

Escape `/` in a key as `~1` and `~` as `~0`. Missing members are absent; traversing a scalar fails. There are no wildcards, expressions or implicit conversions. Every record a sensor prints follows the fixed format below; a sensor cannot supply `fields` itself.

| Record key   | Rule                                                                                                                                                                                                                                            |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`         | 1 to 4,096 characters without control characters; at most 7,988 once percent-encoded, so it fits a BF address.                                                                                                                                  |
| `title`      | 1 to 4,096 characters without control characters.                                                                                                                                                                                               |
| `text`       | Optional; at most 4,194,304 characters.                                                                                                                                                                                                         |
| `time`       | Optional ISO 8601 timestamp with a timezone.                                                                                                                                                                                                    |
| `url`        | Optional; at most 8,192 characters without control characters.                                                                                                                                                                                  |
| `links`      | At most 1,000 identities or URLs of at most 8,192 characters. Other text is stored and searchable but connects only to a note whose path it spells exactly, such as `projects/x.md`; `bf validate` [warns](checks.md#warnings) once per source. |
| `aliases`    | At most 1,000 namespaced identities, never display names, following the [identity rules](link-reference.md#identity-rules).                                                                                                                     |
| `attributes` | JSON values; `updated`, `observed` and `partial` are [reserved](#record-revisions-and-provenance).                                                                                                                                              |

Fields other than `text`, including the mapped `fields` and the `observed` time BF adds, must fit 2 MiB. Text must be valid Unicode: a lone surrogate, as from a non-UTF-8 file name, is rejected with its field named. One invalid record fails the whole collection before anything is saved, and the error names the first one, such as `1.id: Field required`. Only mapped fields apply to a sensor, and `bf validate` checks stored fields against the current declarations, including sources no longer configured.

## Graph and evidence

After collecting a commit by Alice:

```bash
bf read person:email/alice@example.test
bf search "website" --scope person:email/alice@example.test
```

The read groups incoming links by relation, such as `author`, with their supporting records. The search covers the identity's owning note and the evidence linked to it. Each `relation: true` field creates claims from the record to its identity values, keeping the record as their origin. Field values are searchable; field names and `attributes` are not.

The graph lives in the disposable cache. Replacing a record replaces its claims, and structural changes to `fields:`, such as a `type` or `cardinality`, rebuild the cache; `description`, `examples`, `broader` and `targets` apply without one. A field no longer declared as a relation adds no claims; `bf validate` names stored values the current fields reject. [`bf export`](commands.md#export-the-graph) prints every claim.

### Reproject stored records

After renaming a field or changing a mapping, apply the current mappings to the records a sensor already collected, without running it:

```bash
bf build --reproject git-commits --dry-run
bf build --reproject git-commits
```

For example, after renaming `author` to `creator` in `fields:` and in the sensor's mapping, `bf validate` reports `field author is not declared in bf.yaml fields` for each stored commit. The preview counts the changes without writing, such as `{"sensor":"git-commits","dry_run":true,"records":2,"changed":2,"unchanged":0,"failed":0}`. The second command rewrites those records' `fields` and refreshes the cache; validation then passes and `bf read person:email/alice@example.test --rel creator` lists the commits.

Reprojection reads each record as its sensor printed it and keeps every other value, including `observed`; it never removes a record. It cannot map a value the sensor never printed: a record the mappings reject keeps its fields, counts as `failed` and appears under `problems` (up to 200), and the command exits 1.

Recollect those records after updating the sensor. Changes commit in transactions of up to 1,000 records; after an interruption, `bf build` recovers and a rerun completes the reprojection.
