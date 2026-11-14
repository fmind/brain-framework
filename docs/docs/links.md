---
icon: lucide/link
description: Connect notes and records with citations, explicit identities, typed relations and attributes.
---

# Linking knowledge

Links connect decisions to supporting evidence and surface dependents when sources change. Start with plain Markdown links; add typed relations and identities when a recurring question needs them ([link reference](link-reference.md)).

## Connect two notes

Link notes with relative paths (or `#section` anchors — use `## Decision {#decision}` so renames preserve links) and collected records by `SOURCE:ID` (`[Product brief](brief:website-brief)`):

```markdown
[New website decision](../projects/new-website.md#decision)
```

```bash
bf read projects/new-website.md
bf read projects/new-website.md --rel cites
bf validate
```

OKF `sources` entries appear under `cites` backlinks, while plain Markdown links appear under `links`. Whole reads preview the 5 newest backlinks per relation, and `--rel` pages the complete group ([sections and encoding](link-reference.md#sections-and-encoding)).

## Name a relation

Add `?rel=RELATION` to a `bf://` URI to record a typed claim from the containing section:

```markdown
[Website decision](bf://brain/projects/new-website.md?rel=depends-on#decision)
```

```bash
bf read projects/new-website.md
bf read actions/2026-09-27_website-review/ACTION.md
```

The target groups the incoming link under `depends-on` (and open `draft` actions depending on another `draft` action surface `blocked_by` on `bf read review`), while the source note lists `{relation, target, origin}` in `claims`.

### Choose the right relation

Common [relation conventions](schema.md#standard-vocabulary) work with zero declaration:

| Relation     | Meaning                                   | Example                                                                  |
| ------------ | ----------------------------------------- | ------------------------------------------------------------------------ |
| `author`     | Explicit creator.                         | `[Author](bf://brain/people/alice?rel=author)`                           |
| `owner`      | Accountable owner.                        | `[Owner](bf://brain/people/alice?rel=owner)`                             |
| `depends-on` | Prerequisite decision, action or asset.   | `[Decision](bf://brain/projects/new-website.md?rel=depends-on#decision)` |
| `related-to` | Explicit link without narrower semantics. | `[Related](bf://brain/projects/new-website.md?rel=related-to)`           |
| `supersedes` | Replaces an earlier note kept as history. | `[Old decision](bf://brain/projects/old-website.md?rel=supersedes)`      |
| `part-of`    | Parent project, thread or container.      | `[Parent](bf://brain/projects/platform.md?rel=part-of)`                  |

### Declare relation metadata

Any valid camelCase or kebab-case relation (`verifies`, `implementsPart`) works directly without prior configuration. Add optional metadata, `broader` rollups or `targets` prefix bounds under top-level `fields:` in `bf.yaml` ([narrower relations](schema.md#narrower-relations-and-allowed-targets)):

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  verifies:
    description: The subject records a check of the target against stated criteria.
    broader: related-to
```

Write `[Checked project](bf://brain/projects/new-website.md?rel=verifies)` in the asserting note's section; BF never infers reverse or transitive relations.

## Set typed relations and attributes

Set non-`bf://` relations in OKF frontmatter under `relations:` and arbitrary metadata under `attributes:` (never scanned as edges):

```yaml
relations:
  owner: [person:email/alice@example.test]
attributes:
  stage: alpha
```

`relations` requires valid explicit `scheme:value` or `bf://` identities.

## Give a subject a stable identity

When multiple tools refer to the same project, person or repo, bind those exact case-sensitive identities on its owning note:

```yaml
entity: bf://brain/projects/new-website
aliases: [repo:github.com/example/new-website]
resource: https://example.test/new-website
```

```bash
bf read bf://brain/projects/new-website
bf read repo:github.com/example/new-website
```

- `entity` names a logical `bf://` subject in this brain (never a `#section`, `.md` file, page, `SOURCE:ID` record or `bf://brain/actions/SLUG` action folder — [identity rules](link-reference.md#identity-rules)).
- `aliases` and `resource` bind external `scheme:value` identities and asset URIs (`bf validate` [warns](checks.md#warnings) on case-only mismatches).
- Create `concepts/me.md` (`entity: bf://brain/people/me`, `aliases: [person:email/me@example.test, person:github.com/me]`) to query or export everything involving you via `bf read bf://brain/people/me --rel participant` or `bf export --kind items --scope bf://brain/people/me --observed-since 7d`.

Sensors emitting `relations` connect collected records to these identities automatically ([ontology rules](schema.md#shared-fields-and-sensor-mappings)).
