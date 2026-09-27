# Retrieval reference

Use this page when interpreting a reply or building a CLI/MCP client. It defines matching, pages, identities, continuations and completeness. Start with [Search and read](search.md) for everyday use.

Examples use the `brain` created in [Getting started](getting-started.md). Run them inside its directory, or add `--brain ~/brain`.

## Pages

A page is a computed view of the files, rather than a file to edit:

```bash
bf read projects
bf read actions
bf read 7d
```

These show project notes and tasks, action entries, and the last seven days of activity.

| Read                                                             | See                                                                                                   |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `bf read`                                                        | Current project notes, recent actions, changed notes, activity, upcoming items and collection alerts. |
| `projects`, `concepts`, `actions` or a subfolder                 | Notes and their tasks; one `ACTION.md` per action.                                                    |
| `today`, `yesterday`, `2026-09-27`, `2026-09`, `12h`, `7d`, `2w` | Items in that period, modifications and source counts.                                                |
| `memories`                                                       | Sources, record counts, freshness and collected windows.                                              |
| `memories/SOURCE`                                                | Source coverage, partitions and latest records.                                                       |
| `memories/SOURCE/PERIOD`                                         | That source's items in a local period.                                                                |
| `memories/SOURCE/FILE`                                           | Items from one UTC-month partition, `snapshot` or `undated`.                                          |
| `actions/YYYY-MM-DD_slug`                                        | The action, its files, linked projects and backlinks.                                                 |

Pages combine selected brains and label each entry. `bf read bf://brain/projects` restricts the projects page to the selected brain named `brain`; `bf read bf://brain/` opens its home.

### Ordering and review signals

Project pages put deprecated notes last; concepts sort by title, actions newest first. Notes expose task counts and their first open task as `next`.

Draft or stable projects get `review: true` when `updated` is missing, older than 14 days, or earlier than incoming evidence. Future-dated projects wait until their date before being considered for review. `new_links` counts newer linked items. These signals request a review; they do not change the note's status or tasks.

The home page previews the latest 10 actions, notes changed in 7 days, record activity in 24 hours and upcoming items in 7 days. Changed and upcoming lists each hold at most 20 items. `attention` names scheduled programs that failed, never succeeded locally or exceeded twice their refresh interval.

### Time meanings

`today` uses the local calendar day, including daylight-saving changes. `7d` is a trailing window ending now. Record time means event time; a note uses local midnight of its `updated` date. Period `changed` lists use upstream modification time when available.

For example, a message sent last week and edited today keeps last week's event time, but can appear in today's `changed` list. Future agenda items require a sensor that collects future events.

## Tag pages

Add `tags: [website]` to the website project's frontmatter, then browse or search that membership:

```bash
bf read tags
bf read bf://brain/tags/website
bf search "explanation" --scope bf://brain/tags/website
```

The tag page includes that project. The search looks only in notes explicitly tagged `website`; mentioning the word in prose or linking to the tag page does not add membership.

`bf read tags` returns a directory with `brain`, `tag`, `ref`, `uri` and `total` (the number of tagged notes). Directory `total` counts distinct brain/tag pairs, sorted by encoded URI. Member pages sort newest first. Both page types hold at most 200 entries and provide `next_offset`.

`bf read tags/website` combines each selected brain's local members and labels their origins. The same label in two brains still has two identities. A tag address used as the search query returns its members; a tag scope restricts word search to those members. Neither operation expands aliases or ordinary links. Unknown tags return no members, without falling back to prose. Check `problems` and `stale` before treating an empty page as complete; see [tag rules](schema.md#tag-rules).

Each frontmatter membership also produces a built-in `tagged-with` claim, supported by the whole note. No schema declaration is required. Sensor fields and manually written links, even with that role name, do not add membership. The graph retains ordinary links to tag pages as separate claims.

Reading a typed link such as `bf://brain/tags/website?rel=depends-on` opens its tag page; the role must be declared when authoring the link. Search queries and scopes use the tag identity without `?rel=`. Tag pages have no Markdown sections, so fragments are rejected.

## Search

Search a few words from the evidence, then narrow the scope:

```bash
bf search "visitors explanation"
bf search "explanation" --scope projects
bf search "website" --scope 7d
bf search "decision" --scope repo:github.com/team/new-website
```

The final example requires the explicit repository alias from [Linking knowledge](links.md#give-a-subject-a-stable-identity). A scope is a folder, file, period or exact identity.

### Word matching

Search matches any query word and ranks results with BM25, a word-frequency ranking method. Headings carry more weight than body text. Projects, concepts and action entry notes get a boost; archived and deprecated notes rank last. Each note appears once through its best section.

Case and accents do not matter. English stemming matches word forms; English and French function words are dropped unless they are the entire query. There is no translation, French stemming, model or embedding. Rephrase a miss with words the evidence contains: search `explanation` for the website decision rather than expecting a translation to match it.

### Identity matching

An exact identity query returns its owner first, then linking items newest first. Matching is case-sensitive and follows only explicit aliases. An unknown identity-shaped query, such as `re:invent`, searches its words and reports `identity: unknown`. Tag addresses always use exact membership.

`--limit` accepts 1–50 results, default 10. Results include kind, title, time and an excerpt. Source coverage includes searched sources even when they produced no matches; authored-folder scopes omit it.

## Notes, records and identities

Read the returned ref exactly:

```bash
bf read projects/new-website.md
bf read projects/new-website.md#decision
bf read local-documents:website-demo/brief.txt
```

The first opens the whole note and its backlinks; the second returns only the Decision section. The third opens the record created by the [sensor walkthrough](sensors.md#collect-and-read). Record refs are `SOURCE:ID`; preserve any literal `#` in an id.

An identity such as `repo:github.com/team/new-website` resolves to its owning note or record. Without an owner, it opens a page of linked evidence. Ambiguous aliases fail and ask for an exact ref. Similar names never establish equivalence; see [BF links](schema.md#bf-links).

Backlinks group incoming links by declared relationship, followed by untyped links. Each group previews 20 items and reports its `total`. `relations` give each link's subject, role, target and originating section or record. OKF `sources` links use the whole note as their origin; typed BF source links retain their role and claim explanation.

`claims` previews up to 50 typed claims per brain about the subject. `claims_truncated` or an item's `relations_truncated` signals omitted claims. Read originating notes or records for the full assertions; use identity search to continue past backlink previews.

## Continuations

Follow a listing or search reply's `next_offset` until it is absent. Search also returns `more: true` when more results exist; listings report `total`. The [completeness fields](#incomplete-answers-and-freshness) still apply after the last page.

For example, if a search with `--limit 2` returns `next_offset: 2`, continue with the same query and limit:

```bash
bf search "website" --limit 2
bf search "website" --limit 2 --offset 2
```

Use the offset actually returned. Keep the query, ref, scope, limit and brain selection unchanged. Offsets start at zero and count the combined order across brains. Edits or a moving relative period can change that order; restart after changes.

Folder pages hold at most 200 notes, period pages 50 items and source overviews 20 records. These are page sizes, not full-result limits. Home and source catalogs are summaries: follow a listing link first.

### Large exact reads

Exact replies above 4 MiB use JSON chunks. Each reply includes `format: json`, `chunk`, `offset`, `total_characters`, `sha256` and, until complete, `next_offset`:

1. Repeat the same read with each `next_offset`.
1. Concatenate the `chunk` strings in order.
1. Verify their common SHA-256 against the UTF-8 concatenation.
1. Parse the assembled JSON to recover the full reply.

Chunk offsets count Unicode characters, not bytes or items. Restart if the digest changes. All offsets must be non-negative integers below 2^63. A chunk is part of a serialized reply, so its text alone is not a complete note or record.

## Incomplete answers and freshness

Before interpreting an empty result as “there is no evidence,” inspect these fields:

| Field                                  | Meaning                                                                    | What to do                                           |
| -------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------- |
| `problems`                             | Files, identities or brains were skipped or could not be read.             | Resolve the reported problem and repeat the request. |
| `stale`                                | A writer prevented the cache from refreshing.                              | Repeat after the writer finishes.                    |
| Source `failed`, `freshness`, `window` | Collection health and retained local coverage, independently of the cache. | Check `bf status` and the relevant source page.      |

A successful `bf build` can make the cache current while a failed sensor leaves old records. Conversely, a fresh source may cover only the window you collected. Neither state proves complete provider history.

Malformed files, symlinks and special files are skipped and reported. Healthy brains still answer when another selected brain is unavailable. A known record can be read from a healthy partition, but a missing record in a partly unreadable source cannot be declared absent.

Source state is `active`, `disabled` or `historical`. Freshness uses the following values:

| Freshness | Meaning                                        |
| --------- | ---------------------------------------------- |
| `manual`  | No schedule is configured.                     |
| `never`   | No successful collection on this machine yet.  |
| `fresh`   | Last local success was within twice `refresh`. |
| `stale`   | Last local success was longer ago.             |
| `unknown` | The source is disabled or historical.          |

`last_collected` and `window` describe retained local coverage. Shared records do not carry the collecting machine's run history.

If the search cache is damaged, run `bf build`. Exact record reads may still resolve from files. A fresh cache does not establish current provider data.

## MCP tool contract

`bf mcp` exposes the same retrieval services over stdio. Start with the [host connection guide](mcp.md) before building a client.

| Tool     | Arguments                           | Result                                                                                                                     |
| -------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `read`   | `ref`, optional `brain`, `offset`   | Home when `ref` is empty; otherwise a page, note, section, record or identity. `brain` disambiguates within the selection. |
| `search` | `query`, `scope`, `limit`, `offset` | Matches across selected brains. `limit` accepts 1–50; schema-invalid arguments fail before retrieval runs.                 |

For example, the arguments `{"ref":"projects/new-website.md#decision","brain":"brain"}` retrieve the same Decision section as the CLI example above. Text and structured tool results carry the same JSON value as the CLI. Follow [continuations](#continuations) for pagination and lossless large reads. Errors hide brain paths.

Selected roots are fixed when the server starts; direct `brains:` declarations are reread for each request. Restart after changing root selection or updating Brain Framework. The `brain` argument on `read` cannot access a brain outside that selection.

Both tools accept [BF identities and links](schema.md#bf-links) without expanding access or running providers. Exact reads include bounded backlinks and claims; section reads contain only the section. Inspect incompleteness signals before treating results as complete.
