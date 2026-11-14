# Authoring notes, actions, identities, and relations

Brain Framework keeps authored knowledge in Markdown notes with [OKF](https://github.com/GoogleCloudPlatform/open-knowledge-format) YAML frontmatter. Never edit `memories/` files manually; record jobs and `bf ingest` own collected evidence.

## Note directories and lifecycle

- `projects/<slug>.md` or `projects/<topic>/index.md`: active work streams, `Now`, `Decision`, and `Next actions`.
- `concepts/<slug>.md`: durable ideas, people/entity notes, and reusable patterns (`concepts/index.md` is the Home note).
- `actions/YYYY-MM-DD_topic/ACTION.md`: tracked multi-step work sessions created and transitioned via `bf action`.
- Special files: folder `index.md` and changelog `log.md` (`YYYY-MM-DD` headings) carry no review reminders or task checks; ordinary Markdown files (such as `actions/*/inputs/*.md`) index prose metadata while ignoring identity/relation keys so imported text never claims identities.

Every OKF note requires non-empty `type` and `status` (`draft`, `stable`, or `deprecated`), verified by `bf validate`:

```yaml
---
type: project
status: stable
title: Billing migration
updated: 2026-09-27
summary: Cut over billing pipeline without losing audit history.
relations:
  owner: [person:email/alice@example.test]
attributes:
  team: platform
---
```

Keep work logs and status updates in Markdown sections (`Now`, `Decision`, `Next actions`) rather than unbounded frontmatter fields, and use explicit heading anchors (`## Decision {#decision}`) so renames preserve incoming links.

## Manage actions with `bf action`

Create and transition tracked work sessions through the CLI instead of creating `ACTION.md` folders by hand:

```bash
bf action new billing-cutover --summary "Verify invoice dual-write parity"
bf action status 2026-09-27_billing-cutover stable
```

- Pass `--unique` to append a 12-hex suffix when running repeated tasks on one day, and `--run RUN_ID` from inside a job run so `.bf/runtime.sqlite3` records created actions.
- An existing `ACTION.md` is never replaced silently.
- Open `draft` actions appear on Home and `bf read review` (including `blocked_by` from unresolved `draft` `depends-on` links). Mark completed actions `stable` (which clears them from Home while keeping them searchable) or retired ones `deprecated`.
- Keep `Context` (brief state plus `<= 6` verified refs, never raw transcripts) and `Resume` up to date so any future session can continue cleanly.

## Connect notes and records

1. **Plain Markdown links and sections**: `[New website decision](../projects/new-website.md#decision)` records an untyped `links` edge from its originating section; `[Product brief](brief:website-brief)` cites a record.
2. **OKF `sources:`**: frontmatter `sources:` entries with non-empty `resource` generate `cites` edges.
3. **Typed inline `?rel=` links**: add `?rel=RELATION` to a `bf://` URI:

```markdown
[Launch decision](bf://brain/projects/new-website.md?rel=depends-on#decision)
```

`?rel=` is the only supported query parameter on BF links. Whole reads preview up to 5 newest backlinks per relation; `bf read REF --rel RELATION` paginates all incoming edges.

## Identities, `relations`, and `attributes`

BF builds its knowledge graph strictly from explicit evidence — never from prose heuristics or name matching:

- **`entity`, `aliases`, `resource`**: bind exact case-sensitive identifiers on an owning note (for example `concepts/me.md` with `entity: bf://brain/people/me`, `aliases: [person:email/me@example.test, person:github.com/me]`, `resource: https://example.test/me`). `entity` never names a `#section`, `.md` file, page, record, or `bf://brain/actions/SLUG` folder.
- **Typed `relations`**: map any valid relation name (`^[a-z][a-zA-Z0-9]*(?:-[a-z0-9]+)*$`, 1–64 chars, excluding reserved `tagged-with`, `links`, `cites`) to a `scheme:value` or `bf://` string or list. Common conventions (`author`, `owner`, `assignee`, `participant`, `sender`, `recipient`, `cc`, `organizer`, `attendee`, `repository`, `project`, `part-of`, `label`, `depends-on`, `related-to`, `supersedes`) need zero declaration.
- **`attributes`**: attach bounded JSON metadata without creating graph edges.
- **Optional `fields:` constraints in `bf.yaml`**: add `fields:` entries only when enforcing `description`, `broader` rollups (for example `organizer` -> `participant`), or `targets` scheme prefix checks (`bf validate` and `bf build --reproject`). See [Reference](reference.md#bf-yaml-schema).

## Host hooks and verification

For repository-aware session/prompt injection, review and install [`examples/hooks/`](https://github.com/fmind/brain-framework/tree/main/examples/hooks) (`session-context.py`, `prompt-context.py`) from a private host-owned folder — they run read-only `bf read` / `bf search` offline and never print raw record bodies.

Always verify edits before finishing:

```bash
bf validate
bf read projects/new-website.md#decision
```
