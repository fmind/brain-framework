# Identities, tags and relations

Use when changing tags, identities or relations; the [link reference](https://fmind.github.io/brain-framework/docs/link-reference/) owns exact rules. Never infer identities or relations from prose or similar names.

## Links and relations

- Relative links resolve from the containing file; in `projects/` and `concepts/`, a leading `/` resolves from that bundle root. Page links (`tasks`, `review`) take no `#section`.
- Typed links use `bf://` with `?rel=` before any `#section`: `[Decision](bf://NAME/projects/new-website.md?rel=depends-on#decision)`. The subject is the note's `entity` (else its file) and the containing section is `origin`. Percent-encode `#` and `?` in record ids (`%23`, `%3F`) inside `bf://` links.
- Relations are declared once under top-level `fields:` in `bf.yaml` (`bf init` declares the [standard vocabulary](https://fmind.github.io/brain-framework/docs/schema/#standard-vocabulary), including `owner`, `depends-on`, `related-to`, `supersedes`; `cites`, `links`, `tagged-with` are reserved). Merge new entries into the existing `fields:` mapping:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  verifies:
    description: This authored check explicitly verifies the target claim.
    type: identity
    cardinality: many
    relation: true
```

`broader: PARENT` rolls narrower claims into parent `--rel` pages; `targets: ["repo:"]` restricts allowed identity prefixes. Non-`bf://` relations on a note go in frontmatter `fields:` (`owner: [person:email/bob@example.test]`).

## Stable identities, tags and related brains

- OKF notes may declare `entity: bf://NAME/people/ID`, `aliases: [scheme:value]` (exact, case-sensitive identities, never display names or `SOURCE:ID` record refs) and `resource:` URIs.
- A `bf://` identity must use this brain's `name` and name a whole logical subject — never a `#section`, `.md` file, page, `SOURCE:ID` record or `bf://NAME/actions/SLUG` action folder (link to the action instead). Use `{#stable-id}` anchors on durable headings.
- Check `bf read tags` and reuse lowercase hyphenated `tags: [agents, retrieval]`; prose links to `bf://NAME/tags/LABEL` do not tag a note.
- Declare related brains in `bf.yaml` (`brains: {team: {path: ../team}}`) for one-hop retrieval; foreign links alone stay `unresolved` in `bf validate` and never grant execution authority.
