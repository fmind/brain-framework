---
description: Implement CLI and MCP clients with correct pagination, text pages, role pages and completeness checks.
---

# Retrieval reference

Reply fields and rules for CLI/MCP clients. For everyday use, start with [Search and read](search.md). Examples run inside the `brain` from [Getting started](getting-started.md); JSON snippets show selected fields.

## Pages

Pages are computed views, not files to edit. They combine selected brains; when several are selected, each entry names its `brain` and portable `uri`. Use `bf read bf://brain/projects` to select one brain's projects, or `bf read bf://brain/` for its home.

| Command or ref for `bf read`                                     | Result                                                                   |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------ |
| No ref                                                           | Project preview, recent work, upcoming items and collection alerts.      |
| `projects`, `concepts`, `actions` or a subfolder                 | Notes and tasks; one `ACTION.md` per action.                             |
| `tasks`                                                          | Open checkboxes and aggregate counts.                                    |
| `today`, `yesterday`, `2026-09-27`, `2026-09`, `12h`, `7d`, `2w` | Activity, modifications and source counts for that period.               |
| `memories`                                                       | Sources, counts, freshness and collected windows.                        |
| `memories/SOURCE`                                                | Source coverage and latest records.                                      |
| `memories/SOURCE/PERIOD`                                         | Source items in a local period; `undated` selects records without dates. |
| `memories/SOURCE/FILE`                                           | A one-item listing mapping a SHA-256-named file to its `source:id` ref.  |
| `actions/YYYY-MM-DD_topic-SUFFIX`                                | Up to 200 action files, 20 linked projects and backlinks.                |

```bash
bf read projects
bf read tasks
bf read 7d
```

For the sample project, the first command includes `"next":"Draft the product page."`; the second lists that checkbox. The last shows saved activity in the trailing seven days.

### Ordering and review signals

| View               | Order or preview                                                                                                                                                                                            |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Projects           | Deprecated notes last; task counts and first open task as `next`.                                                                                                                                           |
| Concepts / actions | Title order / newest first.                                                                                                                                                                                 |
| Home               | Latest 10 actions; notes changed in 7 days; record activity in 24 hours; upcoming items in 7 days. Changed/upcoming lists each cap at 20 and omit excerpts: read an item, or its period page, for its text. |
| Period `changed`   | Up to 20 items modified in the period but dated outside it; not paginated.                                                                                                                                  |
| Home `attention`   | Scheduled programs that failed, never succeeded locally, have a future success timestamp or exceeded twice their refresh interval.                                                                          |

Records of a [`priority: low`](sensors.md#quiet-a-high-volume-source) source stay out of period `items` and `changed` and home `upcoming`; period `sources` and home `activity` keep their counts, marked `"priority":"low"`, with the `page` listing them. Source pages, exact reads and searches include them.

Only `deprecated` closes a note: it ranks last in search and in projects, and leaves the home preview, reminders and the task queue. Other statuses, including values that `bf validate` rejects, stay open. Previews and backlinks break date ties by ref, last first, as their continuations do.

Review signals appear in folder listings and the home project preview:

| Setting or field            | Rule                                                                                                         |
| --------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Default                     | Projects that are not deprecated: 14 days after local file modification, independent of authored `updated`.  |
| `review_after`              | Override with 1–3650 days. Other note types opt in with this or `review_due`.                                |
| `review_due`                | An explicit date overrides the interval; local midnight, returned in UTC.                                    |
| `modified`, `review_source` | Local modification timestamp; deadline basis is `modified` or `review_due`.                                  |
| `review: true`              | Attention needed; `review_reasons` explains why (below).                                                     |
| `new_links`                 | Count of incoming linked items dated at or after this note's modification. Uses event dates, not copy times. |
| Exclusions                  | Deprecated notes, action attachments and reserved `index.md` and `log.md` notes receive no reminders.        |

| `review_reasons` value | Meaning                                            |
| ---------------------- | -------------------------------------------------- |
| `due`                  | Deadline reached.                                  |
| `newer_evidence`       | Newer dated evidence links to the note.            |
| `future_modified`      | Filesystem clock is ahead; attention is immediate. |
| `unknown_modified`     | Modification time cannot be represented.           |

For example, an overdue project includes:

```json
{ "review": true, "review_reasons": ["due"], "review_source": "modified" }
```

These are reminders, not verification. Copying, cloning or touching a file can reset an automatic interval; explicit deadlines remain portable. Reads change neither metadata nor tasks.

### Time meanings

| Input or timestamp | Meaning                                                |
| ------------------ | ------------------------------------------------------ |
| `today`            | Local calendar day, including daylight-saving changes. |
| `7d`               | Trailing window ending now.                            |
| Record time        | Event time.                                            |
| Note time          | Local midnight of its `updated` date.                  |
| Period `changed`   | Upstream modification time, when available.            |

A message sent last week and edited today keeps last week's event time but can appear in today's `changed` list. Future agenda items require a sensor that collects them.

## Tasks

```bash
bf read tasks
bf read projects/new-website.md#next-actions
```

The sample project's task item includes:

```json
{
  "note": "projects/new-website.md",
  "ref": "projects/new-website.md#next-actions",
  "text": "Draft the product page."
}
```

| Contract     | Rule                                                                                                                                      |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Included     | Unchecked Markdown list items in `projects/`, `concepts/` and canonical action `ACTION.md` notes, regardless of note type.                |
| Excluded     | `deprecated` notes; reserved `index.md` and `log.md` notes; action attachments; code, blockquotes and checkbox-like prose.                |
| Item fields  | Owning `note` and `title`, section `ref`, one-based `line`, `text` preview (up to 320 characters); `brain` and `uri` with several brains. |
| Identity     | Tasks can share a section ref; distinguish them by source line within the same snapshot.                                                  |
| Counts       | `total` and `summary.open`: unchecked; `summary.done`: checked; `summary.notes`: eligible notes with any checkbox, even all completed.    |
| Order / page | Note path, source line, brain name / 50 items. Counts cover all available indexed data.                                                   |
| Address      | `bf://brain/tasks` is a reserved page: no sections, note entity or alias.                                                                 |

Read the ref for full context; a task is evidence, not permission to act. Follow `next_offset`, restart after edits, and check [completeness](#incomplete-answers-and-freshness), even for an empty queue.

## Tag pages

Add `tags: [website, product]` to the sample project's frontmatter:

```bash
bf read tags
bf read bf://brain/tags/website
bf search "explanation" --scope bf://brain/tags/website
```

The directory includes this `website` entry beside `product`; the next commands list the project and search only its tagged membership:

```json
{ "tag": "website", "ref": "bf://brain/tags/website", "total": 1 }
```

| Operation or field                   | Rule                                                                                                                                                                      |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Membership                           | Exact authored frontmatter tags only; prose, sensor fields and links do not add members.                                                                                  |
| `bf read tags`                       | Directory entries: `tag`, `ref` (its address), `total` (member count), and `brain` with several brains. Directory `total` counts distinct brain/tag pairs; address order. |
| `bf read tags/website`               | Combines selected brains' local members, newest first; the same label still has separate identities.                                                                      |
| Tag identity as search query / scope | Return members / restrict word search to members; no alias or ordinary-link expansion.                                                                                    |
| Unknown tag                          | No members, no prose fallback; inspect `problems` and `stale`.                                                                                                            |
| Pages                                | Up to 200 entries; follow `next_offset`. No Markdown sections or fragments.                                                                                               |
| Graph                                | Each membership adds a built-in `tagged-with` claim supported by the whole note; no schema declaration needed. Ordinary links remain separate claims.                     |
| Typed tag link                       | Reading `bf://brain/tags/website?rel=depends-on` opens the page; authoring requires a declared role. Search queries/scopes omit `?rel=`.                                  |

See [tag rules](link-reference.md#tag-rules) for valid labels.

## Search

| Command                                                         | Expected behavior                                                                                                                          |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `bf search "visitors explanation"`                              | Finds the sample Decision section using either word.                                                                                       |
| `bf search "explanation" --scope projects`                      | Restricts matches to project notes.                                                                                                        |
| `bf search "website" --scope 7d`                                | Restricts matches to the trailing seven days.                                                                                              |
| `bf search "decision" --scope repo:github.com/team/new-website` | Searches the identity's owning note and the items that link to it, after [declaring the alias](links.md#give-a-subject-a-stable-identity). |

A search takes one scope: a folder or file (`projects`, `memories/brief`), a period (`today`, `7d`, `2026-09`), a source period (`memories/brief/7d`) or its undated records (`memories/brief/undated`), a record file, an exact identity or a tag address (`bf://brain/tags/website`). Any other segment below `memories/SOURCE` is invalid input (exit 2). A `memories/SOURCE` scope that no selected brain indexes or configures fails like its page; a folder or identity scope that names nothing returns no items. Other `bf://NAME/...` scopes are identities, even when the same address reads as a page.

### Word matching

| Rule           | Behavior                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matching       | Any of the query's first 32 distinct words; queries hold up to 4,096 characters. Case, accent and compatibility insensitive (`ß`, `ﬁ`, full-width letters); English stemming. Excerpts keep the source's characters.                                                                                                                                                                                                                               |
| Ranking        | BM25 word-frequency ranking; a section's note title and heading weigh more than body text. Project, concept and `ACTION.md` notes get a boost, except `index.md` and `log.md`; deprecated notes rank last. Records of a [`priority: low`](sensors.md#quiet-a-high-volume-source) source count half.                                                                                                                                                |
| Deduplication  | Each note appears once, through its best section; a section matching only its note's title yields the whole note. When several sources hold records of one URL, only the best-ranked record's source keeps its records; the best-ranked one's `also` lists up to five of the others' matching refs, sorted, as `bf://` addresses when several brains are selected. One source's records stay distinct, such as several highlights of one document. |
| Function words | English/French function words are dropped unless they are the entire query. A query without any word or identity, such as `!!!`, is invalid input (exit 2).                                                                                                                                                                                                                                                                                        |
| Several brains | Results alternate by each brain's own rank; positions are not comparable across brains. Select one brain with `--brain` for its own ranking.                                                                                                                                                                                                                                                                                                       |
| Limits         | No translation, French stemming, model or embedding. Words need spaces or punctuation between them: unspaced Chinese, Japanese or Thai text matches only as a whole run.                                                                                                                                                                                                                                                                           |

### Identity matching

| Query                                           | Result                                                                               |
| ----------------------------------------------- | ------------------------------------------------------------------------------------ |
| Known exact identity                            | Owner first, then linking items newest first; case-sensitive, explicit aliases only. |
| Unknown identity-shaped query, e.g. `re:invent` | Word search with `identity: unknown`.                                                |
| Tag address                                     | Exact membership, even when empty.                                                   |
| Several brains                                  | Owners from every brain first, then linking items newest first across brains.        |

`--limit` is 1–50 (default 10). Results include `ref`, `kind`, `title`, `time` and `excerpt`; notes add `status` and `type`, records their `source`, and `also` for records of other sources collapsed by URL. Collapse happens within each brain before `--offset` and `--limit` apply, so continuations stay consistent; identity and tag searches list every item. With several selected brains, each result also names its `brain` and portable `uri`. Result and listing titles are previews of at most 200 characters, cut at a word and ending in `…`; `bf read` returns the whole title.

### Source coverage

Search coverage lists, in `sources`, the searched sources that matter to the reply: those of returned records, and those needing attention because their last collection `failed` or their freshness is `stale` or `never`. `sources_omitted` counts the other searched sources. For example, when the [collected brief](getting-started.md#collect-your-first-source) matches, its source is listed:

```json
{ "sources": [{ "source": "brief", "state": "active", "freshness": "manual", "mode": "snapshot" }] }
```

A reply without items, or a search scoped to `memories` or `memories/SOURCE`, lists every searched source, including those with no matches, in each brain that indexes or configures them: coverage is then its answer. Authored-folder scopes search no source and omit both fields.

## Notes, records and identities

| Command                                       | Result                                                                                                                                 |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `bf read projects/new-website.md`             | Whole note and backlinks.                                                                                                              |
| `bf read projects/new-website.md#decision`    | Exact Decision section only.                                                                                                           |
| `bf read brief:website-brief`                 | Record from [Getting started](getting-started.md#collect-your-first-source). Record refs are `SOURCE:ID`; preserve literal `#` in IDs. |
| `bf read repo:github.com/team/new-website`    | Owning note/record after alias setup; linked-evidence page if there is no owner. Ambiguous aliases fail: use an exact ref.             |
| `bf read projects/new-website.md --rel cites` | Every item citing the note, such as through OKF `sources`: a [role page](#role-pages).                                                 |

Similar names never establish identity. See [BF links](link-reference.md#bf-links).

| Exact read       | Reply fields                                                                                                                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Note             | `brain`, `ref`, `text` (the whole file), `sha256`, `modified`, `backlinks`, optional `claims`, `uri` for a BF address; an `ACTION.md` adds `files` and linked `projects`.                         |
| Section          | `brain`, `ref` with its `#section`, `text` of that section only, and the whole file's `sha256` and `modified`.                                                                                    |
| Record           | `brain`, `ref`, `path` of its JSON file, `record` (`id`, `title` and any set `text`, `time`, `url`, `links`, `aliases`, `attributes`, `fields`), `collection`, `sha256`, `modified`, `backlinks`. |
| Any of the above | `notice`, and `problems` or `stale` when evidence was skipped; above 32 KiB, the text arrives in [pages](#large-exact-reads).                                                                     |

`sha256` is the SHA-256 of the file's bytes, also on a section read; `modified` is the file's modification time in UTC. Compare `sha256` before replacing a file you read, to detect another edit in between.

A record's text is `record.text`, never a top-level `text`. A note whose path cannot form a BF address, because it is too long or contains a control character, is skipped by search but still reads exactly, with a problem saying its backlinks are unavailable; rename it.

When several brains are selected, including a brain's direct `brains:` references, entries in lists name their `brain` and `uri`; read a result's `uri`. With one selected brain they omit both, which would only repeat the selection; `problems` still name their `brain`. A plain ref that exists in more than one selected brain fails with exit 1 and asks for a brain-qualified `bf://` address, such as `bf://brain/projects/new-website.md`.

| Graph field   | Contents and limits                                                                                                                                                                                                                         |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backlinks`   | Groups of `relation`, `total` and `items`: `cites` and declared relationships by name, then `links` for untyped links. Each previews its 5 newest items: `ref`, `title`, `time`, `kind`, a record's `source`, a note's `status` and `type`. |
| `claims`      | Up to 50 typed claims per brain made by the subject, each with the `time` of the note or record asserting it; `claims_truncated` marks omissions. Read originating evidence for full assertions.                                            |
| `relations`   | On results of an identity search or scope: subject, role, target and originating section/record of each link; `relations_truncated` marks omissions.                                                                                        |
| OKF `sources` | Built-in `cites` claims with the whole note as origin; typed BF source links retain their role and explanation.                                                                                                                             |

For example, [Connect two notes](links.md#connect-two-notes) gives the project this group:

```json
{
  "relation": "cites",
  "total": 1,
  "items": [
    {
      "ref": "concepts/explain-before-signup.md",
      "title": "Explain before asking for signup",
      "kind": "note",
      "status": "draft",
      "type": "concept"
    }
  ]
}
```

A preview names an item; read its ref for the evidence, list the whole group with a [role page](#role-pages), or search the identity (`bf search bf://brain/projects/new-website.md`) for each link's `relations`.

## Role pages

A role page lists every item linking to a note, record or identity through one relationship, newest first:

```bash
bf read projects/new-website.md --rel cites
bf read repo:github.com/team/new-website --rel depends-on --offset 50
```

After [Connect two notes](links.md#connect-two-notes), the first returns the concept as a listing item, with its excerpt:

```json
{
  "page": "relation",
  "ref": "projects/new-website.md",
  "relation": "cites",
  "total": 1,
  "items": [
    {
      "ref": "concepts/explain-before-signup.md",
      "kind": "note",
      "title": "Explain before asking for signup",
      "type": "concept",
      "status": "draft",
      "excerpt": "Explain who a product helps and what it does before asking visitors to sign up."
    }
  ]
}
```

| Rule         | Behavior                                                                                                                                                         |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REF`        | A whole note, a `source:id` record, an identity or a `bf://` address; an action folder reads its `ACTION.md`. A page or `#section` fails (exit 1).               |
| Relationship | `links` for untyped links, `cites`, or a relationship a selected brain declares in `bf.yaml`. Another value is invalid input (exit 2) naming the valid ones.     |
| Narrower     | Each brain adds the links of its relations declaring this one [`broader`](schema.md#narrower-roles-and-allowed-targets); their items carry their own `relation`. |
| Items        | Like search results, with an `excerpt`; items of every selected brain, the owning item excluded. The same claims the backlink groups count in `total`.           |
| Pages        | 50 items, fewer when they would exceed 32 KiB; follow `next_offset`. MCP `read` takes the relationship as `rel`.                                                 |

## Continuations

If the first reply returns `"next_offset":2`, continue with that value:

```bash
bf search "website" --limit 2
bf search "website" --limit 2 --offset 2
```

| Rule                  | Action                                                                                                                                                         |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `next_offset` present | Repeat with the returned offset until absent; listings also report `total`.                                                                                    |
| Same request          | Keep query/ref, scope, limit and brain selection unchanged.                                                                                                    |
| Snapshot changes      | Restart after edits or a moving relative period changes ordering.                                                                                              |
| Offset                | Zero-based combined order across brains; non-negative integer below 2^63.                                                                                      |
| Exact reads           | Offsets count characters of the note or record text; see [large exact reads](#large-exact-reads).                                                              |
| Page sizes            | Folders/tags: 200; tasks/periods/role pages: 50; source overviews: 20. Not full-result limits.                                                                 |
| Large items           | A page or search ends early when its items, with the page's summaries such as `changed`, would exceed 32 KiB; `next_offset` continues after the last returned. |
| Home/source catalogs  | Summaries; follow a listing link first.                                                                                                                        |

The last page still needs [completeness checks](#incomplete-answers-and-freshness).

### Large exact reads

An exact read whose reply would exceed 32 KiB of serialized JSON returns its Markdown or record text in pages, starting at offset 0. A reply that fits returns whole and rejects a non-zero offset. For example, a note of 1,000 short `## Entry` sections:

```bash
python3 -c 'print("# Log\n\n" + "".join(f"## Entry {n}\n\nChecked the page.\n\n" for n in range(1000)))' > concepts/log.md
bf read concepts/log.md
```

The reply has `"offset":0`, the text's `total_characters`, a `next_offset` and an `outline` whose first entries are `concepts/log.md#log` and `concepts/log.md#entry-0`; its 1,001 sections exceed the outline's 200, so `"outline_truncated":true`. Delete `concepts/log.md` afterward.

| Field                                       | Meaning                                                                                                                                |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `text`, or `record.text`                    | This page's slice of the note or record text. Pages end at a line break when one falls in their second half.                           |
| `offset`, `next_offset`, `total_characters` | Positions and length in Unicode characters of the text, not bytes or JSON.                                                             |
| `sha256`, `modified`                        | The whole file's SHA-256 and modification time, identical on every page.                                                               |
| `outline`                                   | First page of a note: each section of the returned text with its `ref`, `title` and `characters`; up to 200, then `outline_truncated`. |

The first page carries every other field: backlinks, claims, an action's files and projects, or a record's other fields and collection. Later pages carry `brain`, `ref`, their slice, the paging fields, `sha256`, `modified`, `notice` and any `problems` or `stale`.

1. Read a section from the `outline` when it answers the question.
1. Otherwise repeat the same read with each `next_offset` and concatenate the text slices in order.
1. Check that every page names the same `sha256`; restart at offset 0 if it changes. A whole note's concatenation has that SHA-256 as UTF-8.

## Incomplete answers and freshness

| Signal                                 | Meaning / action                                                                                                                                                                                                                                   |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `problems`                             | Objects with `error` and, when known, `brain` and `file`: skipped or unreadable files (200 per brain, then a count), identities or brains. Resolve and repeat.                                                                                     |
| `stale`                                | A writer prevented cache refresh. Retry after it finishes.                                                                                                                                                                                         |
| Source `failed`, `freshness`, `window` | Collection health and local coverage; inspect `bf status` and the source page.                                                                                                                                                                     |
| Damaged cache                          | Run `bf build`; exact record reads may still work from files.                                                                                                                                                                                      |
| Cache write access                     | Search and read refresh the brain's `.bf/` directory; a read-only brain fails with an error that names it. Grant write access or use a copy. `.bf` must be your own directory, not a link: remove one that another account owns so bf rebuilds it. |

For example, zero matches for today's meeting cannot establish absence if its sensor failed. A successful `bf build` refreshes the cache, not provider data. Fresh sources cover only their collected window.

Malformed files, symlinks and special files are skipped and reported; healthy brains still answer. A readable record can resolve, but a missing record in a partly unreadable source cannot be declared absent.

| Source `freshness` | Meaning                                                                |
| ------------------ | ---------------------------------------------------------------------- |
| `manual`           | No schedule.                                                           |
| `never`            | No successful collection on this machine.                              |
| `fresh`            | Last local success is not in the future and is within twice `refresh`. |
| `stale`            | Last local success is in the future or older than twice `refresh`.     |
| `unknown`          | Source disabled or historical.                                         |

Source state is `active` (configured and enabled), `disabled` (configured with `enabled: false`) or `historical` (records retained for a source no longer in `bf.yaml`). `last_collected` and `window` describe retained local coverage; shared records do not carry another machine's run history.

## Reply schemas

`bf search` and `bf read` replies follow published JSON Schemas (draft 2020-12), shared by the CLI and MCP: [search](../search-reply.schema.json) and [read](../read-reply.schema.json). Print the installed version's schema offline and validate a saved reply with any JSON Schema validator, for example:

```bash
bf schema --kind search-reply > search-reply.schema.json
bf search "visitors clear explanation" > reply.json
uvx check-jsonschema --schemafile search-reply.schema.json reply.json
```

The validator prints `ok -- validation done`. The read schema lists each reply shape (home, period, sources, listing and role pages, tasks, tags, identity, note and record reads, including text pages); objects reject undescribed fields, so a new field is a documented contract change. Every reply in the framework's own test suite is validated against these schemas.

## MCP tool contract

Transport: **stdio**. [Set up your host](mcp.md).

| Tool     | Arguments and defaults                                      | Returns                                                   |
| -------- | ----------------------------------------------------------- | --------------------------------------------------------- |
| `read`   | `ref=""`, `rel=""`, `offset=0`                              | Home, page, note, section, record, identity or role page. |
| `search` | Required `query`; `scope=""`, `limit=10` (1–50), `offset=0` | Ranked matches with exact refs.                           |

### Example exchange

Call `search`, then pass its returned ref to `read`:

```json
{ "query": "visitors clear explanation", "scope": "projects" }
```

```json
{ "ref": "projects/new-website.md#decision" }
```

The read returns the saved Decision section. Text and structured tool results contain the same JSON value as the CLI; like CLI output, the text writes DEL and C1 control characters as JSON escapes. Invalid arguments return an error result starting with `invalid input:`, such as `invalid input: give words or an identity to search` for `!!!`.

### Client checklist

| Concern        | Contract                                                                                                       |
| -------------- | -------------------------------------------------------------------------------------------------------------- |
| Selection      | Roots fixed at startup; direct `brains:` references reread per request. Restart to change roots or BF version. |
| Disambiguation | A `bf://NAME/...` ref chooses one selected brain, as in the CLI; it grants no additional access.               |
| Continuation   | Follow `next_offset`; assemble [exact-read text pages](#large-exact-reads).                                    |
| Completeness   | Inspect `problems`, `stale` and source coverage.                                                               |
| Evidence       | Whole-note/identity reads preview backlinks and claims; `rel` lists a group; sections return only their text.  |
| Errors         | Invalid or unknown arguments, such as a misspelled `scope`, fail before retrieval; errors hide brain paths.    |
| Execution      | Neither tool collects, runs providers or follows links onto the network.                                       |
| Guidance       | Tool titles and server `instructions` describe the search, read and verify loop; they grant no authority.      |
