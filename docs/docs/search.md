---
icon: lucide/search
description: Find saved evidence, read exact sources and check whether results are complete.
---

# Search and read

Use `bf search` to find evidence and `bf read` to open it. Both work offline and notice note edits and new collections automatically; after editing a record file by hand, run `bf build`. Run them inside your brain.

**Search a few words → read the returned ref → check that the reply is complete.** Use [pages](#pages) to browse instead.

## Search

After [Getting started](getting-started.md), try:

```bash
bf search "visitors clear explanation"
bf search "product page" --scope projects
```

The first search finds the decision's reason; the second searches project notes only. Any query word can match, and results matching more of the words rank higher. Put variants in one query, such as `signup "sign up" registration`: BF does not translate or expand words.

| Write                                 | To match                                                                                       |
| ------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `visitors signup`                     | Either word, in any form English stemming relates, such as `visitor`.                          |
| `"clear explanation"`                 | The exact phrase.                                                                              |
| `synchro*`                            | Words starting with `synchro`, such as `synchronizes`.                                         |
| `"son" checklist`                     | Either word: quoting keeps a function word, such as French `son`, that search otherwise drops. |
| `repo:github.com/example/new-website` | The identity's owner and every item linking to it.                                             |

Each item has a `title`, an `excerpt` and a `ref`. A note states its `date`; a record states its event `time` with your local offset. `sections` lists up to three other matching sections of the same note, and `unmatched` names query words found nowhere, so you can rephrase; `suggestions` offers close words the brain holds for a misspelling:

```bash
bf search '"clear explanation" zebra'
bf search 'explenation'
```

The first reply returns the Decision section and `"unmatched":["zebra"]`. The second finds nothing and returns `"unmatched":["explenation"]` with `"suggestions":{"explenation":["explanation"]}`: search the suggested word to confirm. Section titles name their parent headings, such as `Portfolio review — Vega — Budget`, and a note's tags weigh more than body text, less than headings. Among equal scores, the newest item comes first. Records of several sources that share a URL appear once; `also` lists the other refs.

If a query misses, use the evidence's own words and drop the scope. The sample says “visitors” and “signing up”, so “customer conversion” need not find it. Accents fold only in Latin script, and Chinese, Japanese, Thai or Devanagari text matches by runs or fragments rather than words; see [other scripts](retrieval.md#search).

### Narrow a search

`--scope` takes one scope: a folder or note such as `projects`, a source such as `memories/brief`, a period such as `7d` or `2026-09-21..2026-09-25`, an identity or a tag address. [Scopes](retrieval.md#scopes) lists every form.

## Read a ref

```bash
bf read projects/new-website.md#decision
```

The `text` field holds the original Decision section. Omit `#decision` to read the whole note with its backlinks. Use returned refs exactly:

| Read     | Example                                       | Result                                                                                                                    |
| -------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Note     | `bf read projects/new-website.md`             | Whole note, backlinks, the claims it makes and, for a project or a note with `stale_after`, its tasks and review signals. |
| Section  | `bf read projects/new-website.md#decision`    | That section, with its note's title, type, status and date.                                                               |
| Record   | `bf read brief:website-brief`                 | The [collected brief](getting-started.md#collect-your-first-source).                                                      |
| Identity | `bf read repo:github.com/example/new-website` | Its owning note, after [declaring the alias](links.md#give-a-subject-a-stable-identity).                                  |
| Relation | `bf read projects/new-website.md --rel cites` | Every item citing the note: a [relation page](retrieval.md#relation-pages).                                               |

A whole read previews the five newest backlinks of each relation, each with a short `excerpt` and single-value `fields` such as a status. A note above 32 KiB returns its text in pages. The first page carries its backlinks and, when headings inside it divide the note, an `outline` of section refs with only the first 4 KiB of text: read the section you need, or follow `next_offset`.

With several brains selected, each result also names its `brain` and a `uri` such as `bf://brain/projects/new-website.md#decision`. Read the `uri`: a plain ref that exists in two brains fails.

## Pages

Pages are computed views for browsing. `bf read` alone opens the home page: projects needing review first, recent work and failing programs. `bf read projects`, `tasks`, `actions`, `7d`, `memories` and `tags` list the rest; [pages](retrieval.md#pages) lists every page and its rules.

For the New website project, `bf read projects` shows `"next":"Draft the product page."` and `bf read tasks` lists that checkbox with its `ref` and `line`. A project with `"review":true` needs attention: `review_reasons` says why, and `newer` names linked evidence to read first; see [review reminders](brain.md#review-reminders). Pages use saved dates and never fetch anything.

## Check that a reply is complete

Before concluding that evidence is absent, look for `problems`, `stale`, sources that failed, are `overdue`, `never` succeeded or fell silent, and `next_offset`: [incomplete answers](retrieval.md#incomplete-answers-and-freshness) says what each means and what to do. No match for today's meeting can mean the meeting was never collected.

## Continue a listing

When a reply contains `next_offset`, repeat the request with that value:

```bash
bf search "project" --limit 1
bf search "project" --limit 1 --offset 1
```

The first reply returns `concepts/welcome.md` with `"next_offset":1`; the second returns `projects/new-website.md` without `next_offset`, which ends the listing. Keep the query, scope, limit and brain selection unchanged, and stop when `next_offset` is absent. Listings and large exact reads continue the same way; see [continuations](retrieval.md#continuations).

## Keep answers findable

Save recurring questions as [retrieval cases](checks.md#retrieval-cases). When a useful question misses, improve the owning note or selected evidence, then check that the answer stays reachable.
