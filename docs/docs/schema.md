# Configuration schema

The [generated JSON Schema](../bf.schema.json) describes `bf.yaml`. It comes from the same strict models the loader uses.

```bash
bf schema
```

Contributors regenerate the checked-in schema from the Brain Framework checkout with `mise run generate:schema`.

`bf.yaml` holds `version: 5`, the brain `name` and optional [sensors](sensors.md) and [routines](sensors.md#routines). Sensor and routine names are distinct; routine names are action slugs (lowercase letters and digits separated by single hyphens). Unknown keys, duplicate keys, anchors and aliases are rejected. Related brains are declared under `brains: {team: {path: ../team}}` (at most 32), resolved relative to this file; absolute and home-relative paths are accepted. Keys must match the target name and cannot repeat this brain's own name. Machine collection trust and optional registrations belong in `~/.config/bf/config.yaml`; see [personal and team brains](brain.md#personal-and-team-brains).

## Shared fields and sensor mappings

Declare a small vocabulary under `schema`. Every field has a description and a scalar `type`; `cardinality` defaults to `optional`, `relation` to `false`, and `examples` to an empty list. Names use lowercase letters, digits and hyphens, starting with a letter (maximum 64 characters).

```yaml
# https://fmind.github.io/brain-framework/
version: 5
name: knowledge
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

Types are `string`, `integer`, `number`, `boolean`, `timestamp` (timezone required, normalized to UTC), and `identity` (an explicit `scheme:value`, case-sensitive). Types are strict: a numeric string is not a number and a boolean is not an integer. Strings are nonempty, have no control characters and are at most 8,192 characters. A relationship requires `type: identity`. Identity normalization belongs to sensors: for example, lowercasing an email address before emitting its person identity. Names and prose never establish identity equivalence.

| Cardinality | Mapped value                                                                                                                                             |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `one`       | One scalar; missing or null fails the entire collection.                                                                                                 |
| `optional`  | One scalar when present; missing or null omits the field.                                                                                                |
| `many`      | A list of up to 1,000 scalars; missing or null omits the field; an empty list explicitly records no values. Exact duplicates are removed in input order. |

Each example is a complete field value and is validated against its type and cardinality. For `many`, examples therefore contain lists. Examples document meaning; they never supply defaults or infer values.

Each sensor's `fields` maps schema names to exactly one `path` or literal `value`. A path is a JSON Pointer into the sensor's record output: `/attributes/author_refs`, `/links`, or `/attributes/people/0`. Escape a literal `/` in a key as `~1` and `~` as `~0`. Missing members are absent; traversing a scalar or using an invalid array index fails. There are no wildcards, executable expressions or implicit transformations. Sensors perform provider-specific extraction and normalization in their own tested code. Unknown fields, invalid constants, examples, pointers or relationship definitions are configuration errors.

The fixed record envelope (`id`, `title`, `text`, `time`, `url`, `links`, `aliases`, `attributes`) remains the sensor output contract. Brain Framework validates that envelope, evaluates the explicit mappings and writes the normalized values into `fields`. Sensors cannot precompute `fields`. Only mapped fields apply to that sensor: a required field does not become mandatory for unrelated sources. `bf validate` checks every stored field against the current schema, including historical sources. Older records may have no normalized fields; absence means unknown, not evidence that a relationship does not exist.

## Graph and evidence

A field with `relation: true` produces directed edges from the record ref to its identity values, labeled with the field name. Each edge is supported by the record file, with its upstream URL and available `updated`, `observed` and `partial` provenance. Field values are searchable words (their names are not); `attributes` remain exact-read details. Generic `links` remain untyped relationships.

The graph is a disposable SQLite projection. Replacing or deleting a record removes its obsolete edges; deleting `.bf/` reconstructs the graph from files. Changing the schema invalidates the cache. Changing a sensor mapping affects subsequent collections, not historical evidence: explicitly backfill from retained structured evidence or recollect a chosen window. Never reconstruct missing roles from flattened links or similar names.

Use `bf read person:email/alice@example.test`: its `backlinks` group the records and notes that link to Alice by relationship (`author`, `sender`, …), each with its claims and evidence; read the returned refs to inspect their fields. `bf search WORDS --scope person:email/alice@example.test` searches within them. Sensor mappings are the only way to populate record fields; notes state relationships with [typed links](#relationship-links). CLI, MCP and retrieval cases share these reads and scopes.

## BF links

A BF address names a resource without contacting a network:

```text
bf://team/projects/archive.md#decision
bf://team/people/marc
bf://team/mail:message-123
```

The authority is the stable `name` in that brain's `bf.yaml`, shared by every clone, not a host or a machine-specific registration alias. Choose a distinctive name before sharing links. Renaming it changes its addresses: update authored links explicitly. A path addresses an existing authored Markdown file, a `source:id` record, or an explicitly declared entity. Entity paths such as `people/marc` are logical names, not new directories or automatic type inference. Other identifiers (`person:...`, `repo:...`, `mailto:...`, HTTPS URLs) remain supported; their schemes and query strings are opaque to BF. For reads, `bf://team/` names that brain's home page and `bf://team/projects` or `bf://team/7d` one of its [pages](search.md#pages); an existing folder takes precedence over an entity of the same path, so name entities differently from folders.

Give an entity a home in an existing authored note:

```markdown
---
entity: bf://team/people/marc
aliases: [person:email/marc@example.test]
---

# Marc

## Contact details {#contact}

Reviewed contact information.
```

`entity` identifies what the note represents; its ordinary file URI still resolves to the note. Aliases are explicit identity equivalences, not evidence of friendship, authorship or ownership. BF entities and aliases must belong to the declaring brain's namespace. Ambiguous owners are validation problems and never silently merged. An email address is an address; only an explicit, reviewed alias associates it with a person. There is no name similarity matching.

A fragment selects a Markdown heading in either a file or an entity's owning note. Use `## Display title {#stable-id}` to keep links valid when changing its wording; anchors accept letters, digits, underscores, hyphens and dots (for example `fmind.dev`). Duplicated explicit anchors are errors. A section does not establish an entity automatically. Fragments on records are rejected. Encode URI components separately: a literal `#` or `?` in a record ID or filename is `%23` or `%3F`, not a fragment or query delimiter.

## Relationship links

Declare a role under the existing schema before using it:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
schema:
  friend:
    description: Explicit friendship from the subject to the target.
    type: identity
    cardinality: many
    relation: true
```

Inside a note representing Alice:

```markdown
[Marc](bf://team/people/marc?rel=friend) [Contact section](bf://team/people/marc?rel=friend#contact)
```

The first target is Marc; the second is his contact section. `rel` is the only query a BF link accepts: it names a declared `relation: true` field, and it precedes the fragment. BF removes it from the target identity, so `?rel=friend` and `?rel=author` do not create different Marc nodes. Only BF links use this convention: `https://example.test/?rel=friend` retains its full identity and is an untyped link.

The subject is the note's `entity`, otherwise its file; a collected record's links have the record as subject. Other query keys, repeated or empty values, userinfo, ports, traversal and malformed percent encoding are rejected, and a link holds at most 8,192 characters. To state a relationship of another entity, write the link in that entity's note. For authorship and ownership, declare relationships such as `author` and `owner`, never URI userinfo; sensors map them for records.

Each projected claim has a `subject`, a `relation`, a `target` and its `origin`: the Markdown section or record that contains it. Two files asserting the same claim remain independently attributable, and removal of one removes only its support; repeating a link within one section adds nothing. Generic links remain untyped edges. Neither symmetric relationships nor transitive relationships are inferred.

## Across brains

Reads and searches expand one uniquely owned identity through its explicit aliases across the selected brains. A qualified BF address resolves only through its named brain, while its backlinks come from every selected brain; it never adds a registered brain to the selection or accesses a network. Search/read include selected roots and their direct `brains:` declarations, with no recursive expansion. Named references must match their destination configuration; missing or invalid references produce `problems`, and conflicting brain names are excluded. No global registry is needed for a local root and its references. Conflicting alias owners produce `problems` and disable expansion; exact matches remain visible. Cross-brain roles retain their declaring brain's schema meaning: equal role names alone do not certify semantic equivalence.

`bf validate` checks local BF targets and sections and reports foreign targets under `unresolved` without fetching or opening those brains. Unresolved foreign targets do not make a locally valid brain invalid; read them with the target brain selected when that evidence is needed. Search and exact reads report inaccessible or ambiguous evidence rather than inventing a destination.

Reads supply identity resolution, backlinks grouped by relationship, claims about a subject and explanations. It does not perform multi-hop inference, temporal truth reconstruction, or graph-based lexical ranking. Relationship dates are explicit evidence attributes, not a retained historical database. Everything remains reconstructible from files in the disposable SQLite cache.
