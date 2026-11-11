# Complete reads and graph context

Use when a reply is paged, a graph claim matters or an answer must be complete; the [retrieval reference](https://fmind.github.io/brain-framework/docs/retrieval/) owns exact fields and limits.

## Continue a result

Pass each `next_offset` to `--offset` with the query/ref, scope, limit and brain selection unchanged until `next_offset` is absent; restart if files or selected brains change. Exact reads above 32 KiB page their text: the first page carries `backlinks`, `claims` and any section `outline`. Read a listed `#section` when it suffices, or assemble all pages with `python3 SKILL_DIR/scripts/evidence.py read 'REF' --brain PATH` (verifies matching `sha256` across pages; [capture](evidence.md#retain-a-revision) huge outputs to a file).

## Refs and search rules

- Quote refs in shell commands. Record refs keep `#` or `?` literally (`bf read 'mail:thread#2'`); percent-encode them (`%23`, `%3F`) only inside `bf://` URIs. `--scope` takes a whole note, folder, period, identity or combined period scope (`--scope "7d repo:github.com/owner/name"`), never a `#section`.
- Only the first 32 distinct words count. English/French function words drop unless quoted (`"son"`) or ALL-CAPS (`AI`, `EU`); `AND` and `OR` always drop. CJK matches whole unspaced runs; combining-mark scripts split at marks.
- `priority: low` sources and assistant `generated` records rank at half weight; `"identity":"unknown"` falls back to word search.

## Coverage and graph

- Check `problems`, `stale` and `sources` ([source coverage](https://fmind.github.io/brain-framework/docs/retrieval/#source-coverage)); retry `stale` caches. Search and read refresh `.bf/` and need write access to the brain (or a writable copy).
- Whole reads group `backlinks` by `relation` (`cites`, declared relations, `links`). `--rel RELATION` pages one relation (including `broader` children) on a note, action folder, record or identity. `claims` lists outgoing typed claims with `origin`; `bf search 'IDENTITY'` returns every linking item's `relations`.
- `bf export` streams graph edges as JSONL, `--kind identities` multi-name subjects, and `--kind items` every note and record (`fields`, multi-value `values`, stored `attributes`, `text`), filterable by `--scope` (such as `memories/SOURCE`) and `--observed-since 7d`.

## Key pages

- `bf read` (home): projects needing review first, up to 10 open `draft` `actions`, `changed`, `activity`, `upcoming` and uniform `attention` (`{kind, name, freshness, failed, ...}`).
- `bf read review`: paginated non-deprecated notes needing review (`due` or `newer_evidence`), open `draft` `actions` (with `blocked_by` naming unresolved `draft` `depends-on` actions) and `attention`.
- `bf read tasks` and `bf read actions`: open checkboxes across non-deprecated notes, and all actions newest first.
- Period pages (`7d`, `2026-09-27`): place records by event `time`, else `observed`; `memories/SOURCE/undated` lists timeless records.
