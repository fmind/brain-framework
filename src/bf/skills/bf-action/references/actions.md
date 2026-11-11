# Track and settle a work session

Details for [bf-action](../SKILL.md). Routines (`output: action`) and assistants (`propose: true`) also create `status: draft` actions for review.

## Start a session {#start-a-session}

- `bf action new SLUG [--title TITLE] [--summary SUMMARY] [--unique]` creates `actions/YYYY-MM-DD_SLUG/ACTION.md` (`type: action`, `status: draft`) with the canonical OKF sections, validates it and refreshes `.bf/`.
- In multi-writer Git brains, pass `--unique` to add a 12-hex suffix (`actions/2026-09-29_website-review-3f9a1c2e5b7d/`) distinct from 8-hex routine actions. Never reuse or rename an existing folder.
- Populate sections from [templates/action.md](../templates/action.md) with observed refs and a relative link to the owning `projects/*.md` note; declare blocking actions with `depends-on` so `bf read review` surfaces `blocked_by`. Put artifacts in `inputs/` and `outputs/` only when needed.

## Resume a session {#resume-a-session}

1. Locate open sessions via `bf read review`, `bf read actions` or `bf search "topic words" --scope actions` and address the exact ref (or `uri` across multiple brains).
1. Read `#context` and `#resume`, the owning project's `#decision` and only the refs the next step requires ([budget](context.md#read-on-resume)).
1. If an action is already `stable` or `deprecated`, report its `Outcome` and start a new action linking it rather than reopening it silently.

## Finish or hand off {#finish-or-hand-off}

1. Keep `Context` and `Resume` current as facts change; before handing off or compacting, run [handoff.md](handoff.md) and leave `status: draft`.
1. To settle a completed session:
   - Write `## Outcome` comparing expectations against observed `SOURCE:ID` or check evidence.
   - Resolve every `- [ ]` checkbox in `ACTION.md`: tick completed items, move remaining tasks to the owning `projects/*.md` note's `## Next actions`, or drop them with a stated reason.
   - Sync durable changes (`Now`, `Decision`, `Next actions`) into the owning `projects/*.md` note in the same turn (`bf-use`).
   - Mark the session settled with `bf action status SLUG stable` (or `bf action status SLUG deprecated` when cancelled/retired), removing it from the active Home and `bf read review` queues.
1. Run `bf validate` and report the action ref, synced project ref and diff; commit only when authorized.

## Metadata and attachments

`ACTION.md` uses `type: action` and `status: draft|stable|deprecated`; add `verified` (`by`, `at`) only after real verification. Never declare an `entity` or `alias` pointing to `bf://NAME/actions/SLUG`: link to the action instead. Files under `inputs/` and `outputs/` are ordinary Markdown (`entity`, `aliases`, `tags`, `sources` and `stale_after` are ignored there) and remain searchable, so keep private data out of shared brains.
