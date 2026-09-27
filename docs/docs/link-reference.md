# Link reference

Start with [Linking knowledge](links.md) for examples you can follow. This page defines exact address, identity and relationship rules.

## BF links

A BF address names a resource without contacting a network. Read this example from left to right:

```text
bf://brain/projects/new-website.md?rel=depends-on#decision
```

| Part                       | Meaning                                                |
| -------------------------- | ------------------------------------------------------ |
| `bf://`                    | A Brain Framework address; no network request.         |
| `brain`                    | The stable `name` in `bf.yaml`.                        |
| `/projects/new-website.md` | The target path inside that brain.                     |
| `?rel=depends-on`          | Optional declared meaning of the link from its source. |
| `#decision`                | Optional section within the target note.               |

Use `?rel=` before `#`; omit either when you do not need it. Other examples:

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

For example, add this field to the New website note's existing frontmatter:

```yaml
# https://fmind.github.io/brain-framework/docs/link-reference/#tag-rules
tags: [website, accessibility]
```

```bash
bf read tags
bf read bf://brain/tags/website
bf search "explanation" --scope bf://brain/tags/website
```

The directory lists both tags; the `website` page includes the note; the search only considers notes with that tag. Writing `[Website](bf://brain/tags/website)` in another note does not tag that note.

Tags need no concept note and imply no dependency. Provider labels remain collected evidence.

Labels are exact and case-sensitive: `website` and `Website` are different tags. Each note accepts up to 1,000 labels of 1–128 characters, without control characters, surrounding whitespace, slashes or dot segments (`.` and `..`). Duplicates count once; numeric YAML labels become strings. No synonym, case or accent normalization is applied.

`bf read tags` returns brain-qualified refs and note counts. Follow those refs for encoded labels and `next_offset` for complete listings. Editing a note refreshes membership automatically. Equal labels in different brains have different identities. The `bf://NAME/tags/LABEL` namespace belongs to computed pages and cannot be an entity or alias; use ordinary links to refer to it.
