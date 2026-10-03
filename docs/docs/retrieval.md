---
icon: lucide/file-search
description: Reply fields and rules for search, read, pages, relation pages, continuations and MCP clients.
---

# Retrieval reference

Exact reply fields and rules for CLI and MCP clients. For everyday use, start with [Search and read](search.md). Examples use the `brain` from [Getting started](getting-started.md).

Replies state a note's `updated` day as `date` (`2026-09-27`) and other instants with your local offset, to the second (`2026-09-29T09:00:00+02:00`). Values under `attributes` and `fields` are returned as stored. Collection stores `timestamp` fields in UTC (`2026-10-05T07:00:00.000000Z`), and listings show a note's timestamp fields the same way; its exact read keeps the note as written. With several selected brains, list entries also name their `brain` and portable `uri`, and `newer`, `also` and `sections` name items by `bf://` address.

## Pages

Pages are computed views, not files. They combine the selected brains; `bf read bf://brain/projects` selects one brain's page.

| Ref for `bf read`                                 | Result                                                                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| none                                              | Home: projects, recent actions and notes, activity, upcoming items and failing programs.                     |
| `projects`, `concepts`, `actions` or a subfolder  | Notes with their tasks; one `ACTION.md` per action.                                                          |
| `tasks`                                           | Open checkboxes and counts.                                                                                  |
| `today`, `yesterday`, `12h`, `7d`, `2w`           | Items dated in that period, items modified in it and source counts.                                          |
| `2026-09-27`, `2026-09`, `2026-09-21..2026-09-25` | The same for a local day, month or inclusive range of days; `previous` and `next` name the adjacent periods. |
| `memories`                                        | Sources with counts, freshness and collected windows.                                                        |
| `memories/SOURCE`, `memories/SOURCE/PERIOD`       | One source's coverage and records; `undated` selects records without a time.                                 |
| `memories/SOURCE/FILE`                            | Maps a SHA-256-named record file to its `SOURCE:ID` ref.                                                     |
| `tags`, `tags/LABEL`                              | Tag directory and members; see [tag rules](link-reference.md#tag-rules).                                     |
| `actions/YYYY-MM-DD_topic`                        | The action's `ACTION.md`, up to 200 files, 20 linked projects and backlinks.                                 |

Listed items show declared single-value `fields`, such as a status, when the note or record sets them. A folder ref may end with `/`, as shell completion adds: `tags/website/` reads like `tags/website`.

### Ordering and review signals

| View               | Order or preview                                                                                                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Projects           | Deprecated notes last; task counts and the first open task as `next`.                                                                                                                                                    |
| Concepts / actions | Title order / newest first.                                                                                                                                                                                              |
| Home `projects`    | Those needing review first, each group newest first, as many as fit the 32 KiB page beside the other sections; `projects_total` counts every project that is not deprecated, and `bf read projects` lists them all.      |
| Home               | 10 latest actions; notes dated in the last 7 days (`changed`); record counts of the last 24 hours (`activity`); items in the next 7 days (`upcoming`). `changed` and `upcoming` hold at most 20 items, without excerpts. |
| Period `changed`   | Up to 20 items modified in the period but dated outside it; not paginated.                                                                                                                                               |
| Home `attention`   | Scheduled programs that failed or are `overdue` or `never` succeeded on this machine.                                                                                                                                    |

Records of a [`priority: low`](sensors.md#quiet-a-high-volume-source) source leave period `items`, `changed` and home `upcoming`; period `sources` and home `activity` keep their counts with `"priority":"low"`. Only `deprecated` closes a note: it ranks last and leaves every home section, reminders and the task list.

Folder listings, every home section and the whole read of a note that gets reminders carry review signals:

| Field           | Rule                                                                                                                                                                                                                                                                          |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `review_due`    | The local day a review falls due: the note's `stale_after`, or 14 days after a project's last edit.                                                                                                                                                                           |
| `review_source` | `stale_after` or `modified`.                                                                                                                                                                                                                                                  |
| `modified`      | The file's modification time.                                                                                                                                                                                                                                                 |
| `review: true`  | Attention needed; `review_reasons` says why.                                                                                                                                                                                                                                  |
| `new_links`     | Linked items that happened, or changed upstream (`attributes.updated`), between the note's last edit and now: items linking to the note and records it links to. As in backlinks, a link through a name several items claim counts for none, unless it is a record's own ref. |
| `newer`         | Up to 5 of those items, newest first, to read before relying on the note.                                                                                                                                                                                                     |

| `review_reasons` value | Meaning                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------- |
| `due`                  | The deadline has passed.                                                           |
| `newer_evidence`       | Linked evidence happened or changed upstream after the note's last edit (`newer`). |
| `future_modified`      | The file's time is ahead of the clock; review now.                                 |
| `unknown_modified`     | The modification time cannot be represented.                                       |

A future event counts once it happens, and editing the note clears `newer_evidence` until newer evidence arrives. `observed` never counts: an upstream change made before the edit but collected after it raises no signal. Only projects, and notes that set `stale_after`, get reminders; `index.md`, `log.md` and action attachments never do. Reads never change files.

### Time meanings

| Input or value        | Meaning                                                                               |
| --------------------- | ------------------------------------------------------------------------------------- |
| `today`, `2026-09-27` | A local calendar day, including daylight-saving changes.                              |
| `7d`, `12h`, `2w`     | A trailing window ending now.                                                         |
| Record `time`         | Event time.                                                                           |
| Note `date`           | Its `updated` day, placed at local midnight.                                          |
| Period `changed`      | Upstream modification time (`attributes.updated`), when present.                      |
| Review signals        | A linked item's event time or upstream `updated`, against the note file's `modified`. |

A message sent last week and edited today keeps last week's event time and appears in today's `changed` list.

## Tasks

| Rule         | Contract                                                                                                                                                                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Included     | Unchecked list items in `projects/`, `concepts/` and canonical `ACTION.md` notes, whatever the note type.                                                                                                                                            |
| Excluded     | Deprecated notes, `index.md` and `log.md`, other files in an action's folder, code, blockquotes and checkbox-like prose.                                                                                                                             |
| Item fields  | Owning `note` and `title`, section `ref`, one-based `line` and `text` (up to 320 characters). Task links keep their targets as `[label](ref)` with brain-relative refs. Refs and code spans keep every character; other words lose emphasis markers. |
| Counts       | `total` and `summary.open`: unchecked; `summary.done`: checked; `summary.notes`: notes with any checkbox.                                                                                                                                            |
| Order, pages | Note path, line and brain; 50 items per page. Counts cover the whole selection.                                                                                                                                                                      |

Tasks can share a section ref; their `line` tells them apart within one snapshot.

## Search

Queries hold up to 4,096 characters. `--limit` is 1–50 (default 10).

| Rule           | Behavior                                                                                                                                                                                                                                                                                                                                                                                                 |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Terms          | The first 32 distinct words, `"quoted phrases"` and `word*` prefixes. Case, Latin-accent and compatibility insensitive (`ß`, `ﬁ`, full-width letters); English stemming. English and French function words, and words stemming to one such as `having`, drop unless they are the whole query, quoted, such as `"son"`, or written as an acronym, such as `EU AI Act` or `IT`; `AND` and `OR` still drop. |
| Other scripts  | Greek `ή` and Cyrillic `ё` stay distinct from `η` and `е`. Unspaced Chinese and Japanese match only as a whole run between punctuation. Scripts written with combining marks, such as Devanagari, Thai or vocalized Arabic, split at those marks and match by whole fragments, so a query can miss a word or match unrelated text.                                                                       |
| Ranking        | BM25 word frequency. Note titles, headings and tags weigh more than body text; a section ranks under its parent headings. A passage matching all of several terms gains 20%. Project, concept and `ACTION.md` notes rank higher, deprecated notes last and `priority: low` records at half weight. Equal scores list the newest first.                                                                   |
| Deduplication  | A note appears once, through its best section; `sections` lists up to three others. Records of several sources sharing a URL appear once, from the best-ranked source; `also` lists up to five other refs.                                                                                                                                                                                               |
| `unmatched`    | Query words and phrases found nowhere in the selected brains, whatever the scope.                                                                                                                                                                                                                                                                                                                        |
| Several brains | Results alternate by each brain's own rank; positions are not comparable across brains.                                                                                                                                                                                                                                                                                                                  |
| Invalid input  | A query without any word or identity, such as `!!!`, exits 2.                                                                                                                                                                                                                                                                                                                                            |

Items carry `ref`, `kind`, `title`, `excerpt`, a note's `date`, `type` and `status`, and a record's `time`, `source`, `url`, provider `updated` time, BF's `observed` time and `"partial":true` when the sensor kept only part of the text. Titles are previews of at most 200 characters ending in `…`; exact reads keep the whole title. A section's title reads `Note — Parent — Child`.

### Scopes

A search takes one scope: a folder or a whole note, a period, `memories/SOURCE` with an optional period or `undated`, a record file, an exact identity or a tag address. Other segments below `memories/SOURCE` are invalid (exit 2), and so is a section, such as `projects/new-website.md#decision`, or a page's `#fragment`: scope the whole note, then read the section. A source that no selected brain configures or indexes fails like its page; a folder or identity that names nothing returns no items.

### Identity matching

| Query                                          | Result                                                                       |
| ---------------------------------------------- | ---------------------------------------------------------------------------- |
| A known identity                               | Its owner first, then linking items newest first, each with its `relations`. |
| An unknown identity-shaped query (`re:invent`) | A word search with `"identity":"unknown"`.                                   |
| A tag address                                  | Exact membership, even when empty.                                           |

Identities are case-sensitive and resolve only through explicit aliases, entities and `resource` URIs.

### Source coverage

Search lists in `sources` the sources of returned records and those needing attention because they `failed` or are `overdue` or `never` collected. `sources_omitted` counts the others. A reply without items, or a search scoped to `memories` or `memories/SOURCE`, lists every searched source: coverage is then the answer. Authored-folder scopes omit both fields.

| Coverage field                      | Meaning                                                                                                             |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `state`                             | `active`, `disabled` or `historical` (records remain but `bf.yaml` no longer declares the sensor).                  |
| `freshness`                         | `fresh`, `overdue`, `never`, `manual` (no `refresh`) or `unknown` (disabled or historical).                         |
| `last_collected`, `window`          | This machine's last collection that brought the source up to date, and the contiguous interval collected.           |
| `failed`                            | The last attempt failed.                                                                                            |
| `mode`, `records`, `latest`, `page` | `window` or `snapshot`; on `memories` pages, also the indexed records, the newest event time and the source's page. |

## Notes, records and identities

| Exact read | Reply fields                                                                                                                                                                                                                                                             |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Note       | `brain`, `ref`, `text` (the whole file), `sha256`, `modified`, `backlinks`, `claims`; `uri` for a BF address; an `ACTION.md` adds `files` and linked `projects`; a note that gets reminders adds `tasks`, `next` and its [review signals](#ordering-and-review-signals). |
| Section    | `brain`, `ref` with its `#section`, that section's `text`, the whole file's `sha256` and `modified`, and the note's `title` and `type`, with its `status` and `date` when set.                                                                                           |
| Record     | `brain`, `ref`, `path`, `record` (`id`, `title` and any `text`, `time`, `url`, `links`, `aliases`, `attributes`, `fields`), `collection`, `sha256`, `modified`, `backlinks`, `claims`.                                                                                   |
| Identity   | The owning note or record; without an owner, a page of the backlinks naming it. An ambiguous alias fails: read an exact ref.                                                                                                                                             |
| Any        | `notice`, and `problems` or `stale` when evidence was skipped or the brain was busy.                                                                                                                                                                                     |

`sha256` covers the file's bytes, also on a section read: compare it before replacing a file you read. A record's text is `record.text`, never a top-level `text`. Record refs are `SOURCE:ID`: read a returned ref exactly as written, even when its id holds `#` or `?`, and percent-encode those as `%23` and `%3F` only inside a `bf://` address.

| Graph field | Contents and limits                                                                                                                                                                                                                                                                        |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `backlinks` | Groups of `relation`, `total` and `items`: `cites` and declared relations by name, then `links` for untyped links. Each group previews its 5 newest items with `ref`, `title`, `date` or `time`, `kind`, `excerpt` (up to 160 characters) and single-value `fields`.                       |
| `claims`    | Typed claims the subject makes (declared relations, `cites`, `tagged-with`), each with the `date` or `time` of its origin: up to 20 per relation and 50 in all; `claims_truncated` marks omissions. Untyped links stay in the text, in each target's `links` backlinks and in `bf export`. |
| `relations` | On identity search results: subject, relation, target and origin of each link; `relations_truncated` marks omissions. An untyped link repeating a typed one from the same origin to the same target is listed once, under its relation.                                                    |

Read a preview's ref for the evidence, or list the whole group with a relation page.

## Relation pages

A relation page lists every item linking to a note, record or identity through one relation, newest first:

```bash
bf read projects/new-website.md --rel cites
bf read repo:github.com/example/new-website --rel depends-on --offset 50
```

After [Connect two notes](links.md#connect-two-notes), the first returns `"page":"relation"`, `"relation":"cites"` and the concept as an item with its `excerpt`.

| Rule       | Behavior                                                                                                                                                       |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REF`      | A whole note, a `SOURCE:ID` record, an identity or a `bf://` address; an action folder reads its `ACTION.md`. A page or `#section` is invalid input (exit 2).  |
| `RELATION` | `links` for untyped links, `cites`, or a relation a selected brain declares. Another value exits 2 and names the valid ones.                                   |
| Narrower   | The page adds the links of relations declaring this one [`broader`](schema.md#narrower-relations-and-allowed-targets); their items carry their own `relation`. |
| Items      | Like search results, with an `excerpt`, from every selected brain. `total` counts the backlink group's claims plus those of its narrower relations.            |
| Pages      | 50 items, fewer above 32 KiB; follow `next_offset`. MCP `read` takes the relation as `rel`.                                                                    |

## Continuations

| Rule                  | Action                                                                                                   |
| --------------------- | -------------------------------------------------------------------------------------------------------- |
| `next_offset` present | Repeat the same request with that offset until it is absent; listings also report `total`.               |
| Same request          | Keep the query or ref, scope, limit and brain selection unchanged.                                       |
| Changed files         | Restart after edits, or when a moving period such as `7d` reorders items.                                |
| Offsets               | Zero-based, combined across brains, at most 2^53−1; a larger offset is invalid input (exit 2).           |
| Page sizes            | Folders and tags: 200; `memories/SOURCE`: 20; tasks, periods, relation pages and other source pages: 50. |
| Large items           | A page ends early when its items would exceed 32 KiB; `next_offset` continues after the last one.        |

The last page still needs the [completeness checks](#incomplete-answers-and-freshness).

### Large exact reads

An exact read whose reply would exceed 32 KiB returns its text in pages. A note's first page carries its `outline` and, for a whole note, its backlinks and claims. The outline lists the headings inside the read: never the H1 title spanning the whole note, nor the section being read. When such headings divide the text, the first page holds only its first 4 KiB; otherwise, as for a note with only a title or a section without subsections, it holds as much as fits. A record's first page carries every other field. Later pages carry `brain`, `ref`, any `uri`, their slice, the paging fields, `sha256`, `modified`, `notice` and any `problems` or `stale`.

| Field                                       | Meaning                                                                                     |
| ------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `text`, or `record.text`                    | This page's slice; a page ends at a line break when one falls in its second half.           |
| `offset`, `next_offset`, `total_characters` | Positions and length in Unicode characters of the text.                                     |
| `sha256`, `modified`                        | The whole file's digest and modification time, identical on every page.                     |
| `outline`                                   | Each section with its `ref`, `title` and `characters`; up to 200, then `outline_truncated`. |

1. Read a section from the `outline` when it answers the question.
1. Otherwise repeat the read with each `next_offset` and concatenate the slices in order.
1. Check that every page names the same `sha256`; restart at offset 0 if it changes.

A reply that fits returns whole and rejects a non-zero offset.

## Incomplete answers and freshness

| Signal                                 | Meaning and action                                                                                                                                                                |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `problems`                             | Objects with `error` and, when known, `brain` and `file`: skipped files (200 per brain, then a count; each error cut to 1 KiB with `…`), identities or brains. Repair and repeat. |
| `stale`                                | Brains answered from a cache or record that a writer was changing. Retry after it finishes.                                                                                       |
| Source `failed`, `freshness`, `window` | Collection health and local coverage; inspect `bf status` and the source page.                                                                                                    |
| Damaged cache                          | BF discards and rebuilds it once; run `bf build` if the problem persists.                                                                                                         |
| Cache write access                     | Search and read refresh `.bf/`; a read-only brain fails naming it. Grant write access or use a copy.                                                                              |

Malformed files, links and special files are skipped and reported while healthy brains still answer. While a selected brain is unavailable, an exact read returns what the others hold with a `problems` entry naming it, and a ref they do not hold fails as incomplete instead of not found, since it could exist there. Read a brain-qualified address, such as a search result's `uri` (`bf://NAME/...`), or repair that brain, for example with `bf build`. [What BF does not do](concepts.md#what-bf-does-not-do) states what an incomplete reply cannot prove.

## Reply schemas

`bf search` and `bf read` replies follow published JSON Schemas (draft 2020-12), shared by the CLI and MCP: [search](../search-reply.schema.json) and [read](../read-reply.schema.json). Validate a saved reply against the installed version's schema:

```bash
bf schema --kind search-reply > search-reply.schema.json
bf search "visitors clear explanation" > reply.json
uvx check-jsonschema --schemafile search-reply.schema.json reply.json
```

The validator prints `ok -- validation done`. The schemas describe every reply shape and accept added fields: a minor release may add optional fields, and only a major release changes or removes one. Clients should ignore fields they do not know.

## MCP tool contract

Transport: **stdio**. [Set up your host](mcp.md).

| Tool     | Arguments and defaults                                      | Returns                                                         |
| -------- | ----------------------------------------------------------- | --------------------------------------------------------------- |
| `search` | Required `query`; `scope=""`, `limit=10` (1–50), `offset=0` | Ranked matches with exact refs.                                 |
| `read`   | `ref=""`, `rel=""`, `offset=0`                              | Home, a page, note, section, record, identity or relation page. |

Each tool publishes its [reply schema](#reply-schemas) as `outputSchema`. Text and structured results carry the same JSON value as the CLI. Integral numbers such as `5.0` count as integers. Arguments outside the tool's input schema, such as `limit: 0`, return an error result with the MCP SDK's message. Values the tool rejects after that return `invalid input: ARGUMENT: reason`, naming `query`, `scope`, `ref` or `rel`, such as `invalid input: ref: invalid period: 2026-13`; an unknown scope or relation names the valid choices. Other errors carry the CLI's message, naming the `search` and `read` tools where the CLI names commands. Error text escapes control and format characters like CLI diagnostics, and shows a path that starts with a selected brain root, the registry file or the home directory as `<brain>`, `<registry>` or `~`.

| Concern        | Contract                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Selection      | Checked at startup, then resolved again for each call: `--brain`, `BF_BRAIN`, the enclosing brain of the server's working directory, then every registered brain. Registry changes and newly available brains need no restart, and direct `brains:` references are reread too; a change during a `next_offset` walk invalidates its offsets. Pass an absolute `--brain` for a stable selection, and restart to change it or the BF version. |
| Disambiguation | A `bf://NAME/...` ref chooses one selected brain; it grants no access.                                                                                                                                                                                                                                                                                                                                                                      |
| Continuation   | Follow `next_offset` and assemble [text pages](#large-exact-reads).                                                                                                                                                                                                                                                                                                                                                                         |
| Completeness   | Inspect `problems`, `stale` and source coverage.                                                                                                                                                                                                                                                                                                                                                                                            |
| Execution      | Neither tool collects, runs programs or follows links onto the network.                                                                                                                                                                                                                                                                                                                                                                     |
| Guidance       | Tool titles and server instructions describe the search, read and verify loop; they grant no authority.                                                                                                                                                                                                                                                                                                                                     |
