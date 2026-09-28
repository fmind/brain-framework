---
description: Look up exact rules for BF addresses, section anchors, identities, relationships and tags.
---

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

These name a note section, an explicitly declared entity, and a record. A path whose first segment contains `:` always names a `source:id` record, never another scheme's alias: read an alias's owner through its file address or entity. Such a path therefore cannot be a note's `entity` or a BF alias; `bf validate` reports it and search skips the note. `brain` is the stable `name` in `bf.yaml`, shared by every clone. It is not a hostname or machine registration alias. Renaming the brain changes its addresses, so update authored links explicitly.

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

Only projects, concepts and `ACTION.md` notes declare `entity`, `aliases` and `tags`. Other Markdown, such as a document copied into an action's `inputs/`, is [ordinary](brain.md#notes): BF ignores those fields there, so a capture cannot claim an identity or join a tag page. Its typed links use the file itself as their subject.

A record's ref, such as `jira:PROJ-1`, is already its identity. A note alias repeating it makes both owners ambiguous, which `bf validate` reports. To tie a project to that issue, link to the ref, with a [declared role](#relationship-links) if needed.

Other identities (`person:...`, `repo:...`, `mailto:...`, HTTPS URLs) remain supported; their schemes and query strings are opaque to BF.

### Page addresses

`bf://brain/` opens home, and `bf://brain/projects`, `bf://brain/tags` or `bf://brain/7d` opens a [page](search.md#pages). A hub note can link to the pages `bf read` opens: home, `tasks`, `tags` and a tag some note declares, a folder that exists under `projects/`, `concepts/` or `actions/`, and a period such as `today`, `7d`, `2026-09` or `2026-09-27`. Under `memories`, it can link to a known source and, below it, a period, `undated` or an existing record file. `bf validate` reports any other page target, such as `2026-13`, an unused tag, an unknown source or a missing record file, because `bf read` would fail or show an empty page. Pages have no sections.

Pages own their namespace. An entity or alias cannot be home, `tasks`, a folder root, a period or anything below `tags/` or `memories/`, and naming one after an existing folder is a validation problem. Give entities distinct paths such as `bf://brain/people/alice`.

### Sections and encoding

A fragment selects a heading in a file or an entity's owning note. For example, `bf://brain/people/alice#contact` opens the contact section above. Its explicit anchor survives a heading rename:

```markdown
## How to contact Alice {#contact}
```

Anchors start with a letter or digit and continue with letters, digits, underscores, hyphens or dots, such as `fmind.dev`. A `{#...}` suffix that does not match this syntax stays part of the heading title. An anchor ending in `.md` would read as a file name, so it is an error that makes the note invalid. Duplicate explicit anchors are errors; a section does not automatically establish an entity. Records do not accept fragments.

Encode URI components separately: a literal `#` or `?` in a record id or filename becomes `%23` or `%3F`, rather than a fragment or query delimiter.

BF reads a link destination as written, in the body as in frontmatter. `[Éric](person:email/éric@example.test)` names the same identity as that value in `aliases`, and `[Notes](local:docs/Réunion.txt)` cites that record. Another scheme or a relative path can contain spaces inside angle brackets: `[Notes](<local:docs/Meeting notes.txt>)`. A relative path is decoded once, so `Meeting%20notes.md` and `<Meeting notes.md>` name the same file. A BF address never contains whitespace or a `%` that does not start an escape, even in angle brackets: write `bf://brain/projects/launch%20plan.md`, with a literal `%` as `%25`. Otherwise the whole note is invalid, and search skips it until you encode the link.

Relative links start from the file containing them. In a project or concept note, a leading `/` starts from that folder, as in OKF's recommended [bundle-relative links](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md): `[Customers](/tables/customers.md)` in any concept note names `concepts/tables/customers.md`, and keeps working when the note moves within `concepts/`. `projects/` is the other bundle root. Actions and their attachments have none, so `bf validate` reports a leading `/` there as an absolute link. Embedded images, such as `![Logo](../assets/logo.svg)`, are links too; inline `data:` images name no target. Validation compares exact file names, so a link must match the file's case even on a case-insensitive disk.

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

`rel` is the only accepted BF query key; it must name a declared `relation: true` field and precede any fragment. `tagged-with` is reserved for [tag membership](#tag-rules). BF removes it from the target identity, so links with `?rel=owner` and `?rel=author` still point to the same Alice. An HTTPS URL such as `https://example.test/?rel=owner` keeps its whole identity and remains untyped.

Other query keys, repeated or empty values, userinfo, ports, traversal and malformed percent encoding are rejected. Links hold at most 8,192 characters. Use declared roles for ownership and authorship, never URI userinfo. To assert another entity's relationship, write it in that entity's note.

Two files asserting the same claim remain separately attributable; removing one removes only its support. Repeating a link within one section adds nothing. Untyped links stay untyped. BF infers neither a reverse relationship nor a chain of relationships: Alice owning the website does not make the website an owner of Alice.

## Across brains

Search and read use selected roots plus their direct `brains:` references, without recursive expansion. A qualified BF address resolves only in its named brain; backlinks can come from every selected brain. The address neither adds a registered brain to the selection nor contacts a network.

For example, `bf://team-brain/projects/new-website.md` can be read only when the `team-brain` brain is selected or directly referenced. Merely pasting that link into a personal note does not grant access to it.

Explicit aliases expand a uniquely owned identity across selected brains. Conflicting alias owners produce `problems` and disable that expansion; exact matches remain visible. Missing or invalid references also produce `problems`, and conflicting brain names are excluded. A root and its references need no global registration. Cross-brain roles retain their declaring schema's meaning; equal role names do not establish equal semantics.

`bf validate` checks local BF targets and sections, naming the file that wrote each one. It reports foreign targets under `unresolved` without opening those brains; unresolved foreign targets do not make a locally valid brain invalid. Select the target brain to inspect that evidence. Search and read report inaccessible or ambiguous evidence rather than inventing a destination.

Reads return identity resolution, grouped backlinks and supported claims. They do not perform multi-hop inference, reconstruct past truth, or use the graph to rank word matches. Relationship dates remain explicit evidence attributes, rather than a historical graph database. The cache stays reconstructible from files.

## Tag rules

For example, add this field to the New website note's existing frontmatter:

```yaml
# https://fmind.github.io/brain-framework/docs/link-reference/#tag-rules
tags: [website, product]
```

```bash
bf read tags
bf read bf://brain/tags/website
bf search "explanation" --scope bf://brain/tags/website
```

The directory lists both tags; the `website` page includes the note; the search only considers notes with that tag. Writing `[Website](bf://brain/tags/website)` in another note does not tag that note.

Tags need no concept note and imply no dependency. Provider labels remain collected evidence.

Labels are exact and case-sensitive: `website` and `Website` are different tags. Each note accepts up to 1,000 labels of 1–128 characters, without control characters, surrounding whitespace, forward or backward slashes or dot segments (`.` and `..`). Duplicates count once; numeric YAML labels become strings. No synonym, case or accent normalization is applied.

`bf read tags` returns brain-qualified refs and note counts. Follow those refs for encoded labels and `next_offset` for complete listings. Editing a note refreshes membership automatically. Equal labels in different brains have different identities. The `bf://NAME/tags/LABEL` namespace belongs to computed pages and cannot be an entity or alias; use ordinary links to refer to it. Each membership is a built-in `tagged-with` claim, so links and schema fields cannot use that role. Only project, concept and `ACTION.md` notes join tag pages.
