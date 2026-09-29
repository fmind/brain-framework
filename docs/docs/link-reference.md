---
description: Exact rules for BF addresses, section anchors, identities, relation links and tags.
---

# Link and tag reference

Start with [Linking knowledge](links.md) for examples you can follow. This page defines exact address, identity, relation and tag rules.

## BF links

A BF address names a resource without contacting a network. Read this example from left to right:

```text
bf://brain/projects/new-website.md?rel=depends-on#decision
```

| Part                       | Meaning                                                |
| -------------------------- | ------------------------------------------------------ |
| `bf://`                    | A Brain Framework address; no network request.         |
| `brain`                    | The stable `name` in `bf.yaml`, shared by every clone. |
| `/projects/new-website.md` | The target path inside that brain.                     |
| `?rel=depends-on`          | Optional declared relation of the link.                |
| `#decision`                | Optional section of the target note.                   |

Other examples:

```text
bf://brain/projects/new-website.md#decision
bf://brain/people/alice
bf://brain/local-documents:website-demo/brief.txt
```

These name a note section, a declared entity and a record. A path whose first segment holds `:` always names a `SOURCE:ID` record, so it cannot be an entity or alias. The brain name is not a hostname: renaming the brain changes its addresses.

Entity paths such as `people/alice` are logical names; they create no folder. To give that identity a home, save `concepts/alice.md`:

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

All three addresses then open the same note:

```bash
bf read bf://brain/people/alice
bf read person:email/alice@example.test
bf read bf://brain/concepts/alice.md
```

An alias establishes identity, not authorship or ownership. BF entities and aliases must use their own brain's namespace, and two notes claiming one identity are a validation problem, never merged.

Only OKF notes declare `entity`, `aliases`, `resource` and `tags`; a document copied into an action's `inputs/` cannot. A record's ref, such as `jira:PROJ-1`, is already its identity: link to it rather than repeating it as a note alias. Other identities, such as `person:`, `repo:`, `mailto:` or HTTPS URLs, are opaque to BF.

### Page addresses

`bf://brain/` opens home, and `bf://brain/projects`, `bf://brain/tags` or `bf://brain/7d` open [pages](retrieval.md#pages). A hub note can link to home, `tasks`, `tags` or a used tag, an existing folder under `projects/`, `concepts/` or `actions/`, a period, a known source and its periods, `undated` or record files. `bf validate` reports any other page target, such as `2026-13` or an unknown source. Pages have no sections, and an entity or alias cannot take a page's address.

### Sections and encoding

A fragment selects a heading of a note or an entity's note, such as `bf://brain/people/alice#contact`. An explicit anchor survives a heading rename:

```markdown
## How to contact Alice {#contact}
```

Anchors start with a letter or digit and continue with letters, digits, underscores, hyphens or dots. An anchor ending in `.md` or a duplicate explicit anchor makes the note invalid. Records take no fragment.

Encode URI components separately: a literal `#` or `?` in a record id or file name becomes `%23` or `%3F`. A link destination reads as written, so `[Éric](person:email/éric@example.test)` names that identity. Wrap spaces in angle brackets, as in `[Notes](<local:docs/Meeting notes.txt>)`. A BF address never holds whitespace or a stray `%`: write `bf://brain/projects/launch%20plan.md`.

Relative links start from the containing file. In a project or concept note, a leading `/` starts from that bundle's root, as in [OKF bundle-relative links](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md): `[Customers](/tables/customers.md)` in a concept names `concepts/tables/customers.md`. Actions have no bundle root. Images are links too. Validation compares exact file names, even on a case-insensitive disk.

## Relation links

Declare a relation under `fields:` in `bf.yaml` before writing a typed link. New brains declare `owner`:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  owner:
    description: Person or organization explicitly responsible for the subject.
    type: identity
    cardinality: many
    relation: true
```

If Alice is explicitly responsible for the website project, write in its note:

```markdown
## Ownership

[Owner: Alice](bf://brain/people/alice?rel=owner)
```

The claim says the project has Alice as owner, with the Ownership section as origin. Its subject is the note's `entity`, if declared, otherwise its file; a record's links use the record. Each claim records `subject`, `relation`, `target` and `origin`. A note can make the same claim from its frontmatter [`fields`](links.md#set-typed-fields), with the whole note as origin.

`rel` is the only query key a BF address accepts. It must name a declared relation or `cites`, and precede any fragment. Only `bf://` links take it: `bf validate` reports `?rel=` elsewhere, and a URL such as `https://example.test/?rel=owner` stays one untyped identity. A link naming an undeclared relation stays searchable as an untyped link, and validation names the relation to declare. The relation never changes the target: `?rel=owner` and `?rel=author` links point to the same Alice.

| Built-in relation | Meaning                                                         |
| ----------------- | --------------------------------------------------------------- |
| `cites`           | Each OKF `sources` entry: the note derives from its `resource`. |
| `tagged-with`     | Each tag of a note; see [tag rules](#tag-rules).                |
| `links`           | The backlink group of untyped links.                            |

`bf.yaml` cannot declare these three. An item that both cites and links to a target is listed once, under `cites`. A relation's [`broader`](schema.md#narrower-relations-and-allowed-targets) adds its links to the broader relation page, and its `targets` restrict the identities it may name.

Other query keys, empty or repeated values, userinfo, ports, traversal and malformed encoding are rejected; links hold at most 8,192 characters. Two files asserting one claim stay separately attributable, and removing one removes only its support. BF infers neither reverse nor chained relations: Alice owning the website does not make the website Alice's owner.

## Across brains

Search and read use the selected brains plus their direct `brains:` references, without recursion. A `bf://` address resolves only in its named brain, while backlinks can come from every selected brain. An address never adds a brain to the selection or grants access.

Explicit aliases resolve across the selected brains when one brain owns them. Conflicting owners produce `problems` and stop that resolution; exact refs still work. Equal relation names in two brains keep their own declared meanings.

`bf validate` checks targets and sections in its own brain, naming the file that wrote each link. It lists targets in other brains under `unresolved`, without opening them; they never make a brain invalid. Reads return identity resolution, backlinks and claims; they never infer across several hops.

## Tag rules

Add labels to a note's frontmatter, such as the New website project:

```yaml
# https://fmind.github.io/brain-framework/docs/link-reference/#tag-rules
tags: [website, product]
```

```bash
bf read tags
bf read bf://brain/tags/website
bf search "explanation" --scope bf://brain/tags/website
```

The directory lists both tags with `tag`, `ref` and `total`; the `website` page lists the note; the scoped search considers only notes carrying that tag. A plain search for `website` can also match prose. Reuse labels from `bf read tags`, and prefer a few lowercase, hyphenated words.

| Rule           | Behavior                                                                                                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Labels         | Exact and case-sensitive; 1–128 characters without control characters, surrounding spaces, slashes or `.`/`..`. Up to 1,000 per note; duplicates count once; numbers become text. |
| Members        | Only projects, concepts and `ACTION.md` notes join tag pages. Prose, record fields and links add no member: `[Website](bf://brain/tags/website)` does not tag a note.             |
| Ranking        | A note's tags weigh like its headings in search.                                                                                                                                  |
| Pages          | The directory counts distinct brain and tag pairs; a tag page lists members newest first. Both list 200 entries per page and have no sections.                                    |
| Several brains | Equal labels in different brains are different identities, combined on the page.                                                                                                  |
| Graph          | Each membership is a `tagged-with` claim supported by the whole note.                                                                                                             |
| Addresses      | `bf://NAME/tags/LABEL` belongs to computed pages: it cannot be an entity or alias. A search scope rejects `?rel=` on it; a read ignores it.                                       |

Tags need no concept note and imply no dependency. Provider labels stay collected evidence in record fields.
