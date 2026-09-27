---
name: bf-learn
description: Keep Brain Framework project notes, concepts and action folders current after meaningful work. Use after a decision, a finished action, a corrected assumption, or when the user asks to remember something or review their knowledge.
license: MIT
compatibility: Requires Brain Framework 13 (the bf command) on Linux or macOS.
metadata:
  version: "13.0.1"
---

# bf-learn

Knowledge is Markdown in the brain; Git keeps its history. Keep each note short and current so the next session can act on it.

Load only the guide needed: [periodic review](references/review.md) for stale projects, unresolved questions and decision outcomes; [evidence and impact](references/evidence.md) for retained revisions, disputed beliefs and bounded dependency review; [consolidation](references/consolidate.md) for deriving a reusable procedure from outcomes; [sharing](references/share.md) for preparing selected knowledge for another brain. These are optional workflows over existing files; they do not run automatically.

1. Select the intended brain and audience. Find the owning note with `bf search "project or topic" --brain NAME`. Prefer updating it over creating a new one. Read it fully before editing; resolve the brain path before using filesystem tools.
1. Choose the place: a project's state, decisions and next actions in `projects/<project>.md`; reusable knowledge in `concepts/<concept>.md`; one session of work the user asked to track as an action in `actions/YYYY-MM-DD_slug/ACTION.md` (see `bf-action`); media shared by several notes in `assets/`; a repeated procedure in the brain's `skills/`.
1. Reuse topic tags from `bf read tags`; add a few useful labels to project, concept and action frontmatter (`tags: [agents, retrieval]`). Prefer lowercase hyphenated names, retain `type` and `status` for their existing meanings, and avoid parallel spellings. Tags connect topics; typed links state dependencies and other roles. Create a concept only when it has knowledge to explain. After changing tags, read their returned `bf://NAME/tags/LABEL` refs and check an exact tag scope. Never infer classification from provider labels or edit collected records to tag them.
1. Make focused edits in place; preserve unrelated paragraphs and stable anchors. Keep session detail in its action and update the project only when durable state changes. Replace outdated statements instead of appending history sections; add a dated one-line entry under `## Decisions` for each decision, with its reason and a ref to the evidence (`[meeting](source:id)`). Set `updated: YYYY-MM-DD`.
1. Only write what you verified or the user stated. Mark proposals as proposals. Never invent provenance, verification or dates.
1. When records disagree, compare upstream revision time, observation time and declared collection coverage; do not silently turn a partial or historical record into a current fact. Preserve selected prior evidence before a consequential belief revision, and distinguish disputed from superseded claims. Review explicit dependencies to at most two hops and ten distinct dependents; report limits. Promote only reviewed, shareable summaries into a team brain, with evidence teammates can access.
1. Run `bf validate --brain NAME` and fix problems introduced by the edit; report unrelated failures without deleting evidence to pass a check. When the edit changes an answer the brain should retain, add or update its `evals/retrieval.yaml` case and run `bf eval --brain NAME`. `bf read projects` derives review reminders from local file modification time (14 days by default), an optional `review_after` interval or an explicit `review_due` date, and newer linked evidence (`new_links`). Read `review_reasons` and the supporting backlinks. File edits, including copying or touching a file, are activity signals rather than proof of review; do not touch a note merely to clear the reminder. A deadline remains due until deliberately changed. Use explicit review metadata to opt concepts or canonical actions into reminders. Keep `updated` for meaningful content changes. Show the diff; commit only when the user's standing instructions allow it.

Project notes follow this shape:

```markdown
---
type: project
status: draft # draft | stable | deprecated
updated: 2026-09-22
tags: [retrieval]
summary: One sentence on what this project is for.
aliases: [repo:github.com/owner/name]
---

# Project

Intent in two or three sentences.

## Now

Current state in a short paragraph, with links to canonical documents or repositories.

## Decisions

- 2026-09-22: decision, because reason ([evidence](source:id)).

## Next actions

- [ ] The single most important next step.
```

Projects, concepts and action `ACTION.md` notes follow OKF v0.2 ([concept template](templates/concept.md), [action template](../bf-action/templates/action.md)): a `type`, `status: draft|stable|deprecated`, `sources` as mappings with a `resource`, and `verified` only for real checks with `by` and `at`. Keep work progress in the body and task list. Keep `concepts/index.md` a short list of entry points. Actions follow the `bf-action` skill and keep a Resume section with the exact next action. Keep next actions as task list items (`- [ ]`) so pages count them and show the first open one; `bf read tasks` returns the complete paginated open list with source refs. Summaries should use plain bullets or refs, not copies of open checkboxes that become new tasks.

Replace sample dates, identities and refs with verified values. Do not rewrite collected records to make a note true; update the authored interpretation and retain its evidence.

After substantial work, recommend one concrete update to the user when it would save a future session time: what to change, where, and why.

Shared field meanings, types, cardinality and examples live under `schema` in `bf.yaml`; sensor `fields` map explicit output paths or constants into them. Read an identity (`bf read IDENTITY`) to follow its typed relationships and read their supporting records. Keep technical checks in `tests/` and retrieval suites in `evals/`; `bf eval` runs all suites, while `--path evals/NAME.yaml` selects one.

## Portable links and relationships

Use stable `bf://<bf.yaml name>/...` addresses across brains. An authored note may declare `entity: bf://NAME/people/ID` (or another logical namespace), with verified alternate identities in `aliases`. BF entities and aliases must use this brain's own namespace; link to a foreign brain instead of claiming its identity. Declare each role in `bf.yaml` (`type: identity`, `relation: true`) before writing `[label](bf://NAME/path?rel=ROLE#section)`. The subject is the note entity, otherwise its file: to state another entity's relationship, write the link in that entity's note. `rel` is the only BF link query and precedes the fragment. Keep authorship and ownership in named relationships, not URI userinfo. Other URI schemes retain their original query semantics.

`bf read IDENTITY` returns incoming links grouped by relationship under `backlinks` and claims with that explicit subject under `claims`. Read the returned `relations[].origin`: the exact section or record that makes each claim. Exact reads accept BF addresses. Use explicit heading anchors (`## Display title {#stable-id}`) when a section needs a durable ref. Keep the brain's `name` stable across clones. Selected-brain scope is a boundary: links never add another brain or contact a network. Ambiguous aliases and incomplete searches need review; `bf validate` reports foreign links under `unresolved` without opening them.

Related brains belong in `bf.yaml` as `brains: {team: {path: ../team}}`. Use stable matching names and paths relative to the declaring root. Search/read include direct references only; check `problems` before claiming absence. References never grant sensor execution permission.

When concurrent revisions conflict, follow the [resolution guide](../bf-maintain/references/conflicts.md); do not silently select a winner or invent consensus.
