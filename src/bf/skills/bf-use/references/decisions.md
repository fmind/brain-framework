# Decisions, intentions and unknowns

Use the sections an action or project needs and omit empty ones. This is written reasoning inside one requested session or its owning project, not a task engine. Keep commitments and unresolved questions that outlive an action in the owning project.

## Decide and learn

Before a consequential choice, record the chosen option, the useful alternative, the assumption that distinguishes them, evidence refs, an observable expected outcome and a review date or event. Write the expectation before observing the outcome, and never invent a measurement or a numerical confidence.

For example, before testing a website draft:

```markdown
## Decision {#decision}

Lead with a product explanation, then offer signup. The alternative is a signup-first page. We expect new visitors to describe what the product does without opening the form; see the [project rationale](../../projects/new-website.md#decision).

Review after the next usability session. Outcome: unknown until the session notes are available.
```

At review, compare the expectation with an observed result and its evidence: met, missed or unknown, with the circumstances that changed. A shipped change is delivery, not proof of impact. Keep a failed prediction, correct the current conclusion and link the lesson; one outcome does not establish causation. Before reusing a similar decision, inspect both its earlier conditions and its result.

When an old rationale matters, keep a small dated decision note under the action's `outputs/` with supporting captures in `inputs/`. These files are ordinary Markdown: `type`, `status` and `updated` apply, but reminders (`stale_after`) work only in projects, concepts and `ACTION.md` notes, so keep the review deadline in the owning project. Correct a mistake explicitly or write a successor with a `supersedes` link; never silently rewrite an earlier prediction. See the [evidence guide](evidence.md) for captures and belief revision.

## Remember an intention

Put an intention under a stable project heading with an owner, a condition, the refs to inspect, the proposed next action, an expiry and a state: waiting, ready, unknown, resolved or expired. These are words in Markdown, not `status` values. For example: "When the documented release contains feature X, review the workaround; expire after the migration is complete."

Check it at the next requested review. Start from dates, an explicit status in a named source or a change between evidence captures. Never turn prose into executable conditions or treat an absence as proof of no reply: a condition needing absence requires known collection coverage of the interval, otherwise it stays unknown. Record the checked evidence revision and result; re-reading the same revision raises nothing new. A ready intention asks for review, never permission to send, deploy or run a provider. Mark it resolved or expired explicitly.

## Investigate an unknown

Record the question, the decision it blocks, what was inspected, what observation would resolve it and the smallest next investigation. For example: "Does the supported version export this format? Resolve with its current contract and one fixture; older documentation is insufficient."

Prioritize questions whose answer could change an active decision, and reuse local evidence first. Never collect, browse or contact someone merely because a note proposes it: the current request's authorization applies. Resolve a question only with evidence, keep material contradictions and move blocking questions into the action's Context.
