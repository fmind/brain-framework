---
description: Reply fields and rules for search, read, pages, relation pages, continuations and MCP clients.
---

# Retrieval reference

Exact reply fields and rules for CLI and MCP clients. For everyday use, start with [Search and read](search.md). Examples use the `brain` from [Getting started](getting-started.md).

Replies state a note's `updated` day as `date` (`2026-09-27`) and every instant with your local offset, to the second (`2026-09-29T09:00:00+02:00`). With several selected brains, list entries also name their `brain` and portable `uri`.

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

Listed items show declared single-value `fields`, such as a status, when the note or record sets them.

### Ordering and review signals

| View               | Order or preview                                                                                                                                                                                                                                        |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Projects           | Deprecated notes last; task counts and the first open task as `next`.                                                                                                                                                                                   |
| Concepts / actions | Title order / newest first.                                                                                                                                                                                                                             |
| Home               | Projects needing review first; 10 latest actions; notes dated in the last 7 days (`changed`); record counts of the last 24 hours (`activity`); items in the next 7 days (`upcoming`). `changed` and `upcoming` hold at most 20 items, without excerpts. |
| Period `changed`   | Up to 20 items modified in the period but dated outside it; not paginated.                                                                                                                                                                              |
| Home `attention`   | Scheduled programs that failed or are `overdue` or `never` succeeded on this machine.                                                                                                                                                                   |

Records of a [`priority: low`](sensors.md#quiet-a-high-volume-source) source leave period `items`, `changed` and home `upcoming`; period `sources` and home `activity` keep their counts with `"priority":"low"`. Only `deprecated` closes a note: it ranks last and leaves home, reminders and the task list.

Folder listings and the home preview carry review signals:

| Field           | Rule                                                                                                |
| --------------- | --------------------------------------------------------------------------------------------------- |
| `review_due`    | The local day a review falls due: the note's `stale_after`, or 14 days after a project's last edit. |
| `review_source` | `stale_after` or `modified`.                                                                        |
| `modified`      | The file's modification time.                                                                       |
| `review: true`  | Attention needed; `review_reasons` says why.                                                        |
| `new_links`     | Items linking to the note whose event time is at or after its last edit.                            |

| `review_reasons` value | Meaning                                                |
| ---------------------- | ------------------------------------------------------ |
| `due`                  | The deadline has passed.                               |
| `newer_evidence`       | Evidence dated after the note's last edit links to it. |
| `future_modified`      | The file's time is ahead of the clock; review now.     |
| `unknown_modified`     | The modification time cannot be represented.           |

Only projects, and notes that set `stale_after`, get reminders; `index.md`, `log.md` and action attachments never do. Reads never change files.

### Time meanings

| Input or value        | Meaning                                                          |
| --------------------- | ---------------------------------------------------------------- |
| `today`, `2026-09-27` | A local calendar day, including daylight-saving changes.         |
| `7d`, `12h`, `2w`     | A trailing window ending now.                                    |
| Record `time`         | Event time.                                                      |
| Note `date`           | Its `updated` day, placed at local midnight.                     |
| Period `changed`      | Upstream modification time (`attributes.updated`), when present. |

A message sent last week and edited today keeps last week's event time and appears in today's `changed` list.

## Tasks

| Rule         | Contract                                                                                                                                                                |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Included     | Unchecked list items in `projects/`, `concepts/` and canonical `ACTION.md` notes, whatever the note type.                                                               |
| Excluded     | Deprecated notes, `index.md` and `log.md`, other files in an action's folder, code, blockquotes and checkbox-like prose.                                                |
| Item fields  | Owning `note` and `title`, section `ref`, one-based `line` and `text` (up to 320 characters). Task links keep their targets as `[label](ref)` with brain-relative refs. |
| Counts       | `total` and `summary.open`: unchecked; `summary.done`: checked; `summary.notes`: notes with any checkbox.                                                               |
| Order, pages | Note path, line and brain; 50 items per page. Counts cover the whole selection.                                                                                         |

Tasks can share a section ref; their `line` tells them apart within one snapshot.

## Search

Queries hold up to 4,096 characters. `--limit` is 1–50 (default 10).

| Rule           | Behavior                                                                                                                                                                                                                                                                                                                               |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Terms          | The first 32 distinct words, `"quoted phrases"` and `word*` prefixes. Case, accent and compatibility insensitive (`ß`, `ﬁ`, full-width letters); English stemming. English and French function words are dropped unless they are the whole query.                                                                                      |
| Ranking        | BM25 word frequency. Note titles, headings and tags weigh more than body text; a section ranks under its parent headings. A passage matching all of several terms gains 20%. Project, concept and `ACTION.md` notes rank higher, deprecated notes last and `priority: low` records at half weight. Equal scores list the newest first. |
| Deduplication  | A note appears once, through its best section; `sections` lists up to three others. Records of several sources sharing a URL appear once, from the best-ranked source; `also` lists up to five other refs.                                                                                                                             |
| `unmatched`    | Query words and phrases found nowhere in the selected brains, whatever the scope.                                                                                                                                                                                                                                                      |
| Several brains | Results alternate by each brain's own rank; positions are not comparable across brains.                                                                                                                                                                                                                                                |
| Invalid input  | A query without any word or identity, such as `!!!`, exits 2.                                                                                                                                                                                                                                                                          |

Items carry `ref`, `kind`, `title`, `excerpt`, a note's `date`, `type` and `status`, and a record's `time` and `source`. Titles are previews of at most 200 characters ending in `…`; exact reads keep the whole title. A section's title reads `Note — Parent — Child`.

### Scopes

A search takes one scope: a folder or file, a period, `memories/SOURCE` with an optional period or `undated`, a record file, an exact identity or a tag address. Other segments below `memories/SOURCE` are invalid (exit 2). A source that no selected brain configures or indexes fails like its page; a folder or identity that names nothing returns no items.

### Identity matching

| Query                                          | Result                                                                       |
| ---------------------------------------------- | ---------------------------------------------------------------------------- |
| A known identity                               | Its owner first, then linking items newest first, each with its `relations`. |
| An unknown identity-shaped query (`re:invent`) | A word search with `"identity":"unknown"`.                                   |
| A tag address                                  | Exact membership, even when empty.                                           |

Identities are case-sensitive and resolve only through explicit aliases, entities and `resource` URIs.

### Source coverage

Search lists in `sources` the sources of returned records and those needing attention because they `failed` or are `overdue` or `never` collected. `sources_omitted` counts the others. A reply without items, or a search scoped to `memories` or `memories/SOURCE`, lists every searched source: coverage is then the answer. Authored-folder scopes omit both fields.

| Coverage field             | Meaning                                                                                            |
| -------------------------- | -------------------------------------------------------------------------------------------------- |
| `state`                    | `active`, `disabled` or `historical` (records remain but `bf.yaml` no longer declares the sensor). |
| `freshness`                | `fresh`, `overdue`, `never`, `manual` (no `refresh`) or `unknown` (disabled or historical).        |
| `last_collected`, `window` | This machine's last collection and the contiguous interval it covered.                             |
| `failed`                   | The last attempt failed.                                                                           |

## Notes, records and identities

| Exact read | Reply fields                                                                                                                                                                 |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Note       | `brain`, `ref`, `text` (the whole file), `sha256`, `modified`, `backlinks`, `claims`; `uri` for a BF address; an `ACTION.md` adds `files` and linked `projects`.             |
| Section    | `brain`, `ref` with its `#section`, that section's `text`, and the whole file's `sha256` and `modified`.                                                                     |
| Record     | `brain`, `ref`, `path`, `record` (`id`, `title` and any `text`, `time`, `url`, `links`, `aliases`, `attributes`, `fields`), `collection`, `sha256`, `modified`, `backlinks`. |
| Identity   | The owning note or record; without an owner, a page of the backlinks naming it. An ambiguous alias fails: read an exact ref.                                                 |
| Any        | `notice`, and `problems` or `stale` when evidence was skipped or the brain was busy.                                                                                         |

`sha256` covers the file's bytes, also on a section read: compare it before replacing a file you read. A record's text is `record.text`, never a top-level `text`. Record refs are `SOURCE:ID`; encode a literal `#` in an id as `%23`.

| Graph field | Contents and limits                                                                                                                                                                                                                                                  |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backlinks` | Groups of `relation`, `total` and `items`: `cites` and declared relations by name, then `links` for untyped links. Each group previews its 5 newest items with `ref`, `title`, `date` or `time`, `kind`, `excerpt` (up to 160 characters) and single-value `fields`. |
| `claims`    | Claims the subject makes, each with the `date` or `time` of its origin: up to 20 per relation and 50 in all; `claims_truncated` marks omissions.                                                                                                                     |
| `relations` | On identity search results: subject, relation, target and origin of each link; `relations_truncated` marks omissions.                                                                                                                                                |

Read a preview's ref for the evidence, or list the whole group with a relation page.

## Relation pages

A relation page lists every item linking to a note, record or identity through one relation, newest first:

```bash
bf read projects/new-website.md --rel cites
bf read repo:github.com/team/new-website --rel depends-on --offset 50
```

After [Connect two notes](links.md#connect-two-notes), the first returns `"page":"relation"`, `"relation":"cites"`, `"total":1` and the concept as an item with its `excerpt`.

| Rule       | Behavior                                                                                                                                                       |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REF`      | A whole note, a `SOURCE:ID` record, an identity or a `bf://` address; an action folder reads its `ACTION.md`. A page or `#section` fails (exit 1).             |
| `RELATION` | `links` for untyped links, `cites`, or a relation a selected brain declares. Another value exits 2 and names the valid ones.                                   |
| Narrower   | The page adds the links of relations declaring this one [`broader`](schema.md#narrower-relations-and-allowed-targets); their items carry their own `relation`. |
| Items      | Like search results, with an `excerpt`, from every selected brain. `total` counts the same claims as the backlink group.                                       |
| Pages      | 50 items, fewer above 32 KiB; follow `next_offset`. MCP `read` takes the relation as `rel`.                                                                    |

## Continuations

| Rule                  | Action                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------- |
| `next_offset` present | Repeat the same request with that offset until it is absent; listings also report `total`.        |
| Same request          | Keep the query or ref, scope, limit and brain selection unchanged.                                |
| Changed files         | Restart after edits, or when a moving period such as `7d` reorders items.                         |
| Offsets               | Zero-based, combined across brains, at most 2^53−1.                                               |
| Page sizes            | Folders and tags: 200; tasks, periods and relation pages: 50; source pages: 20.                   |
| Large items           | A page ends early when its items would exceed 32 KiB; `next_offset` continues after the last one. |

The last page still needs the [completeness checks](#incomplete-answers-and-freshness).

### Large exact reads

An exact read whose reply would exceed 32 KiB returns its text in pages. A note's first page carries its `outline`, backlinks and claims and only the first 4 KiB of text; a record's first page carries every other field. Later pages carry `brain`, `ref`, their slice, the paging fields, `sha256`, `modified`, `notice` and any `problems` or `stale`.

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

| Signal                                 | Meaning and action                                                                                                                              |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `problems`                             | Objects with `error` and, when known, `brain` and `file`: skipped files (200 per brain, then a count), identities or brains. Repair and repeat. |
| `stale`                                | Brains answered from a cache or record that a writer was changing. Retry after it finishes.                                                     |
| Source `failed`, `freshness`, `window` | Collection health and local coverage; inspect `bf status` and the source page.                                                                  |
| Damaged cache                          | BF discards and rebuilds it once; run `bf build` if the problem persists.                                                                       |
| Cache write access                     | Search and read refresh `.bf/`; a read-only brain fails naming it. Grant write access or use a copy.                                            |

Malformed files, links and special files are skipped and reported while healthy brains still answer. [What BF does not do](concepts.md#what-bf-does-not-do) states what an incomplete reply cannot prove.

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

Each tool publishes its reply schema as `outputSchema`. Text and structured results carry the same JSON value as the CLI. Invalid arguments return an error result starting with `invalid input:`. Other errors name the `search` and `read` tools where the CLI names commands, and hide brain paths.

| Concern        | Contract                                                                                                               |
| -------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Selection      | Roots are fixed at startup; direct `brains:` references are reread per request. Restart to change roots or BF version. |
| Disambiguation | A `bf://NAME/...` ref chooses one selected brain; it grants no access.                                                 |
| Continuation   | Follow `next_offset` and assemble [text pages](#large-exact-reads).                                                    |
| Completeness   | Inspect `problems`, `stale` and source coverage.                                                                       |
| Execution      | Neither tool collects, runs programs or follows links onto the network.                                                |
| Guidance       | Tool titles and server instructions describe the search, read and verify loop; they grant no authority.                |
