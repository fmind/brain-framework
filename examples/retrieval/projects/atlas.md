---
type: project
status: draft
updated: 2026-09-12
tags: [atlas, retrieval]
aliases: [repo:example/atlas]
---

# Atlas

## Decision

Atlas uses SQLite for offline retrieval because queries must work without network access. The [review record](meetings:offline) records this decision.

## Budget

The Atlas launch budget is 4200 credits.

## Next actions

- [ ] Measure cold-cache retrieval latency.
