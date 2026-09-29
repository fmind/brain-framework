---
description: Find saved evidence, read exact sources and check whether results are complete.
---

# Search and read

Use `bf search` to find evidence and `bf read` to open it. Both work offline and notice file edits automatically. Run them inside your brain.

**Search a few words → read the returned ref → check that the reply is complete.** Use [pages](#pages) to browse instead.

## Search

After [Getting started](getting-started.md), try:

```bash
bf search "visitors clear explanation"
bf search "product page" --scope projects
```

The first search finds the decision's reason; the second searches project notes only. Any query word can match, and results matching more of the words rank higher. Put variants in one query, such as `signup "sign up" registration`: BF does not translate or expand words.

| Write                                 | To match                                                              |
| ------------------------------------- | --------------------------------------------------------------------- |
| `visitors signup`                     | Either word, in any form English stemming relates, such as `visitor`. |
| `"clear explanation"`                 | The exact phrase.                                                     |
| `synchro*`                            | Words starting with `synchro`, such as `synchronizes`.                |
| `repo:github.com/example/new-website` | The identity's owner and every item linking to it.                    |

Each item has a `title`, an `excerpt` and a `ref`. A note states its `date`; a record states its event `time` with your local offset. `sections` lists up to three other matching sections of the same note, and `unmatched` names query words found nowhere, so you can rephrase:

```bash
bf search '"clear explanation" zebra'
```

The reply returns the Decision section and `"unmatched":["zebra"]`. Section titles name their parent headings, such as `Portfolio review — Vega — Budget`, and a note's tags rank like headings. Among equal scores, the newest item comes first. Records of several sources that share a URL appear once; `also` lists the other refs.

If a query misses, use the evidence's own words and drop the scope. The sample says “visitors” and “signing up”, so “customer conversion” need not find it. Unspaced Chinese, Japanese or Thai text matches only as a whole run between punctuation.

### Narrow a search

`--scope` takes one scope:

| Scope                                 | Searches                                                               |
| ------------------------------------- | ---------------------------------------------------------------------- |
| `projects`, `concepts/team`           | A folder or file.                                                      |
| `memories/brief`, `memories/brief/7d` | One source, optionally within a period.                                |
| `today`, `7d`, `2026-09`              | A period of local time.                                                |
| `2026-09-21..2026-09-25`              | Local days from the first through the last, inclusive.                 |
| `repo:github.com/example/new-website` | The identity's owner and the items linking to it.                      |
| `bf://brain/tags/website`             | Notes carrying that tag; see [tag rules](link-reference.md#tag-rules). |

## Read a ref

```bash
bf read projects/new-website.md#decision
```

The `text` field holds the original Decision section. Omit `#decision` to read the whole note with its backlinks. Use returned refs exactly:

| Read     | Example                                       | Result                                                                                   |
| -------- | --------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Note     | `bf read projects/new-website.md`             | Whole note, backlinks and the claims it makes.                                           |
| Section  | `bf read projects/new-website.md#decision`    | That section only.                                                                       |
| Record   | `bf read brief:website-brief`                 | The [collected brief](getting-started.md#collect-your-first-source).                     |
| Identity | `bf read repo:github.com/example/new-website` | Its owning note, after [declaring the alias](links.md#give-a-subject-a-stable-identity). |
| Relation | `bf read projects/new-website.md --rel cites` | Every item citing the note: a [relation page](retrieval.md#relation-pages).              |

A whole read previews the five newest backlinks of each relation, each with a short `excerpt` and single-value `fields` such as a status. A note above 32 KiB returns its text in pages. The first page carries its backlinks and, when the note has sections, an `outline` of section refs with only the first 4 KiB of text: read the section you need, or follow `next_offset`.

With several brains selected, each result also names its `brain` and a `uri` such as `bf://brain/projects/new-website.md#decision`. Read the `uri`: a plain ref that exists in two brains fails.

## Pages

Pages are computed views for browsing:

| Command                          | Use it to…                                                     |
| -------------------------------- | -------------------------------------------------------------- |
| `bf read`                        | See projects needing review, recent work and failing programs. |
| `bf read projects`               | Find each project's state and first open task.                 |
| `bf read tasks`                  | List open checkboxes with the section owning each.             |
| `bf read actions`                | Find a session to resume.                                      |
| `bf read 7d`                     | Browse the last seven days of dated notes and records.         |
| `bf read 2026-09-21..2026-09-25` | Browse a range of local days.                                  |
| `bf read memories`               | See each source's record count, freshness and coverage.        |
| `bf read tags`                   | List topic labels and their note counts.                       |

For the New website project, `bf read projects` shows `"next":"Draft the product page."` and `bf read tasks` lists that checkbox with its `ref` and `line`. A project with `"review":true` needs attention; `review_reasons` says why. Pages use saved dates and never fetch anything. The [page reference](retrieval.md#pages) lists every page and its rules.

## Incomplete answers and freshness

Check the reply before concluding that evidence is absent:

| Signal                       | What to do                                                                       |
| ---------------------------- | -------------------------------------------------------------------------------- |
| `problems`                   | Repair the named files or brains, then repeat the request.                       |
| `stale`                      | Another command is updating the cache; retry in a moment for the newest results. |
| `sources`, `sources_omitted` | Check that the sources you need collected the period you ask about.              |
| `next_offset`                | Repeat the same request with `--offset` for the rest.                            |

No match for today's meeting can mean the meeting was never collected. `bf read memories` and `bf status` show local coverage without contacting providers. If the cache is damaged, `bf build` rebuilds it from the files. [What BF does not do](concepts.md#what-bf-does-not-do) lists the limits these signals guard.

## Continue a listing

When a reply contains `next_offset`, repeat the request with that value:

```bash
bf search "product" --limit 1
bf search "product" --limit 1 --offset 1
```

Keep the query, scope, limit and brain selection unchanged, and stop when `next_offset` is absent. Listings and large exact reads continue the same way; see [continuations](retrieval.md#continuations).

## Keep answers findable

Save recurring questions as [retrieval cases](checks.md#retrieval-cases). When a useful question misses, improve the owning note or selected evidence, then check that the answer stays reachable.
