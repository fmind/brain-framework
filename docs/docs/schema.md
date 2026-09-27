# Schema and link reference

<span id="configuration-schema"></span>

Use this page to define shared record fields, exact identities and relationship roles. Start with [Linking knowledge](links.md) for a worked example, or [Configuration](configuration.md) to select brains.

The [generated JSON Schema](../bf.schema.json) describes `bf.yaml`. To inspect the schema for your installed version:

```bash
bf schema
```

The loader uses the same strict models. Contributors regenerate the checked-in schema with `mise run generate:schema`.

A minimal configuration declares a format version and stable brain name:

```yaml
# https://fmind.github.io/brain-framework/
version: 5
name: brain
```

Add [sensors](sensors.md), [routines](routines.md), shared `schema` fields and related `brains` only as needed. Unknown keys, duplicate keys, anchors and aliases are rejected. Sensor and routine names must be distinct; routine names are action slugs: lowercase letters and digits separated by single hyphens, starting with a letter.

A reference such as `brains: {team-brain: {path: ../team-brain}}` selects a sibling directory whose configuration says `name: team-brain`. Reference keys must match the target name and cannot repeat this brain's own name. Up to 32 direct references are allowed; paths are relative to `bf.yaml`, with absolute and home-relative paths also accepted. Optional machine registration stores only names and paths; see [configuration](configuration.md).

## Record revisions and provenance

A source keeps one record per id. Collecting the same id again replaces that record; if its event month changes, the line moves to the corresponding partition. History belongs in Git or backups, not duplicate records.

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

A schema gives provider-specific fields a shared meaning. For example, map a Git record's explicit author identities to `author`, and label its kind as `commit`:

```yaml
# https://fmind.github.io/brain-framework/
version: 5
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

Only mapped fields apply to a sensor. For example, requiring `kind` in the Git sensor does not require it in an unrelated mail source. `bf validate` checks stored fields against the current schema, including historical sources. Older records may lack fields; absence means unknown, not that a relationship does not exist.

## Graph and evidence

After collecting a record with the example author identity, read its linked evidence:

```bash
bf read person:email/alice@example.test
bf search "website" --scope person:email/alice@example.test
```

The read groups incoming links by role, such as `author` or `sender`, with their supporting claims. Follow the returned refs to inspect the original records or notes. The search looks within evidence linked to that identity. CLI, MCP and retrieval cases use the same reads and scopes.

A `relation: true` field creates directed edges from the record ref to its identity values, labeled with the field name. Each edge retains the record as its source, with the upstream URL and available `updated`, `observed` and `partial` provenance. Field values are searchable words; field names are not. `attributes` remain exact-read details. Generic `links` remain untyped.

The graph is a disposable SQLite projection of files. Replacing or deleting a record removes its old edges; the next rebuild reconstructs a deleted `.bf/` cache. Schema changes invalidate the cache.

Changing a sensor mapping affects future collections. To add fields to older evidence, explicitly backfill from retained structured evidence or recollect a chosen window. Never infer missing roles from flattened links or similar names. Notes state relationships with [typed links](#relationship-links); sensor mappings populate record fields.

## BF links

A BF address names a resource without contacting a network:

```text
bf://brain/projects/new-website.md#decision
bf://brain/people/alice
bf://brain/local-documents:website-demo/brief.txt
```

These name a note section, an explicitly declared entity, and a record. `brain` is the stable `name` in `bf.yaml`, shared by every clone. It is not a hostname or machine registration alias. Renaming the brain changes its addresses, so update authored links explicitly.

Entity paths such as `people/alice` are logical names; they do not create directories or infer a type. To give that identity a home, save `concepts/alice.md`:

```markdown
---
type: person
status: draft
entity: bf://brain/people/alice
aliases: [person:email/alice@example.test]
---

# Alice

## Contact details {#contact}

Reviewed contact information belongs here.
```

Both identities now resolve to the same note; its file address still works:

```bash
bf read bf://brain/people/alice
bf read person:email/alice@example.test
bf read bf://brain/concepts/alice.md
```

Only a reviewed, explicit alias associates the email address with Alice. Aliases establish identity equivalence, not friendship, authorship or ownership. BF entities and aliases must belong to their declaring brain's namespace. Ambiguous owners are validation problems and are never silently merged.

Other identities (`person:...`, `repo:...`, `mailto:...`, HTTPS URLs) remain supported; their schemes and query strings are opaque to BF. `bf://brain/` opens home, and `bf://brain/projects` or `bf://brain/7d` opens a [page](search.md#pages). Existing folders take precedence over entities of the same path, so give entities distinct names.

### Sections and encoding

A fragment selects a heading in a file or an entity's owning note. For example, `bf://brain/people/alice#contact` opens the contact section above. Its explicit anchor survives a heading rename:

```markdown
## How to contact Alice {#contact}
```

Anchors accept letters, digits, underscores, hyphens and dots, such as `fmind.dev`. Duplicate explicit anchors are errors; a section does not automatically establish an entity. Records do not accept fragments.

Encode URI components separately: a literal `#` or `?` in a record id or filename becomes `%23` or `%3F`, rather than a fragment or query delimiter.

## Relationship links

Declare the role under `schema` before writing a typed link. New brains already declare `owner`; its meaning is:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
schema:
  owner:
    description: Person or organization explicitly responsible for the subject.
    type: identity
    cardinality: many
    relation: true
```

If Alice is explicitly responsible for the fictional website project, put this in its project note:

```markdown
## Ownership

[Owner: Alice](bf://brain/people/alice?rel=owner)
```

The claim says the project has Alice as its owner, supported by the Ownership section. Its subject is the note's `entity`, if declared, otherwise its file. A record's links use the record as their subject. Each claim records `subject`, `relation`, `target` and `origin`.

`rel` is the only accepted BF query key; it must name a declared `relation: true` field and precede any fragment. BF removes it from the target identity, so links with `?rel=owner` and `?rel=author` still point to the same Alice. An HTTPS URL such as `https://example.test/?rel=owner` keeps its whole identity and remains untyped.

Other query keys, repeated or empty values, userinfo, ports, traversal and malformed percent encoding are rejected. Links hold at most 8,192 characters. Use declared roles for ownership and authorship, never URI userinfo. To assert another entity's relationship, write it in that entity's note.

Two files asserting the same claim remain separately attributable; removing one removes only its support. Repeating a link within one section adds nothing. Untyped links stay untyped. BF infers neither a reverse relationship nor a chain of relationships: Alice owning the website does not make the website an owner of Alice.

## Across brains

Search and read use selected roots plus their direct `brains:` references, without recursive expansion. A qualified BF address resolves only in its named brain; backlinks can come from every selected brain. The address neither adds a registered brain to the selection nor contacts a network.

For example, `bf://team-brain/projects/new-website.md` can be read only when the `team-brain` brain is selected or directly referenced. Merely pasting that link into a personal note does not grant access to it.

Explicit aliases expand a uniquely owned identity across selected brains. Conflicting alias owners produce `problems` and disable that expansion; exact matches remain visible. Missing or invalid references also produce `problems`, and conflicting brain names are excluded. A root and its references need no global registration. Cross-brain roles retain their declaring schema's meaning; equal role names do not establish equal semantics.

`bf validate` checks local BF targets and sections. It reports foreign targets under `unresolved` without opening those brains; unresolved foreign targets do not make a locally valid brain invalid. Select the target brain to inspect that evidence. Search and read report inaccessible or ambiguous evidence rather than inventing a destination.

Reads return identity resolution, grouped backlinks and supported claims. They do not perform multi-hop inference, reconstruct past truth, or use the graph to rank word matches. Relationship dates remain explicit evidence attributes, rather than a historical graph database. The cache stays reconstructible from files.

## Tag rules

Tags classify authored notes through frontmatter. Add `tags: [website]` to a note to include it in `bf://brain/tags/website`; a Markdown link to that page does not add membership. Provider labels remain collected evidence. Tags imply neither dependencies nor proven claims, and need no concept note.

Labels are exact and case-sensitive: `website` and `Website` are different tags. Each note accepts up to 1,000 labels of 1–128 characters, without control characters, surrounding whitespace, slashes or dot segments (`.` and `..`). Duplicates count once; numeric YAML labels become strings. No synonym, case or accent normalization is applied.

`bf read tags` returns brain-qualified refs and note counts. Follow those refs for encoded labels and `next_offset` for complete listings. Editing a note refreshes membership automatically. Equal labels in different brains have different identities. The `bf://NAME/tags/LABEL` namespace belongs to computed pages and cannot be an entity or alias; use ordinary links to refer to it.
