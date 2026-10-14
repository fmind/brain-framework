# Identities, tags and relations

Use when changing a note's tags, identity or relations; the [link reference](https://fmind.github.io/brain-framework/docs/link-reference/) owns the exact rules. Never infer an identity or a relation from similar names or prose: write only what a source or the user states.

## Link notes and name a relation

Relative links start from the file holding them; in `projects/` and `concepts/`, a leading `/` starts from that folder. A link to a page, such as `tasks`, takes no `#section`.

A typed link states a relation: `[Decision](bf://NAME/projects/new-website.md?rel=depends-on#decision)`, where `NAME` is the `name` in `bf.yaml`.

- `?rel=` is the only query a `bf://` link takes; it precedes the fragment and is rejected on any other link.
- The subject is the note's `entity`, otherwise its file: to state another subject's relation, write the link in that subject's note.
- The link's section is the claim's `origin`: keep the supporting observation beside it.
- In a `bf://` link to a record, percent-encode a `#` or `?` of its id as `%23` or `%3F`.

Declare each relation once under `fields:` in `bf.yaml`: `bf init` declares `author`, `owner`, `depends-on` and `related-to`, and `cites`, `links` and `tagged-with` are reserved. `bf validate` names an undeclared relation, whose links stay untyped. Merge a new relation into the existing mapping, since a second `fields:` key is invalid:

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

`broader: PARENT` makes a relation a narrower kind of another; `targets: ["repo:"]` restricts the identities it may point to, failing collection or validation outside them.

## Set declared fields in a note

A relation to an identity that is not a `bf://` address, such as a person or a repository, belongs under the note's frontmatter `fields:`, where the whole note supports it:

```yaml
fields:
  owner: [person:email/bob@example.test]
  depends-on: [repo:github.com/example/new-website]
```

A declared field at the top level of the frontmatter asserts nothing.

## Give a subject a stable identity

A file address is enough for most notes. When sources name the same subject differently, a project, concept or `ACTION.md` note may declare:

- `entity: bf://NAME/people/ID`, or another logical path of this brain, when the note owns that identity.
- `aliases:` with verified namespaced identities (`scheme:value`), never display names, spelled as the sensors write them, since identities are case-sensitive.
- `resource:` with the URI of the asset the note describes, such as `https://github.com/example/new-website`.

Link to a record ref such as `jira:PROJ-1` rather than repeating it as an alias, which would make both owners ambiguous. A BF identity uses this brain's own name and names a whole subject, never a `#section`, note file, page or `SOURCE:ID`; link to another brain instead of claiming its identity. Retype an identity that `bf validate` reports for an invisible character. Give sections that need a durable ref an explicit anchor (`## Display title {#stable-id}`), and keep the brain's `name` stable across clones.

## Reuse topic labels

Browse `bf read tags` before adding `tags: [agents, retrieval]`, and reuse established lowercase hyphenated labels. Tags are exact, case-sensitive and local to their brain; linking to `bf://NAME/tags/LABEL` does not tag a note.

## Relate another brain

Related brains belong in `bf.yaml` as `brains: {team: {path: ../team}}`, relative to the declaring brain; search and read include direct references only. A link to another brain never adds it to retrieval, and `bf validate` lists such links under `unresolved`. References and registration never grant permission to run another brain's programs.
