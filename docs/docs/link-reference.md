---
icon: lucide/tags
description: Exact rules for BF addresses, section anchors, identities, relation links and tags.
---

# Link and tag reference

Start with [Linking knowledge](links.md) for practical examples. This page defines exact BF address, identity, relation and tag contracts.

## BF links

```text
bf://brain/projects/new-website.md?rel=depends-on#decision
```

| Component                  | Meaning                                                          |
| -------------------------- | ---------------------------------------------------------------- |
| `bf://`                    | Offline Brain Framework URI scheme.                              |
| `brain`                    | Stable `name` in `bf.yaml` (shared across clones).               |
| `/projects/new-website.md` | Note path, logical entity (`people/alice`), page or `SOURCE:ID`. |
| `?rel=depends-on`          | Optional declared relation (or `cites`), before `#fragment`.     |
| `#decision`                | Optional heading anchor on a note or entity target.              |

A first path segment containing `:` always addresses a `SOURCE:ID` record (`bf://brain/local-documents:website-demo/brief.txt`). Logical entity paths (`entity: bf://brain/people/alice` with `aliases: [person:email/alice@example.test]` in `concepts/alice.md`) resolve to their owning OKF note without requiring a `people/` folder.

### Identity rules

`bf validate` enforces these rules on OKF `entity`, `aliases` and URI `resource` fields, record `aliases`, and `type: identity` field values:

| Rule          | Requirement                                                                                                                                                                                                                                                                                                       |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Own namespace | A `bf://` entity, alias or resource must use its own brain's `name` (`bf://brain/...`).                                                                                                                                                                                                                           |
| Whole subject | A `bf://` entity, alias or resource must not carry `#section`, name a `.md` note file, name a computed page, name a `SOURCE:ID` record, or point to an action folder `bf://NAME/actions/SLUG` (`a BF path to an action folder in actions/ names its ACTION.md and no other subject; link to the action instead`). |
| Visible       | Must not contain Unicode format (`Cf`) characters such as U+200B, soft hyphens or bidi/tag controls (even percent-encoded in `bf://`).                                                                                                                                                                            |
| Bounded       | At most 1,000 aliases per note or record, each <= 8,192 characters.                                                                                                                                                                                                                                               |

Duplicate identity claims across notes or records fail `bf validate` and fail `bf read IDENTITY` with `ambiguous identity` rather than merging silently.

### Page addresses

`bf://brain/` opens Home, and `bf://brain/review`, `bf://brain/projects`, `bf://brain/tasks`, `bf://brain/tags`, `bf://brain/7d` or `bf://brain/memories/SOURCE` open computed [pages](retrieval.md#pages).

- Bare `bf://brain` (no `/`), trailing slashes (`bf://brain/projects/`), empty path segments and `#section` fragments on pages (`page cannot select a section`) are invalid.
- Linking to an action folder (`bf://brain/actions/2026-09-27_website-review` or `bf read actions/SLUG --rel ...`) resolves to its `ACTION.md`.

### Sections and encoding

- **Explicit anchors**: `## Heading {#contact}` (`[A-Za-z0-9][A-Za-z0-9_.-]*` at the end of the heading line after a space) survives heading renames; otherwise headings use their slug (`-1`, `-2` on duplicates). Anchors ending in `.md`, duplicate anchors or bare trailing `#` on `bf://` addresses are invalid; records take no `#fragment`.
- **Percent-encoding**: inside `bf://` addresses, encode spaces as `%20`, literal `#` as `%23` and `?` as `%3F` (`%23` also in relative file links). Non-`bf://` targets read verbatim (`[Mail](mail:a#b)` names `mail:a#b`; wrap spaces in `<...>`).
- **Relative links**: resolve from the containing file; in `projects/` and `concepts/`, leading `/` resolves from that bundle root (`/tables/customers.md` -> `concepts/tables/customers.md`). Case must match exact filenames even on case-insensitive filesystems.

## Relation links

Write typed links as `[Alice](bf://brain/people/alice?rel=owner)` or in OKF frontmatter `fields:` / `sources:`. Each graph claim records `(subject, relation, target, origin)`:

- `subject` is the note's `entity` if declared, else its file URI (or record URI). Links inside OKF footnotes (`[^1]: ...`, including indented continuations) attribute `origin` to the section that first cites `[^1]`.
- `rel` is the only allowed query parameter on `bf://` links (`?rel=` on non-`bf://` URLs is reported by `bf validate`).
- Undeclared `?rel=` values keep the note searchable as an untyped link while failing `bf validate` (`link relation X is undeclared...`, `field X is not a relation...`, `remove ?rel=links`, or `tagged-with is reserved`).
- Built-in relations (reserved, never declared in `bf.yaml`): `cites` (link-shaped `sources[].resource` entries), `tagged-with` (note `tags`) and `links` (untyped backlinks).

## Across brains

Retrieval spans the selected brains and their direct `brains:` references (one hop, never recursive):

- `bf://NAME/...` resolves only in brain `NAME` and never grants access to unselected brains; explicit aliases and bare `SOURCE:ID` backlinks gather across selected brains when unambiguous.
- `bf validate` checks its own brain offline and lists cross-brain targets under `unresolved` without failing validation.

## Tag rules

Declare tags in OKF frontmatter (`tags: [website, product]`) and browse or scope with `bf read tags`, `bf read bf://brain/tags/website` and `bf search "query" --scope bf://brain/tags/website`:

- Exact, case-sensitive strings of 1–128 characters (no control chars, surrounding spaces, `/` or `.`/`..`; up to 1,000 per note; percent-encode `#` as `bf://brain/tags/C%23`).
- Only `projects/`, `concepts/` and `ACTION.md` notes join tag pages (200 entries/page, newest first, weighted above body text in search); prose links to `bf://brain/tags/LABEL` do not tag a note, and `bf://NAME/tags/LABEL` cannot be claimed as an `entity`, `alias` or `resource`.
