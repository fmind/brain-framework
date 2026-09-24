---
name: fkf-learn
description: Keep FKF project notes, wiki concepts and task folders current after meaningful work. Use after a decision, a finished task, a corrected assumption, or when the user asks to remember something.
license: MIT
---

# fkf-learn

Knowledge is Markdown in the base; Git keeps its history. Keep each note short and current so the next session can act on it.

1. Select the intended base and audience. Find the owning note with `fkf search --base NAME`. Prefer updating it over creating a new one. Read it fully before editing; resolve the base path before using filesystem tools.
1. Choose the place: a project's state, decisions and next actions in `projects/<project>.md`; reusable knowledge in `wiki/<concept>.md`; a delegated or multi-session job in `tasks/YYYY-MM-DD_slug/TASK.md` with `inputs/` and `outputs/`; a repeated procedure in the base's `skills/`.
1. Edit in place. Replace outdated statements instead of appending history sections; add a dated one-line entry under `## Decisions` for each decision, with its reason and a ref to the evidence (`[meeting](source:id)`). Set `updated: YYYY-MM-DD`.
1. Only write what you verified or the user stated. Mark proposals as proposals. Never invent provenance, verification or dates.
1. When records disagree, compare upstream revision time, observation time and declared collection coverage; do not silently turn a partial or historical record into a current fact. Promote only reviewed, shareable summaries into a team base, with evidence teammates can access.
1. Run `fkf validate --base NAME` and fix problems introduced by the edit; report unrelated failures without deleting evidence to pass a check. When the edit changes an answer the base should retain, add or update its `queries.yaml` case and run `fkf eval --base NAME`. `fkf status` lists active or blocked project notes due for `review` (not updated for 14 days); refresh one when you touch its project. Show the diff; commit only when the user's standing instructions allow it.

Project notes follow this shape:

```markdown
---
type: project
status: active # active | paused | blocked | done | archived
updated: 2026-09-22
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

Wiki concepts follow OKF v0.2 ([template](templates/concept.md)): a `type`, `status: draft|stable|deprecated`, `sources` as mappings with a `resource`, and `verified` only for real checks with `by` and `at`. Keep `wiki/index.md` a short list of entry points. Tasks start from the [task template](templates/task.md) and keep a Resume section with the exact next action.

Replace sample dates, identities and refs with verified values. Do not rewrite collected records to make a note true; update the authored interpretation and retain its evidence.

After substantial work, recommend one concrete update to the user when it would save a future session time: what to change, where, and why.
