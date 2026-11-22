---
type: project
tags: [retention, evidence]
status: active
created: 2026-09-19
updated: 2026-09-19
stale_after: 90d
description: Demonstrate collecting fictional evidence and querying unified JSONL records locally.
---

# Example project

Demonstrate collecting fictional evidence, compiling Markdown with `attr_list` metadata to JSONL, and retrieving grounded answers locally.

## Now {#now status=active}

The source is a local deterministic script with no credentials or network access. See the [retention concept](../concepts/retention.md){rel=related-to}.

## Decision {#decision owner=maintainer}

- 2026-09-19: keep original evidence ([record](demo:retention){rel=cites}).

## Next actions

- [ ] Run the [retention action](../actions/retention/index.md). {: priority=p1}

## Intention {#intention owner=maintainer state=waiting}

When the [archive policy](../concepts/archive-policy.md#retention){rel=depends-on} changes from the revision reviewed on 2026-09-25, review the [retention decision](../actions/retention-review/outputs/decision.md).

## Unknown {#unknown state=unresolved}

What storage cost would retaining selected revisions add? Next investigation: measure ten representative synthetic records locally; no provider call is required.
