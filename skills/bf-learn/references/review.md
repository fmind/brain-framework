# Review knowledge periodically

Use for a requested weekly, monthly or project review. Select the brain, projects and period from the user's request; reuse established scope and authorization. A review does not require an action folder, a new skill or a schedule.

1. Read `bf read projects --brain PATH` for projects needing review, `bf read tasks --brain PATH` for open work and counts, and a period such as `bf read 2026-09 --brain PATH` for relevant activity. Follow `next_offset` when the review needs further pages. Prioritize the selected projects, unresolved blockers and decisions whose review date or event has arrived. State what was covered and what remains unreviewed.
1. Read each selected project's current note and the supporting refs needed to assess it. A `review` flag is a reason to inspect, not proof that a note is wrong. Inspect `review_reasons`, automatic `modified` time and `review_due`. An explicit deadline wins over the age interval; absent one, a file edit moves the automatic deadline. Copies, restores and Git checkouts can reset modification times, so use explicit deadlines for portable commitments. Never report an edit or elapsed deadline as completed verification. Inspect `problems`, `stale` and source coverage; missing or incomplete evidence leaves the conclusion unknown. Retrieval does not authorize collection.
1. Compare consequential decisions' recorded expectations with observed outcomes: met, missed or unknown. Keep the original prediction and its evidence; do not rewrite hindsight as foresight. Use the [evidence guide](evidence.md) if a source changed or a belief needs revision.
1. Revisit material unknowns and conditional intentions in the owning project. Resolve a question only with evidence, and retire it when the decision it blocked no longer matters. An intention becoming ready proposes a next step; it does not authorize external work. Reuse the [decision conventions](../../bf-action/references/decisions.md) when that companion is installed.
1. When note updates are authorized, revise the current state, verified status and next action in the owning notes. Preserve material contradictions and durable decision evidence; change `updated` only when the note's meaning changes. For a review-only request, present the proposed edits instead. Use [consolidation](consolidate.md) only when outcomes support a reusable lesson.
1. Validate edited notes with `bf validate --brain PATH`. Add or update an `evals/` case when an answer must stay findable, then run `bf eval --brain PATH`. Report changed conclusions, remaining unknowns and the next useful action with their refs, plus any coverage limits. Keep durable conclusions in their owning notes rather than accumulating duplicate review summaries.

For example, a website review may establish that the draft was delivered while its effect remains unknown:

```markdown
## Now {#now}

The [product-page draft](../actions/2026-09-27_product-page-SUFFIX/outputs/product-page.md) is ready for review. The expectation that visitors can explain the product remains untested: no usability-session notes are available.

## Next actions {#next-actions}

- [ ] Review the draft against the [decision](#decision).
- [ ] Agree how to test whether visitors understand the product.
```

Use this wording only after checking that the draft exists and the stated evidence is unavailable. An untested outcome is not a failed prediction.
