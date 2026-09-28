# Complete reads and graph context

Use when a reply is paginated or chunked, a graph claim matters, or a review needs source coverage.

## Continue a result

Keep the query or page ref, scope, limit and brain selection unchanged; set `--offset` to `next_offset`. Stop when no `next_offset` remains; there is no other continuation flag. Restart if evidence changes. A page or preview is not the complete result.

Exact replies longer than 65,536 characters of serialized JSON always return `format: json` and a `chunk` string, starting at offset 0. Follow the same ref and selection with each `next_offset`, concatenate chunks with identical `sha256`, verify that digest over the UTF-8 concatenation, then parse the JSON. Restart if the hash changes. Offsets count Unicode characters, not bytes. Prefer a note section when the task needs only that passage. See the [retrieval contract](https://fmind.github.io/brain-framework/docs/retrieval/#continuations).

Preserve returned refs literally and quote them in shell commands. A `#` inside a record ID is part of its identity; note section refs use the heading after the `.md` filename. With several selected brains, results alternate by each brain's own rank, and a plain ref present in two brains fails with exit 1: read the returned `uri` (`bf://NAME/...`) instead. MCP `read` takes only `ref` and `offset`; choose a brain with a `bf://NAME/` address.

## Check coverage and relationships

Inspect `problems`, `stale` and source coverage, including searched sources with zero matches. Each problem is an object with `error` and, when known, `brain` and `file`. Check `failed`, `freshness`, `window` and source `state`: `active` (configured and enabled), `disabled` (`enabled: false`) or `historical` (records kept for a sensor no longer in `bf.yaml`). `bf status` reports each brain's `cache` as `ready` or `stale`; `bf build` and `bf update` report a `skipped` count and exit 1 when the refresh skipped files, which `bf validate` names. A fresh cache does not establish fresh provider evidence; an incomplete empty result does not prove absence.

Search matches words case-, accent- and compatibility-insensitively and returns excerpts verbatim. Unspaced Chinese, Japanese or Thai text matches only as a whole run. A query without any word or identity, such as `!!!`, exits 2; a `memories/SOURCE` scope that no selected brain knows fails. Search and read refresh the brain's `.bf/` cache: a read-only brain fails with a write-access error, and when several brains are selected the affected one appears in `problems` with its `brain`, so grant write access or use a copy; remove a `.bf/` that another account owns. See [search rules](https://fmind.github.io/brain-framework/docs/retrieval/#search).

A whole note or record read includes `backlinks` grouped by explicit `relation`, with each claim's originating section or record under `relations[].origin`, and `claims` whose explicit subject is the read item. Check `claims_truncated` and `relations_truncated`. Backlink groups preview at most 20 items: when a group's `total` exceeds its `items`, continue with `bf search 'IDENTITY'` (following `next_offset`): each item's `relations` give the role, origin and target section. Read an item only when you need its supporting evidence. Section reads omit graph context: read the parent for dependency review. Never infer a relationship from similar names or prose.

`bf read IDENTITY` returns its owning note, or links to it when no note owns it. `bf://NAME/path#section` addresses a note, section or record; `bf read 'bf://NAME/'` returns that brain's home page, and addresses such as `bf://NAME/tasks`, `bf://NAME/7d` or `bf://NAME/tags/LABEL` open pages. Pages own that namespace: no entity or alias can claim it. Links never add another brain or contact a network. Ambiguous aliases fail visibly; `bf validate` reports foreign links under `unresolved` without opening them. Use `bf-learn` to author identities and typed relationships.

## Interpret attention signals

Project entries include `modified`, `review_due`, `review` and `review_reasons`. The default deadline is 14 days after local file modification; optional `review_after` days or an explicit `review_due` date customize it. Copies and checkouts can reset file times. A reminder, edit or new backlink is a reason to inspect, not evidence of verification.

`bf read tasks` lists open checkboxes in project/concept notes and canonical actions. Only `deprecated` closes a note: `draft` and `stable` notes keep their tasks, while deprecated notes, `index.md`, `log.md` and action attachments are excluded. Follow `next_offset`; counts cover the selection but remain qualified by `problems` and `stale`. Period pages distinguish items dated in the period (`items`, `total`) from items modified in it (`changed`). Future periods and home `upcoming` use agenda sources.
