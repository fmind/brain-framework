# Complete reads and graph context

Use when a reply is paged, a graph claim matters or an answer must be complete; the [retrieval reference](https://fmind.github.io/brain-framework/docs/retrieval/) owns the exact fields and limits.

## Continue a result

Keep the query or ref, scope, limit and brain selection unchanged and pass each `next_offset` to `--offset` until it is absent; restart when evidence or the selected brains change meanwhile. A page or preview is never the complete result.

An exact read above 32 KiB returns its text in pages; the first carries the backlinks, claims and, when headings divide the read, an `outline`: read a section it lists when that answers the question. To assemble the whole text, run `python3 "$SKILL_DIR/scripts/evidence.py" read REF --brain PATH`: it follows every page, checks that each names the same file `sha256` and keeps every page's `problems` and `stale`. It prints the whole reply at once, past the 32 KiB page budget: redirect it to an owner-only file outside the brain, or pipe it as the [evidence guide](evidence.md#retain-a-revision) does.

## Write refs exactly

- Quote refs in shell commands and read them as returned. A section ref uses the heading slug or explicit `{#id}`; pages have no sections.
- A record ref keeps a `#` or `?` of its id as written (`bf read 'mail:thread#2'`); only a `bf://` address percent-encodes them as `%23` and `%3F`.
- A `--scope` takes a whole note, never its `#section` or a result's section `uri`: search the note, then read the section.

## Search gotchas

- English and French function words are ignored unless quoted (`"son"`) or written as a capitalized acronym (`AI`, `EU`); `AND` and `OR` always drop. Only the first 32 distinct words count.
- Unspaced Chinese and Japanese match only as a whole run; Thai, Devanagari and vocalized Arabic match by fragments, so a query can miss a word.
- Records of a `priority: low` source, and every assistant's records, rank at half weight; on period and home pages they appear only as a count with the `page` that lists them.
- `"identity":"unknown"` means nothing in the cache names that identity.

## Check coverage

Inspect `problems`, `stale` and `sources` before interpreting a result, and retry shortly after a `stale` reply. Search lists the sources of returned records plus any that failed, are `overdue` or `never` collected, or fell `silent`; an empty reply or a `memories` scope lists every searched source. A backfill extends a source's `window` without making it fresh, and a fresh cache never proves fresh provider evidence ([source coverage](https://fmind.github.io/brain-framework/docs/retrieval/#source-coverage)). Search and read refresh the `.bf/` cache, so a read-only brain fails with a write-access error: grant access or use a copy.

## Follow the graph

- A whole note, record or identity read groups `backlinks` by `relation`: `links` for untyped links, `cites` for OKF sources. `--rel RELATION` takes a whole note, record or identity, never a page or section, and `--rel PARENT` includes relations declared `broader: PARENT`.
- `claims` lists the item's typed claims with their `target` and `origin`, the section or record making each; `claims_truncated` marks a capped list. `bf search 'IDENTITY'` gives every linking item's `relations` at once.
- Section reads omit graph context: read the parent note for a dependency review.
- For analysis outside BF, `bf export` streams one JSON line per edge and `bf export --kind identities` every item with several names; both exit 1 when evidence was skipped.

## Pages worth knowing

- Home shows as many projects as fit, those needing review first, and `attention` for failing or late programs and silent sources; `bf read projects` lists every project, and the [review guide](review.md) interprets their signals.
- `bf read tasks` lists open checkboxes in projects, concepts and `ACTION.md` notes; only `deprecated` closes a note's tasks.
- Period pages place records by event `time`, else by `observed`; `memories/SOURCE/undated` lists records without event time.
