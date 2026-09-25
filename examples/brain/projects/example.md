---
type: project
entity: bf://example/projects/example
status: active
updated: 2026-09-19
aliases: ["repo:example/project"]
---

# Example project

Demonstrate collecting fictional evidence and retrieving a grounded answer locally.

## Now {#now}

The source is a local deterministic script with no credentials or network access. See the [retention concept](bf://example/concepts/retention.md?rel=related-to).

## Decisions

- 2026-09-19: keep original evidence ([record](demo:retention)).

## Next actions

- [ ] Run the [retention action](../actions/2026-09-19_retention/ACTION.md).

## Intention {#intention}

Owner: the example maintainer. When the [archive policy](../concepts/archive-policy.md#retention) changes from the revision reviewed on 2026-09-25, review the [retention decision](../actions/2026-09-25_retention-review/outputs/decision.md). State: waiting. Check on the next requested project review; expire when this project closes. Re-reading the same revision does not create another reminder. A missing or incomplete source makes the condition unknown, not ready.

## Unknown {#unknown}

What storage cost would retaining selected revisions add? This blocks choosing a retention limit. Inspected: the fictional archive policy, which states no storage sizes. Resolve with a measured synthetic sample and the intended retention interval. Next investigation: measure ten representative synthetic records locally; no provider call is required. State: unresolved.
