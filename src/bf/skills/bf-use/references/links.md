# Identities, tags and relations

Use when changing a note's classification, its stable identity or its explicit relations. The [link reference](https://fmind.github.io/brain-framework/docs/link-reference/) owns the exact rules. Never infer an identity or a relation from similar names or prose: write only what a source or the user states.

## Link notes and name a relation

Relative links start from the file that holds them. In `projects/` and `concepts/`, a leading `/` starts from that folder, so `[Tables](/tables.md)` in a concept names `concepts/tables.md`; `ACTION.md` notes and their attachments use ordinary relative links. Embedded images are checked like links. A link to a page that `bf read` opens (home, `tasks`, `tags`, a declared tag, an existing folder under `projects/`, `concepts/` or `actions/`, a period, a known source) is valid too.

A typed link states a relation: `[Decision](bf://NAME/projects/new-website.md?rel=depends-on#decision)`, where `NAME` is the `name` in `bf.yaml`. `?rel=` is the only query a `bf://` link takes, it precedes the fragment, and it types `bf://` links only: `bf validate` rejects it on any other identity. The subject is the note's `entity`, otherwise its file; to state another subject's relation, write the link in that subject's note. The link's section is the claim's `origin`, so keep the supporting observation beside it.

Declare each relation once under `fields:` in `bf.yaml`, with a `description`, `type: identity` and `relation: true`. `bf init` declares `author`, `owner`, `depends-on` and `related-to`; `cites`, `links` and `tagged-with` are reserved. A link naming an undeclared relation stays searchable as an untyped link, and `bf validate` names the relation to declare. Merge a new relation into the existing mapping; a second `fields:` key is invalid:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  # Keep the existing relations, such as depends-on.
  supersedes:
    description: This authored decision explicitly replaces the target decision.
    type: identity
    cardinality: many
    relation: true
```

Two optional settings refine a relation. `broader: PARENT` makes it a narrower kind of another declared relation, one level deep: `--rel PARENT` then also lists its items, each naming its own `relation`. `targets: ["repo:"]` restricts the identities it may point to: collection fails without changing evidence when a mapped value falls outside them, and `bf validate` reports notes and records that do. An OKF note's `sources` entries become `cites` claims, so `bf read 'REF' --rel cites` lists what cites an item.

## Set declared fields in a note

A relation to an identity that is not a `bf://` address, such as a person or a repository, belongs under the note's frontmatter `fields:`, validated like a record's mapped fields:

```yaml
fields:
  owner: [person:email/bob@example.test]
  depends-on: [repo:github.com/example/new-website]
```

The whole note supports these claims. A declared field written at the top level of the frontmatter asserts nothing, and `bf validate` says to move it under `fields:`. A declared single-value field that is not a relation, such as a status, appears as a short fact in listings and backlink previews.

## Give a subject a stable identity

A file address is enough for most notes. When several sources name the same subject differently, a project, concept or `ACTION.md` note may declare:

- `entity: bf://NAME/people/ID` (or another logical path in this brain's namespace), when the note owns that identity.
- `aliases:` with verified namespaced identities (`scheme:value`), never display names. Identities are case-sensitive: write them as the sensors do (the reviewed examples lowercase GitHub owners and names).
- `resource:` with the URI of the asset the note describes, such as `https://github.com/example/new-website`: it becomes one of the note's identities, like an alias.

A record ref such as `jira:PROJ-1` is already an identity: link to it rather than repeating it as an alias, which would make both owners ambiguous. BF entities and aliases must use this brain's own namespace; link to another brain instead of claiming its identity. [Action attachments](actions.md#metadata-and-attachments) ignore these keys.

`bf read IDENTITY` returns the owning note, or the links to the identity when nothing owns it: `backlinks` grouped by relation (5 newest each) and `claims` with that subject, each with its `origin` and the origin's `date` (a note) or `time` (a record). `bf search 'IDENTITY'` gives each linking item's `relations` with their origin and target section. Use explicit heading anchors (`## Display title {#stable-id}`) when a section needs a durable ref, and keep the brain's `name` stable across clones.

## Reuse topic labels

Browse `bf read tags` before adding frontmatter such as `tags: [agents, retrieval]`. Prefer established lowercase hyphenated labels over parallel spellings. Tags are exact, case-sensitive and local to their brain; they classify authored notes, while concepts explain knowledge and typed links declare relations. Linking to `bf://NAME/tags/LABEL` does not tag a note. After tagging, read the returned `bf://NAME/tags/LABEL` page or search within it with `--scope`.

## Relate another brain

Related brains belong in `bf.yaml` as `brains: {team: {path: ../team}}`, with paths relative to the declaring brain. Search and read include direct references only, never their references; check `problems` before claiming absence. A link to another brain never adds it to retrieval or contacts a network, and `bf validate` lists such links under `unresolved` without opening them. References and registration never grant permission to run another brain's programs.
