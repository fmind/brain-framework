---
icon: lucide/search
description: Find saved evidence, read exact sources and check whether results are complete.
---

# Search and read

Use `bf search` to find evidence and `bf read` to open it. Run them inside your brain. Both work offline and notice note edits and new collections; after editing a record file by hand, run `bf build`.

**Search a few words → read the returned ref → check that the reply is complete.** Use [pages](#pages) to browse instead.

## Search

After [Getting started](getting-started.md), try:

```bash
bf search "visitors clear explanation"
bf search "product page" --scope projects
```

The first search finds the decision's reason, and the second searches project notes only. Any query word can match; results matching more words rank higher. BF does not translate or expand words, so put variants in one query, such as `signup "sign up" registration`.

| Write                                 | To match                                                              |
| ------------------------------------- | --------------------------------------------------------------------- |
| `visitors signup`                     | Either word, in any form English stemming relates, such as `visitor`. |
| `"clear explanation"`                 | The exact phrase.                                                     |
| `synchro*`                            | Words starting with `synchro`, such as `synchronizes`.                |
| `repo:github.com/example/new-website` | The identity's owner and every item linking to it.                    |

Each item has a `title`, an `excerpt` and a `ref`. A note states its `date`, and a record its event `time`. `unmatched` names query words found nowhere, so you can rephrase, and `suggestions` offers close words for a misspelling:

```bash
bf search '"clear explanation" zebra'
bf search 'explenation'
```

The first reply returns the Decision section and `"unmatched":["zebra"]`. The second finds nothing and returns `"unmatched":["explenation"]` with `"suggestions":{"explenation":["explanation"]}`: search the suggested word to confirm. A note appears once, with other matching `sections`; records sharing a URL appear once, with the other refs in `also`. [Search rules](retrieval.md#search) explain ranking.

If a query misses, use the evidence's own words and drop the scope. The sample says “visitors” and “signing up”, so “customer conversion” need not find it. Search also drops function words such as `the` unless quoted, folds accents only in Latin script and matches some scripts by fragments; see [other scripts](retrieval.md#search).

### Narrow a search

`--scope` takes one scope: a folder or note such as `projects`, a source such as `memories/brief`, a period such as `7d` or `2026-09-21..2026-09-25`, an identity or a tag address such as `bf://brain/tags/website` (not `tags/website`). A period, a space, then one other scope narrows it in time, as in `bf search "launch" --scope "7d repo:github.com/example/new-website"`. [Scopes](retrieval.md#scopes) list every form.

## Read a ref

```bash
bf read projects/new-website.md#decision
```

The `text` field holds the original Decision section; omit `#decision` to read the whole note with its backlinks. Use returned refs exactly:

| Read     | Example                                       | Result                                                                                                                    |
| -------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Note     | `bf read projects/new-website.md`             | Whole note, backlinks, the claims it makes and, for a project or a note with `stale_after`, its tasks and review signals. |
| Section  | `bf read projects/new-website.md#decision`    | That section, with its note's title, type, status and date.                                                               |
| Record   | `bf read brief:website-brief`                 | The [collected brief](getting-started.md#collect-your-first-source).                                                      |
| Identity | `bf read repo:github.com/example/new-website` | Its owning note, after [declaring the alias](links.md#give-a-subject-a-stable-identity).                                  |
| Relation | `bf read projects/new-website.md --rel cites` | Every item citing the note: a [relation page](retrieval.md#relation-pages).                                               |

A whole read previews the five newest backlinks of each relation. A large note returns its text in pages, with an `outline` of section refs when headings divide it: read the section you need, or follow `next_offset` ([large exact reads](retrieval.md#large-exact-reads)).

With several brains selected, each result also names its `brain` and a `uri` such as `bf://brain/projects/new-website.md#decision`. Read the `uri`: a plain ref that exists in two brains fails.

## Pages

Pages are computed views for browsing; they use saved dates and never fetch anything. `bf read` alone opens the home page: projects needing review first, recent work and failing programs. `bf read projects`, `tasks`, `actions`, `7d`, `memories` and `tags` list the rest; [pages](retrieval.md#pages) lists every page and its rules.

For the New website project, `bf read projects` shows `"next":"Draft the product page."` and `bf read tasks` lists that checkbox with its `ref` and `line`. A project with `"review":true` needs attention: `review_reasons` says why, and `newer` names linked evidence to read first; see [review reminders](brain.md#review-reminders).

## Check that a reply is complete

Before concluding that evidence is absent, look for `problems`, `stale`, sources that failed, are `overdue`, `never` succeeded or fell silent, and `next_offset`: [incomplete answers](retrieval.md#incomplete-answers-and-freshness) says what each means and what to do. No match for today's meeting can mean the meeting was never collected.

## Continue a listing

When a reply contains `next_offset`, repeat the request with that value:

```bash
bf search "project" --limit 1
bf search "project" --limit 1 --offset 1
```

The first reply returns `concepts/welcome.md` with `"next_offset":1`. The second returns `projects/new-website.md` without `next_offset`, which ends the listing. Keep the query, scope, limit and brain selection unchanged. Listings and large exact reads continue the same way; see [continuations](retrieval.md#continuations).

## Keep answers findable

Save recurring questions as [retrieval cases](checks.md#retrieval-cases). When a useful question misses, improve the owning note or selected evidence, then check that the answer stays reachable.
