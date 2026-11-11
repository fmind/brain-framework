---
icon: lucide/file-search
description: Reply fields and rules for search, read, pages, relation pages, continuations and MCP clients.
---

# Retrieval reference

Exact reply contracts shared by the CLI and MCP (`bf schema --kind search-reply|read-reply`). When multiple brains are selected, list entries include `brain` and portable `uri`, and cross-item refs (`newer`, `also`, `sections`, `outline`, `target_ref`) use `bf://` addresses.

## Pages

Computed views across selected brains (`bf read bf://brain/projects` restricts to one brain):

| Ref for `bf read`                                                        | Returns                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| _(omitted)_                                                              | Home: `projects`, up to 10 open (`draft`) `actions`, `changed` notes, 24h `activity`, 7d `upcoming`, `attention` and `pages`.                                                                                                                                                                                             |
| `review`                                                                 | Review page (`page: "review"`): paginated non-deprecated notes needing review (`due`, `newer_evidence`, `future_modified` or `unknown_modified`), up to 20 open `draft` `actions` (`actions_total` counts all open drafts; `blocked_by` lists up to 10 unresolved `draft` `depends-on` targets), and uniform `attention`. |
| `projects`, `concepts`, `actions` or subfolder                           | Folder notes with task counts (`ACTION.md` per action folder).                                                                                                                                                                                                                                                            |
| `tasks`                                                                  | Open checkboxes across non-deprecated OKF notes (50/page) plus `summary` totals.                                                                                                                                                                                                                                          |
| `12h`, `7d`, `2w`, `today`, `yesterday`, `YYYY-MM-DD`, `YYYY-MM`, `A..B` | Period items, upstream `changed` records (up to 20) and `sources` counts (`previous`/`next` on calendar periods).                                                                                                                                                                                                         |
| `memories`, `memories/SOURCE`, `memories/SOURCE/PERIOD\|undated\|FILE`   | Source overview, per-source records/coverage, undated records or SHA-256 file -> `SOURCE:ID` lookup.                                                                                                                                                                                                                      |
| `tags`, `tags/LABEL`                                                     | Tag directory and members (200/page; [tag rules](link-reference.md#tag-rules)).                                                                                                                                                                                                                                           |
| `actions/YYYY-MM-DD_SLUG`                                                | The action's `ACTION.md`, up to 200 folder `files`, 20 linked `projects` and `backlinks`.                                                                                                                                                                                                                                 |

### Ordering and review signals

- **Home**: `projects` orders notes needing review first (each group newest `updated` first, fitting within 32 KiB alongside `projects_total` of non-deprecated projects); `actions` lists up to 10 newest **`status: draft`** actions (`stable` and `deprecated` actions clear from Home while staying in `bf read actions` and search); `changed` and `upcoming` list up to 20 items; `attention` uses uniform `{kind, name, freshness, failed}` entries ([attention entries](commands.md#attention-entries)).
- **`priority: low` sources**: omitted from period `items`/`changed` and Home `upcoming`, while counted in `sources`/`activity` with `"priority": "low"`.
- **`deprecated` notes**: excluded from Home, `review`, reminders and `tasks`, and ranked last in listings and search.

Review fields on projects and notes with `stale_after`:

| Field          | Meaning                                                                                                                     |
| -------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `review: true` | Needs review; `review_reasons` holds `due`, `newer_evidence`, `future_modified` and/or `unknown_modified`.                  |
| `review_due`   | Local due date from `stale_after` (`review_source: "stale_after"`) or 14 days after project file `modified` (`"modified"`). |
| `new_links`    | Count of linked items whose `time` or `attributes.updated` is newer than the note's `modified` (`newer` previews up to 5).  |

`attributes.observed` never triggers `newer_evidence`; editing the note after reviewing clears `newer_evidence`.

### Time meanings

- Calendar days (`today`, `2026-09-27`) use local midnight boundaries (DST-aware); `7d`/`12h`/`2w` trail from now.
- Periods place records by `time`, falling back to `attributes.observed` only when `time` is absent (`memories/SOURCE/undated` still lists all timeless records).

### Time values

Note `updated` days appear as `YYYY-MM-DD` (`date`); instants use local second-precision offsets (`2026-09-29T09:00:00+02:00`, or `+00:00` for pre-standard-time historical offsets); record/listing `timestamp` fields use UTC (`Z`), while `attributes` and exact-read note frontmatter stay as stored.

## Tasks

Indexes unchecked `- [ ]` items in non-deprecated `projects/`, `concepts/` and `ACTION.md` notes (ignoring `index.md`, `log.md`, action attachments, code blocks and blockquotes), returning `note`, `title`, section `ref`, 1-based `line` and `text` (<= 320 chars, preserving `[label](ref)` links), 50 per page, with `total` and `summary` (`open`, `done`, `notes`).

## Search

Queries accept up to 4,096 characters and `--limit` 1–50 (default 10; invalid symbol-only queries such as `!!!` exit 2):

- **Matching**: first 32 distinct words, `"quoted phrases"` and `word*` prefixes; case-, Latin-accent- and compatibility-folded with English stemming; drops unquoted lowercase English/French function words (`AND`/`OR` always drop; uppercase acronyms such as `IT` stay); CJK matches unspaced runs between punctuation.
- **Ranking**: BM25 weighted toward titles, headings, filename stems, aliases and tags, +20% multi-term coverage bonus, OKF notes boosted, `deprecated` last, `priority: low` and `"generated": true` assistant records at half weight, ties newest first.
- **Deduplication & diagnostics**: one best section per note (up to 3 others in `sections`), one best source per shared `url` (up to 5 others in `also`), `unmatched` query tokens and up to 3 folded `suggestions` per unmatched word >= 4 letters.
- **Item fields**: `ref`, `kind`, `title` (<= 200 chars), `excerpt` (<= 320 chars); notes add `date`, `type`, `status`; records add `time`, `source`, `url`, `updated`, `observed`, optional `"partial": true` and `"generated": true`.

### Prefixes and other languages

`word*` prefixes of >= 3 letters expand up to 64 matching stems in alphabetical order (`organi*` matches `organization` and `évaluation`/`organisations` via accent folding and `-s` stemming).

### Scopes

`--scope` (shared by `bf search` and `bf export`) accepts one scope, optionally prefixed by a period and a space (`7d repo:github.com/example/new-website`, `2026-09 projects`):

- Authored folder or whole note (`projects`, `projects/new-website.md`) — never a `#section` or `bf://` page URI.
- Source (`memories/brief`, `memories/brief/7d`, `memories/brief/undated`, `memories/brief/FILE`).
- Period (`today`, `7d`, `2026-09`, `2026-09-21..2026-09-25`).
- Identity (`repo:github.com/example/new-website` — owner plus linking items; direct identity queries in `search` return owner first + linking items with `relations`, or fall back to word search with `"identity": "unknown"`).
- Tag URI (`bf://brain/tags/website` — full `bf://` URI required).

### Source coverage

Search replies include `sources` (sources of returned records plus any `failed`, `overdue`, `never`, `silent` or `degraded` sources) and `sources_omitted` whenever records were eligible; empty results and `memories` scopes list every searched source.

## Notes, records and identities

| Exact read | Returned fields                                                                                                                                                                                                |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Note       | `brain`, `ref`, `text`, `sha256`, `modified`, `backlinks`, optional `claims`, `tasks`/`next`/review signals, and `files`/`projects` on `ACTION.md`.                                                            |
| Section    | `brain`, `ref#section`, section `text`, whole-file `sha256` & `modified`, note `title`, `type`, optional `status` & `date`.                                                                                    |
| Record     | `brain`, `ref`, `path`, `record` (`id`, `title`, `text`, `time`, `url`, `links`, `aliases`, `attributes`, `fields`), `collection`, `sha256`, `modified`, `backlinks`, optional `claims` & `"generated": true`. |
| Identity   | Owning note/record, or backlinks page when unowned (`ambiguous identity` fails).                                                                                                                               |

Every reply carries `notice` (untrusted-evidence reminder) plus optional `problems` and `stale`. `backlinks` previews up to 64 relation groups (5 newest items each; `backlinks_omitted` counts extra groups); `claims` lists up to 20 per relation and 50 total (`claims_truncated`).

## Relation pages

`bf read REF --rel RELATION [--offset N]` (or MCP `read` with `rel`) lists incoming items for `links`, `cites` or a declared relation (including narrower relations declaring `broader: RELATION`), newest first, 50 per page (<= 32 KiB). `REF` must be a whole note, action folder, record or identity (pages and `#sections` exit 2).

## Continuations

Follow `next_offset` (`0`..`2^53-1`) with identical parameters until absent (Home and `memories` overview take no offset).

### Large exact reads

Exact reads exceeding 32 KiB paginate `text` (or `record.text`) by character offsets (`offset`, `next_offset`, `total_characters`, identical whole-file `sha256` on every page). A multi-section note's first page returns its first 4 KiB plus `outline` (up to 200 child sections with `ref`, `title`, `characters`).

## Incomplete answers and freshness

Always inspect `problems` (skipped files/brains), `stale` (writer held lock past 2s cache / 3s record wait) and `sources` coverage: an empty reply with `problems`, `stale` or unhealthy sources never proves absence.

### Cache freshness

Search and read automatically rescan authored folders and changed `memories/SOURCE` directories (including a 5s settle window). Run `bf build` after editing JSON files in `memories/` in place or on coarse/lagging network filesystems.

## Reply schemas

Validate replies against draft 2020-12 JSON Schemas ([`search`](../search-reply.schema.json), [`read`](../read-reply.schema.json), [`status`](../status-reply.schema.json)):

```bash
bf schema --kind search-reply > search-reply.schema.json
bf search "visitors clear explanation" > reply.json
uvx check-jsonschema --schemafile search-reply.schema.json reply.json
```

## MCP tool contract

Local **stdio** server (`bf mcp`; [setup](mcp.md)) publishing `outputSchema` on two read-only tools:

- **`search`**: required `query`; optional `scope=""`, `limit=10` (1–50), `offset=0`.
- **`read`**: optional `ref=""`, `rel=""`, `offset=0`.

Selection resolves at startup and on every call (`--brain`, `BF_BRAIN`, enclosing brain, else all registered brains). Errors escape control/format characters and redact brain/registry/home prefixes as `<brain>`, `<registry>` and `~`.
