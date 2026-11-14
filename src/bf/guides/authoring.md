# Authoring — notes, actions, identities, and typed relations

Brain Framework keeps authored knowledge in Markdown notes with [OKF](https://github.com/GoogleCloudPlatform/open-knowledge-format) YAML frontmatter. Never edit `memories/` files manually; sensors own collected records.

## Note directories and lifecycle

- `projects/<slug>.md` or `projects/<topic>/index.md`: active work streams, decisions, current status (`Now`), and next steps (`Next actions`).
- `concepts/<slug>.md`: durable ideas, architecture, entities, and reusable reference notes.
- `actions/YYYY-MM-DD_topic/ACTION.md`: isolated multi-step work sessions created and transitioned with `bf action`.
- `concepts/index.md`: brain home page rendered by `bf read` with no reference.

Every authored note requires OKF frontmatter with `type` and `status` (`draft`, `stable`, or `deprecated`), enforced by `bf validate`:

```yaml
---
type: project
status: stable
title: Billing migration
reviewed: 2026-04-01
relations:
  maintains: github:example/billing
attributes:
  team: platform
---
```

Keep work progress and dated notes in the Markdown body rather than appending unbounded frontmatter keys. When saving an outcome, update the owning project's `Now`, `Decision`, and `Next actions` sections and cite exact source refs (`SOURCE:ID` or relative Markdown paths).

## Manage actions with `bf action`

Create and transition tracked work sessions through the CLI instead of hand-creating action folders:

```bash
bf action new billing-cutover
bf action status 2026-04-01_billing-cutover stable
```

An existing `ACTION.md` is never silently overwritten. Open (`draft`) actions appear in `bf read review` and `bf read tasks`, including any `blocked_by` relationships. Set status to `stable` when completed or `deprecated` when abandoned.

## Identities, `relations`, and `attributes`

BF builds its knowledge graph strictly from explicit evidence — never from prose heuristics or fuzzy name matching.

- **Explicit identities (`scheme:value` or `bf://`)**: declare canonical identifiers on notes or records (for example `email:alice@example.test`, `github:org/repo#42`, `bf://projects/billing.md`).
- **Typed `relations`**: map relation names (`kebab-case` or `camelCase` schema.org property names, 1–64 chars, excluding reserved `tagged-with`, `links`, `cites`) to one explicit identity string or a non-empty list of explicit identities.
- **Metadata `attributes`**: attach arbitrary JSON scalars/objects (`attributes:` in notes or collected records) without polluting the graph. Attributes are searchable/stored metadata and are never scanned as graph edges.
- **Inline `?rel=` links**: link notes inline in Markdown using `[label](../concepts/topic.md?rel=depends-on)`. `?rel=` is the only supported query parameter on BF links.

Any valid relation name works in notes, records, or `?rel=` links with zero `bf.yaml` configuration. Add entries under `bf.yaml` `fields:` only when you want optional documentation (`description`, `broader`) or target constraints (`targets`) checked by `bf validate`.

## Verify edits

Always validate and re-read after authoring:

```bash
bf validate
bf read projects/billing.md#decision
```
