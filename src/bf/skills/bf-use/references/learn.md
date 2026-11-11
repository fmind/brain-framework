# Write knowledge, review and consolidate

Use after a decision, corrected assumption, completed session or review when authorized (propose the edit for review-only requests). Keep owning notes concise and current; never touch a note solely to clear a review flag, and never edit `memories/`.

## Update the owning note

1. Locate the owner (`bf search "topic words"`): `projects/` owns live state (`## Now {#now}`), decisions (`## Decision {#decision}`) and open checkboxes (`## Next actions {#next-actions}`); `concepts/` owns reusable rules and reference syntheses ([project](../templates/project.md), [concept](../templates/concept.md)). Read the note and record its `sha256`.
1. Edit focused passages, preserving unrelated sections and `{#id}` anchors. Update `Now`, `Decision` and `Next actions` citing `SOURCE:ID` or note refs; keep checkboxes only in their owning note so tasks never duplicate. Keep `status` as `draft`, `stable` or `deprecated` (only `deprecated` closes a note and its tasks), update `updated: YYYY-MM-DD` when meaning changes, and add `verified` (`by`, `at`) only after an actual check.
1. Run `bf validate`, search and `bf read` the updated ref, and run `bf eval` if you added an `evals/` case ([checks](https://fmind.github.io/brain-framework/docs/checks/)). Report changed refs, diff and remaining uncertainty.

## Guard a write {#guard-a-write}

When concurrent sessions could edit the same file without editor locks, pass the `sha256` from `bf read` to `guarded-write.py`:

```bash
python3 SKILL_DIR/scripts/guarded-write.py projects/new-website.md --expect-sha256 SHA256 \
  --old="- [ ] Draft the product page." --new="- [x] Draft the product page."
```

Use `--old=TEXT` / `--new=TEXT` (or `--old-file` / `--new-file` for multi-line edits; `--new-file` alone replaces the whole file). Refusals (`the file changed since it was read`, missing/duplicate old text, active lock) exit 1 without modifying the file: re-read and retry with the new `sha256`.

## Decisions, intentions and unknowns

- **Decisions**: record the chosen option, viable alternative, distinguishing assumption, `SOURCE:ID` refs, observable expected outcome and review trigger before observing outcomes:

  ```markdown
  ## Decision {#decision}

  Lead with a product explanation before signup (alternative: signup-first). Expect new visitors to state what the product does without opening the form; see [brief](brief:website-brief). Review after usability testing. Outcome: unknown.
  ```

  At review, compare the expectation with evidence (`met`, `missed` or `unknown`) without rewriting hindsight as foresight; link a `supersedes` note when replacing a consequential rationale ([evidence](evidence.md)).
- **Intentions**: record owner, condition, refs to inspect, proposed next action, expiry and state (`waiting`, `ready`, `unknown`, `resolved`, `expired`). A `ready` intention prompts review; it never authorizes external effects.
- **Unknowns**: record the blocking question, what was checked and the smallest next check; never browse, collect or contact anyone merely because a note lists an unknown.

## Turnkey consolidation ("Compile, don't RAG") {#consolidate}

Compile collected evidence and settled work into durable notes instead of leaving knowledge buried in raw records:

1. **Sweep**: run `bf read review` (following `next_offset`) to list non-deprecated notes with `due` or `newer_evidence`, open `draft` `actions` (with `blocked_by`) and `attention`; check `bf read actions` for recently settled (`status: stable`) sessions not yet folded into their project.
1. **Reconcile projects**: for each flagged note, read the `SOURCE:ID` refs in `newer` and settled `ACTION.md#outcome` sections. Update `Now`, compare decision expectations against outcomes, and refresh `Next actions` checkboxes while preserving material contradictions and provenance.
1. **Promote concepts**: when two or more outcomes establish a reusable rule or pattern (a single event stays provisional), extend or create a `concepts/*.md` note stating applicability, decision rule, failure modes and links to supporting actions/records. Keep `status: draft` until tested on an independent case.
1. **Verify**: never write to `memories/`. Run `bf validate` (and `bf eval`) and present an inspectable diff of updated projects, promoted concepts and unresolved unknowns.
