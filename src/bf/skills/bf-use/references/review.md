# Review knowledge periodically

Use for a requested weekly, monthly or project review. Take the brain, projects and period from the user's request and reuse established scope and authorization. A review needs no action folder, new skill or schedule.

1. Read `bf read projects` for projects needing review, `bf read concepts` and `bf read actions` for notes past their `stale_after`, `bf read tasks` for open work and a period such as `bf read 2026-09` or `bf read 2026-09-21..2026-09-27` for activity, following `next_offset` when the review needs every page. Prioritize the selected projects, unresolved blockers and decisions whose review date or event has arrived. State what was covered and what remains unreviewed.
1. Read each selected project and the refs needed to assess it. A `review` flag is a reason to inspect, not proof that a note is wrong: check `review_reasons`, `review_due` and `review_source`, and read the refs `newer` lists, the linked evidence that happened or changed upstream after the note's last edit (`newer_evidence`). A project falls due 14 days after its last edit (`modified`) unless its frontmatter sets `stale_after`, an ISO 8601 date-time with its offset; any other note is reminded only by `stale_after`. Copies, restores and checkouts reset file times, so use `stale_after` for commitments that must survive them. Never report an edit or an elapsed deadline as verification. Inspect `problems`, `stale` and source `freshness`: missing or incomplete evidence leaves a conclusion unknown, and retrieval does not authorize collection.
1. Compare each consequential decision's recorded expectation with the observed outcome: met, missed or unknown. Keep the original prediction and its evidence; never rewrite hindsight as foresight. Use the [evidence guide](evidence.md) when a source changed or a belief needs revision.
1. Revisit material unknowns and conditional intentions in the owning project, following the [decision conventions](decisions.md). Resolve a question only with evidence, and retire it when the decision it blocked no longer matters. An intention becoming ready proposes a next step; it never authorizes external work.
1. When note updates are authorized, revise the current state, verified status and next action in the owning notes, preserving material contradictions and decision evidence; change `updated` only when the note's meaning changes, and set a new `stale_after` when the next review has a real deadline. For a review-only request, present the proposed edits instead. Use [consolidation](consolidate.md) only when outcomes support a reusable lesson.
1. Run `bf validate`. Add or update an `evals/` case when an answer must stay findable, then run `bf eval`. Report changed conclusions, remaining unknowns and the next useful action with their refs, plus coverage limits. Keep conclusions in their owning notes rather than accumulating review summaries.

For example, a website review may establish that the draft was delivered while its effect remains unknown:

```markdown
## Now {#now}

The [product-page draft](../actions/2026-09-27_product-page/outputs/product-page.md) is ready for review. The expectation that visitors can explain the product remains untested: no usability-session notes are available.

## Next actions {#next-actions}

- [ ] Review the draft against the [decision](#decision).
- [ ] Agree how to test whether visitors understand the product.
```

Write this only after checking that the draft exists and that the stated evidence is unavailable. An untested outcome is not a failed prediction.
