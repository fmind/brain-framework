---
description: Connect notes and records with citations, explicit identities and declared relationships.
---

# Linking knowledge

Links let you follow a decision to its evidence and see which other work relies on it. Start with ordinary Markdown links; add explicit identities and relationship roles when they answer a question you actually ask.

## Connect two notes

Create the [New website decision](getting-started.md#save-a-decision) and the [Explain before asking for signup concept](brain.md#concepts). The concept already cites the decision in its `sources` metadata. To make that source visible in the body too, add this to the concept:

```markdown
[New website decision](../projects/new-website.md#decision)
```

Then read the destination:

```bash
bf read projects/new-website.md
bf validate
```

The project's `backlinks` include `concepts/explain-before-signup.md` in the `cites` group: each OKF `sources` entry is a built-in `cites` claim, so "derived from" stays distinct from a mention. The body link to the same note adds no second entry; a link from a note without that source would appear in the `links` group, which holds links without a declared relationship. You can now follow the concept to its evidence, or read the project to see where its decision is used. Validation catches a missing local target.

Each group previews its five newest items. A role page lists all of them, newest first, with an excerpt of each:

```bash
bf read projects/new-website.md --rel cites
```

It returns `"relation":"cites"`, `"total":1` and the concept among its `items`; `--rel links` lists untyped links the same way. See [role pages](retrieval.md#role-pages).

Relative links start from the containing file: `../projects/` goes up from `concepts/`, then into `projects/`. The `#decision` fragment selects the Decision section.

Use a source URL for upstream evidence, or a returned record ref such as `[Product brief](local-documents:website-demo/brief.txt)` after completing the [sensor walkthrough](sensors.md). A hub note can also link to a page, such as `[This week](bf://brain/7d)`; see [page addresses](link-reference.md#page-addresses).

## Name a relationship

A relationship says what the connection means. For example, a website review action depends on the decision it is carrying out:

```markdown
[Website decision](bf://brain/projects/new-website.md?rel=depends-on#decision)
```

Create the [example action](brain.md#actions), then put that link in its Objective section. New brains declare `depends-on` in `bf.yaml`; custom roles must be declared before use. `brain` is the configured brain name, which stays stable across clones.

Read the decision's whole note to inspect its incoming backlinks, then the action to inspect the claims it makes, replacing the action path with the ref returned by `bf read actions`:

```bash
bf read bf://brain/projects/new-website.md
bf read 'actions/2026-09-27_website-review-SUFFIX/ACTION.md'
```

Look for a claim with the website-review action as its subject, `depends-on` as its role, the project decision as its target and the Objective section as its origin. This makes the meaning and its source inspectable; it does not prove the dependency or infer further ones.

### Choose the right role

New brains declare these roles in `bf.yaml`:

| Role         | Use when the source explicitly says…                    | Link from the subject's note                                             |
| ------------ | ------------------------------------------------------- | ------------------------------------------------------------------------ |
| `author`     | Who created it.                                         | `[Author](bf://brain/people/alice?rel=author)`                           |
| `owner`      | Who is responsible for it.                              | `[Owner](bf://brain/people/alice?rel=owner)`                             |
| `depends-on` | What it needs to operate or remain valid.               | `[Decision](bf://brain/projects/new-website.md?rel=depends-on#decision)` |
| `related-to` | There is a connection with no more specific known role. | `[Related project](bf://brain/projects/new-website.md?rel=related-to)`   |

The Alice examples require the [person note](link-reference.md#bf-links). Use an ordinary Markdown link when you only need a mention, and an OKF `sources` entry, or the built-in `?rel=cites`, when the note derives from its target.

### Declare your own role

For a review action that explicitly verifies the website, add `verifies` alongside the existing fields under `schema:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
schema:
  verifies:
    description: The subject records a check of the target against stated criteria.
    type: identity
    cardinality: many
    relation: true
```

Add this to the action's Outcome section after recording the actual check:

```markdown
[Checked project](bf://brain/projects/new-website.md?rel=verifies)
```

```bash
bf validate
bf read projects/new-website.md
```

The project read groups the incoming link under `verifies`, and `bf read projects/new-website.md --rel verifies` lists every item making that claim. The action's read shows the claim with its origin, the Outcome section. The relationship records your assertion, not an automatic certification.

To also list these checks on the project's `related-to` role page, add `broader: related-to` to `verifies`. `bf read projects/new-website.md --rel related-to` then includes the review action with `"relation":"verifies"`; its backlink group stays `verifies`. A broader role goes one level only; see [narrower roles](schema.md#narrower-roles-and-allowed-targets).

- Write the link in the note making the claim; its entity (or file) is the subject.
- The address after `bf://` identifies the target; `?rel=` names the meaning.
- Keep supporting observations in the same section so readers can inspect them.
- Reverse or chained relationships are never inferred.

## Give a subject a stable identity

A file address is enough for most notes. When several sources use different identifiers for the same subject, an explicit entity and aliases can connect them. For example, add these fields to the New website project's existing frontmatter:

```yaml
# https://fmind.github.io/brain-framework/docs/links/
entity: bf://brain/projects/new-website
aliases: [repo:github.com/team/new-website]
```

Both addresses now open the same note:

```bash
bf read bf://brain/projects/new-website
bf read repo:github.com/team/new-website
```

The entity is a logical identifier; it does not create a folder. The repository alias is fictional: use your actual repository identity, written in lowercase, such as `repo:github.com/googlecloudplatform/open-knowledge-format`. Identities are case-sensitive, and the example hook and Git history sensor lowercase GitHub owners and names. `bf validate` lists identities that differ only by letter case under `warnings`, without failing, so you can choose one spelling; see [check your brain](checks.md). Only declare aliases you have verified; similar names alone never establish that two subjects are the same.

Use `## Decision {#decision}` when the section address must survive a heading rename.

## Connect collected records and other brains

[Sensor mappings](schema.md#shared-fields-and-sensor-mappings) normalize explicit provider fields, such as an author's identity, into shared relationships. A field named `author` can connect a commit to a person's note without guessing from prose.

To read a link into another brain, include that brain in your [selection](configuration.md#related-brains). A BF address never downloads a brain or expands access by itself.

The [link reference](link-reference.md#bf-links) defines address syntax, aliases, roles and cross-brain resolution. Use it when building integrations or diagnosing invalid links.
