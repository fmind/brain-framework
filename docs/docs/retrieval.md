---
description: Implement CLI and MCP clients with correct pagination, chunk assembly and completeness checks.
---

# Retrieval reference

Reply fields and rules for CLI/MCP clients. For everyday use, start with [Search and read](search.md). Examples run inside the `brain` from [Getting started](getting-started.md); JSON snippets show selected fields.

## Pages

Pages are computed views, not files to edit. They combine selected brains and label each entry. Use `bf read bf://brain/projects` to select one brain's projects, or `bf read bf://brain/` for its home.

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

| View               | Order or preview                                                                                                                          |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Projects           | Deprecated notes last; task counts and first open task as `next`.                                                                         |
| Concepts / actions | Title order / newest first.                                                                                                               |
| Home               | Latest 10 actions; notes changed in 7 days; record activity in 24 hours; upcoming items in 7 days. Changed/upcoming lists each cap at 20. |
| Period `changed`   | Up to 20 items modified in the period but dated outside it; not paginated.                                                                |
| Home `attention`   | Scheduled programs that failed, never succeeded locally, have a future success timestamp or exceeded twice their refresh interval.        |

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

| Contract     | Rule                                                                                                                                   |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Included     | Unchecked Markdown list items in `projects/`, `concepts/` and canonical action `ACTION.md` notes, regardless of note type.             |
| Excluded     | `deprecated` notes; reserved `index.md` and `log.md` notes; action attachments; code, blockquotes and checkbox-like prose.             |
| Item fields  | `brain`, owning `note` and `title`, section `ref`, portable `uri`, one-based `line`, `text` preview (up to 320 characters).            |
| Identity     | Tasks can share a section ref; distinguish them by source line within the same snapshot.                                               |
| Counts       | `total` and `summary.open`: unchecked; `summary.done`: checked; `summary.notes`: eligible notes with any checkbox, even all completed. |
| Order / page | Note path, source line, brain name / 50 items. Counts cover all available indexed data.                                                |
| Address      | `bf://brain/tasks` is a reserved page: no sections, note entity or alias.                                                              |

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
{ "brain": "brain", "tag": "website", "ref": "bf://brain/tags/website", "total": 1 }
```

| Operation or field                   | Rule                                                                                                                                                  |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Membership                           | Exact authored frontmatter tags only; prose, sensor fields and links do not add members.                                                              |
| `bf read tags`                       | Directory entries: `brain`, `tag`, `ref`, `uri`, `total` (member count). Directory `total` counts distinct brain/tag pairs; URI order.                |
| `bf read tags/website`               | Combines selected brains' local members, newest first; the same label still has separate identities.                                                  |
| Tag identity as search query / scope | Return members / restrict word search to members; no alias or ordinary-link expansion.                                                                |
| Unknown tag                          | No members, no prose fallback; inspect `problems` and `stale`.                                                                                        |
| Pages                                | Up to 200 entries; follow `next_offset`. No Markdown sections or fragments.                                                                           |
| Graph                                | Each membership adds a built-in `tagged-with` claim supported by the whole note; no schema declaration needed. Ordinary links remain separate claims. |
| Typed tag link                       | Reading `bf://brain/tags/website?rel=depends-on` opens the page; authoring requires a declared role. Search queries/scopes omit `?rel=`.              |

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

| Rule           | Behavior                                                                                                                                                                                                             |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matching       | Any of the query's first 32 distinct words; queries hold up to 4,096 characters. Case, accent and compatibility insensitive (`ß`, `ﬁ`, full-width letters); English stemming. Excerpts keep the source's characters. |
| Ranking        | BM25 word-frequency ranking; headings weigh more than body text. Project, concept and `ACTION.md` notes get a boost, except `index.md` and `log.md`; deprecated notes rank last.                                     |
| Deduplication  | Each note appears once, through its best section.                                                                                                                                                                    |
| Function words | English/French function words are dropped unless they are the entire query. A query without any word or identity, such as `!!!`, is invalid input (exit 2).                                                          |
| Several brains | Results alternate by each brain's own rank; positions are not comparable across brains. Select one brain with `--brain` for its own ranking.                                                                         |
| Limits         | No translation, French stemming, model or embedding. Words need spaces or punctuation between them: unspaced Chinese, Japanese or Thai text matches only as a whole run.                                             |

### Identity matching

| Query                                           | Result                                                                               |
| ----------------------------------------------- | ------------------------------------------------------------------------------------ |
| Known exact identity                            | Owner first, then linking items newest first; case-sensitive, explicit aliases only. |
| Unknown identity-shaped query, e.g. `re:invent` | Word search with `identity: unknown`.                                                |
| Tag address                                     | Exact membership, even when empty.                                                   |
| Several brains                                  | Owners from every brain first, then linking items newest first across brains.        |

`--limit` is 1–50 (default 10). Results include kind, title, time and excerpt. Source coverage lists the searched sources, including those with no matches, in each brain that indexes or configures them; authored-folder scopes omit it.

## Notes, records and identities

| Command                                    | Result                                                                                                                                 |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `bf read projects/new-website.md`          | Whole note and backlinks.                                                                                                              |
| `bf read projects/new-website.md#decision` | Exact Decision section only.                                                                                                           |
| `bf read brief:website-brief`              | Record from [Getting started](getting-started.md#collect-your-first-source). Record refs are `SOURCE:ID`; preserve literal `#` in IDs. |
| `bf read repo:github.com/team/new-website` | Owning note/record after alias setup; linked-evidence page if there is no owner. Ambiguous aliases fail: use an exact ref.             |

Similar names never establish identity. See [BF links](link-reference.md#bf-links).

| Exact read       | Reply fields                                                                                                                                                                |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Note             | `brain`, `ref`, `text` (the whole file), `backlinks`, optional `claims`, `uri` for a BF address; an `ACTION.md` adds `files` and linked `projects`.                         |
| Section          | `brain`, `ref` with its `#section`, `text` of that section only.                                                                                                            |
| Record           | `brain`, `ref`, `path` of its JSON file, `record` (`id`, `title` and any set `text`, `time`, `url`, `links`, `aliases`, `attributes`, `fields`), `collection`, `backlinks`. |
| Any of the above | `notice`, and `problems` or `stale` when evidence was skipped; above one chunk, a [JSON chunk](#large-exact-reads) instead.                                                 |

A record's text is `record.text`, never a top-level `text`. A note whose path cannot form a BF address, because it is too long or contains a control character, is skipped by search but still reads exactly, with a problem saying its backlinks are unavailable; rename it.

When several brains are selected, read a result's `uri`. A plain ref that exists in more than one selected brain fails with exit 1 and asks for a brain-qualified `bf://` address, such as `bf://brain/projects/new-website.md`.

| Graph field   | Contents and limits                                                                                                                     |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `backlinks`   | Incoming links grouped by declared relationship, then untyped links; 20 previews per group plus `total`. Continue with identity search. |
| `relations`   | Subject, role, target and originating section/record; `relations_truncated` marks omissions.                                            |
| `claims`      | Up to 50 typed claims per brain about the subject; `claims_truncated` marks omissions. Read originating evidence for full assertions.   |
| OKF `sources` | Whole-note origin; typed BF source links retain their role and explanation.                                                             |

## Continuations

If the first reply returns `"next_offset":2`, continue with that value:

```bash
bf search "website" --limit 2
bf search "website" --limit 2 --offset 2
```

| Rule                  | Action                                                                                                          |
| --------------------- | --------------------------------------------------------------------------------------------------------------- |
| `next_offset` present | Repeat with the returned offset until absent; listings also report `total`.                                     |
| Same request          | Keep query/ref, scope, limit and brain selection unchanged.                                                     |
| Snapshot changes      | Restart after edits or a moving relative period changes ordering.                                               |
| Offset                | Zero-based combined order across brains; non-negative integer below 2^63.                                       |
| Page sizes            | Folders/tags: 200; tasks/periods: 50; source overviews: 20. Not full-result limits.                             |
| Large items           | A page or search ends early when its items would exceed 2 MiB; `next_offset` continues after the last returned. |
| Home/source catalogs  | Summaries; follow a listing link first.                                                                         |

The last page still needs [completeness checks](#incomplete-answers-and-freshness).

### Large exact reads

Exact replies longer than 65,536 characters of serialized JSON return JSON chunks, starting at offset 0. A reply that fits in one chunk returns whole and rejects a non-zero offset.

| Field                                       | Meaning                                                       |
| ------------------------------------------- | ------------------------------------------------------------- |
| `format: json`, `chunk`                     | Piece of a serialized JSON reply, not a complete note/record. |
| `offset`, `next_offset`, `total_characters` | Positions/length in Unicode characters, not bytes or items.   |
| `sha256`                                    | Digest of the full UTF-8 serialized reply.                    |

1. Repeat the same read with each `next_offset`.
1. Concatenate `chunk` strings in order.
1. Verify the common SHA-256 against the UTF-8 concatenation; restart if the digest changes.
1. Parse the assembled JSON.

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

## MCP tool contract

Transport: **stdio**. [Set up your host](mcp.md).

| Tool     | Arguments and defaults                                      | Returns                                        |
| -------- | ----------------------------------------------------------- | ---------------------------------------------- |
| `read`   | `ref=""`, `offset=0`                                        | Home, page, note, section, record or identity. |
| `search` | Required `query`; `scope=""`, `limit=10` (1–50), `offset=0` | Ranked matches with exact refs.                |

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
| Continuation   | Follow `next_offset`; assemble [exact-read chunks](#large-exact-reads).                                        |
| Completeness   | Inspect `problems`, `stale` and source coverage.                                                               |
| Evidence       | Whole-note/identity reads include bounded backlinks/claims; sections return only their text.                   |
| Errors         | Invalid or unknown arguments, such as a misspelled `scope`, fail before retrieval; errors hide brain paths.    |
| Execution      | Neither tool collects, runs providers or follows links onto the network.                                       |
