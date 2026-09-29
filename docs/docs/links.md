---
description: Connect notes and records with citations, typed fields, explicit identities and declared relations.
---

# Linking knowledge

Links let you follow a decision to its evidence and see which work relies on it. Start with ordinary Markdown links; add relations and identities when they answer a question you actually ask.

## Connect two notes

Create the [New website decision](getting-started.md#save-a-decision) and the [Explain before asking for signup concept](brain.md#concepts). The concept already cites the decision in its `sources` metadata. To show that source in the body too, add this to the concept:

```markdown
[New website decision](../projects/new-website.md#decision)
```

Then read the destination:

```bash
bf read projects/new-website.md
bf validate
```

The project's `backlinks` include the concept in the `cites` group: an OKF `sources` entry means “derived from”, unlike a plain mention. The body link to the same note adds no second entry. A link from a note without that source would appear in the `links` group, which holds untyped links. Validation catches a missing target.

Each group previews its five newest items. A relation page lists all of them, newest first, with an excerpt of each:

```bash
bf read projects/new-website.md --rel cites
```

It returns `"relation":"cites"`, `"total":1` and the concept among its `items`; `--rel links` lists untyped links the same way.

Relative links start from the containing file: `../projects/` goes up from `concepts/`, then into `projects/`. `#decision` selects the Decision section; write `## Decision {#decision}` to keep that address when the heading is renamed. Link upstream evidence by its URL, or a collected record by its ref, such as `[Product brief](brief:website-brief)`.

## Name a relation

A relation says what a connection means. For example, a website review action depends on the decision it carries out:

```markdown
[Website decision](bf://brain/projects/new-website.md?rel=depends-on#decision)
```

A `bf://` address names a brain by the `name` in its `bf.yaml`, so it works in every clone. `?rel=` names the relation; only `bf://` links take it. Create the [example action](brain.md#actions), put that link in its Objective section, then read both notes:

```bash
bf read projects/new-website.md
bf read actions/2026-09-27_website-review/ACTION.md
```

The project groups the action under `depends-on`. The action's `claims` show the same link with its origin, the Objective section. The claim records your assertion; it does not prove the dependency.

### Choose the right relation

New brains declare four relations under `fields:` in `bf.yaml`:

| Relation     | Use when the source explicitly says…               | Link from the subject's note                                             |
| ------------ | -------------------------------------------------- | ------------------------------------------------------------------------ |
| `author`     | Who created it.                                    | `[Author](bf://brain/people/alice?rel=author)`                           |
| `owner`      | Who is responsible for it.                         | `[Owner](bf://brain/people/alice?rel=owner)`                             |
| `depends-on` | What it needs to operate or remain valid.          | `[Decision](bf://brain/projects/new-website.md?rel=depends-on#decision)` |
| `related-to` | A connection with no more specific known relation. | `[Related project](bf://brain/projects/new-website.md?rel=related-to)`   |

The Alice examples need the [person note](link-reference.md#bf-links). Use a plain Markdown link for a mention, and an OKF `sources` entry when the note derives from its target.

### Declare your own relation

For a review action that verifies the website, add `verifies` beneath the existing entries under `fields:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  verifies:
    description: The subject records a check of the target against stated criteria.
    type: identity
    cardinality: many
    relation: true
```

Put this in the action's Outcome section after recording the actual check:

```markdown
[Checked project](bf://brain/projects/new-website.md?rel=verifies)
```

`bf read projects/new-website.md --rel verifies` then lists the action. To list these checks on the project's `related-to` page too, add `broader: related-to` to `verifies`; see [narrower relations](schema.md#narrower-relations-and-allowed-targets). A link naming an undeclared relation stays searchable as an untyped link, and `bf validate` names the relation to declare.

- Write the link in the note making the claim: that note, or its entity, is the subject.
- Keep supporting observations in the same section, so readers can inspect them.
- BF never infers a reverse or chained relation.

## Set typed fields

A note can set declared fields in its frontmatter, under `fields:`, like a record does. For example, in the website project:

```yaml
fields:
  owner: [person:email/alice@example.test]
```

`owner` is a relation, so the project now claims Alice as its owner, with the whole note as origin. `bf read person:email/alice@example.test --rel owner` lists the project. Values are checked like a record's: an identity must be a `scheme:value`, and a relation's `targets` apply. A declared single-value field that is not a relation, such as a `status` string, shows beside the note in listings and backlink previews.

Write declared fields only under `fields:`. `bf validate` reports a declared relation written at the top of the frontmatter, such as `owner:`, because OKF would ignore it.

## Give a subject a stable identity

A file address is enough for most notes. When sources use different identifiers for one subject, declare them on its note:

```yaml
entity: bf://brain/projects/new-website
aliases: [repo:github.com/team/new-website]
resource: https://example.test/new-website
```

All of them now open the same note:

```bash
bf read bf://brain/projects/new-website
bf read repo:github.com/team/new-website
```

| Field      | Meaning                                                                                         |
| ---------- | ----------------------------------------------------------------------------------------------- |
| `entity`   | A logical `bf://` name for the subject; it creates no folder.                                   |
| `aliases`  | Other exact identities of the subject, such as a repository or an email address.                |
| `resource` | OKF: the URI of the asset the note describes. Links to that URI count as backlinks of the note. |

Identities are case-sensitive: write them as your sensors emit them, such as lowercase GitHub owners. `bf validate` warns about identities that differ only by letter case; see [Check your brain](checks.md#warnings). Declare only identities you have verified.

## Connect records and other brains

[Field mappings](schema.md#shared-fields-and-sensor-mappings) turn explicit provider fields, such as a commit's author identity, into relations. A field named `author` then connects a commit to a person's note without reading prose.

To follow a link into another brain, include that brain in your [selection](configuration.md#related-brains). A `bf://` address never downloads a brain or grants access. The [link reference](link-reference.md) defines address syntax, encoding and cross-brain rules.
