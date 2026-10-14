# Track a work session

Details for the loop in the [skill](../SKILL.md#start-an-action). Routines with `output: action` also write actions, for review.

## Start a session

The helper creates `actions/YYYY-MM-DD_topic/ACTION.md` with the template's empty sections. When that folder exists, it adds a 12-hex suffix (`actions/2026-09-29_website-review-3f9a1c2e5b7d/`) instead of joining it; routine actions take 8, so a session never counts as a routine's action for the day. `--unique` always adds the suffix, so sessions from other clones never meet in one folder after a merge. It refuses linked `actions/` folders and removes what it created when writing fails. Without the helper, create the folder exclusively, add a suffix from `python3 -c 'import uuid; print(uuid.uuid4().hex[-12:])'` on a collision or in a shared brain, and never reuse or rename an existing action.

Fill the sections from the [action template](../templates/action.md) with the authorized objective, a relative link to the owning project, constraints, tasks and the next step, from observed decisions and refs, never sample text; omit unused sections. Create `inputs/` and `outputs/` only for approved files, and link the action from the project only when durable next steps change. For consequential choices, intentions and blocking questions, follow the `bf-use` skill's decision guide.

## Resume a session

1. Find the action with `bf read actions`, whose `tasks` counts show open work, or `bf search "topic words" --scope actions`, and resume it by its exact ref, never by topic alone; with several brains, by its `uri` (`bf://NAME/actions/...`).
1. Read `bf read 'actions/YYYY-MM-DD_topic/ACTION.md#context'` and the same ref's `#resume`; when a section is missing, the error lists the note's sections, so read the whole action once. Then read the owning project's current decision and only what the next step needs, within the [read budget](context.md#read-on-resume).
1. When the action is done, report its Outcome and ask whether to start a follow-up action that links it; never reopen it silently.
1. Continue from Resume within the current request's authorization; ask only when the next step is ambiguous or needs authority you do not have.

## Finish or hand off

1. During the work, tick completed tasks and keep decisions with their reasons and evidence refs. Update Context when its facts change, and Resume with the last verified state, blocker and exact next step.
1. Before a handoff or a planned compaction, follow the [handoff guide](handoff.md); the action stays open.
1. To close it, fill Outcome and compare any expectation with the observed evidence. Resolve every open box: tick it, move it to the owning project's next actions or drop it with the reason in Outcome. Move durable changes and unresolved questions into the owning project with the `bf-use` skill's guide to writing knowledge back. The done action leaves `bf read tasks`.
1. Run `bf validate`, fix what your edit introduced and show the diff. Report the action ref, verified outcome and next step; commit only when authorized.

## Metadata and attachments

An `ACTION.md` uses `type: action` and `status: draft|stable|deprecated`, a maturity level, never progress; only `deprecated` retires an action. Finishing work is not verification: add `verified` events only after actual checks. Files in `inputs/` and `outputs/` are ordinary Markdown: only `title`, `type`, `status`, `updated`, `summary` and `description` apply, their typed links use the file as subject, and `entity`, `aliases`, `tags`, `sources` and `stale_after` are ignored. Action Markdown is searchable: keep private inputs out of shared brains.
