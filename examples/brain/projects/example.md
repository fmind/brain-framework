---
type: project
tags: [retention, evidence]
status: stable
updated: 2026-09-19
stale_after: 90d
summary: Demonstrate collecting fictional evidence and querying unified JSON records locally.
---

# Example project

Demonstrate collecting fictional evidence, compiling Markdown with `attr_list` metadata to JSON, and retrieving grounded answers locally.

## Now {#now status=active}

The source is a local deterministic script with no credentials or network access. See the [retention concept](../concepts/retention.md){rel=related-to}.

## Decision {#decision owner=maintainer}

- 2026-09-19: keep original evidence ([record](../memories/demo/8e220124401da6e1a0288a86b48eb316d254cd2a9b9399cfc9a07a2fbc0253e8.json){rel=cites}).

## Next actions

- [ ] Run the [retention action](../actions/2026-09-19_retention/ACTION.md). {: #task-retention priority=p1}

## Intention {#intention owner=maintainer state=waiting}

When the [archive policy](../concepts/archive-policy.md#retention){rel=depends-on} changes from the revision reviewed on 2026-09-25, review the [retention decision](../actions/2026-09-25_retention-review/outputs/decision.md). {: #policy-watch condition="archive-policy-changed"}

## Unknown {#unknown state=unresolved}

What storage cost would retaining selected revisions add? Next investigation: measure ten representative synthetic records locally; no provider call is required.
