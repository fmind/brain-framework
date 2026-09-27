# Search and read

Use `bf search` to find evidence and `bf read` to open it. Both work offline and notice file edits automatically. Run commands inside your brain directory.

## Search

After the [first-decision walkthrough](getting-started.md), try:

```bash
bf search "visitors clear explanation"
bf search "product page" --scope projects
```

The first search finds the decision's reason; the second limits matches to project notes. Search matches any query word and ranks results, so `product page` can match either word. It does not generate an answer or translate your query. Use a few words the source is likely to contain.

Each item includes a title, excerpt, `ref` and brain-qualified `uri`. Read a returned ref exactly:

```bash
bf read projects/new-website.md#decision
```

The `text` field contains the original Decision section. To include the whole note and its backlinks, omit `#decision`. See [the walkthrough's output](getting-started.md#find-its-reason).

If a query misses, try the evidence's wording and remove the scope. For example, the sample says “visitors” and “signing up”; searching for “customer conversion” need not find it. Add evidence only when the source itself lacks the answer.

## Pages

Use pages when you want to browse rather than search for words:

| Command            | Use it to…                                                         |
| ------------------ | ------------------------------------------------------------------ |
| `bf read`          | See projects needing attention, recent work and collection alerts. |
| `bf read projects` | Find a project's current state and first open task.                |
| `bf read tasks`    | Summarize open tasks and read the section owning each one.         |
| `bf read actions`  | Find a session to resume.                                          |
| `bf read 7d`       | Browse the last seven days of saved activity.                      |
| `bf read memories` | Inspect sources, counts and collection coverage.                   |

For the New website project, `bf read projects` shows `"next":"Draft the product page."`. A project's `review: true` flag is a reminder, with `review_reasons` explaining the deadline or newer evidence. File modification time supplies the default age signal; an explicit `review_due` sets a deadline. Neither means the note was verified. See [review reminders](brain.md#review-reminders).

Recent-activity pages use note dates and record timestamps. They do not fetch anything from a provider. See the [page reference](retrieval.md#pages) for all available pages and their time rules.

## Summarize open tasks

`bf read tasks` returns open checkboxes from project notes, concepts and action `ACTION.md` notes, with full-selection counts in `summary` and a paginated `items` list. Each item has a source section `ref`, owning `note`, source `line` and a short `text` preview. Read that ref before acting. Deprecated notes and action attachments are excluded.

For the first-decision walkthrough, the list contains “Draft the product page.” Checking it off removes it from the open list and increases `summary.done`. Follow `next_offset` for the complete list; `problems` or `stale` also qualify the summary. A task is recorded work, not authorization to perform it. See [task-page details](retrieval.md#tasks).

## Browse tags

After adding `tags: [website, product]` to the [sample project](brain.md#notes), try:

```bash
bf read tags
bf read bf://brain/tags/website
bf search "product page" --scope bf://brain/tags/website
```

The directory gives each tag's count and ref. The tag page lists New website; the scoped search considers only notes with that exact frontmatter tag. A plain search for `website` also matches prose. See [tag authoring](brain.md#tags).

## Notes, records and identities

A note ref is a path, optionally followed by a section: `projects/new-website.md#decision`. A record ref is `SOURCE:ID`; the [sensor walkthrough](sensors.md#collect-and-read) produces `local-documents:website-demo/brief.txt`.

An explicit identity, such as a repository alias, lets you find the note that owns it and the evidence linking to it. An ordinary name does not establish an identity. Start with [Linking knowledge](links.md) before using identity scopes.

## Incomplete answers and freshness

Before concluding that evidence is absent, check the reply:

| Signal                       | What to do                                                                         |
| ---------------------------- | ---------------------------------------------------------------------------------- |
| `problems`                   | Resolve the named skipped or unreadable files and brains, then repeat the request. |
| `stale`                      | Wait for the active writer to finish, then retry the cache refresh.                |
| Source coverage or freshness | Check whether the collecting machine covered the period you need.                  |
| `next_offset`                | Continue with that offset if you need the complete listing.                        |

A clean cache does not prove current provider data. For example, no matches for today's meeting could mean the meeting was never collected. Check locally retained coverage:

```bash
bf read memories
bf status
```

These commands do not contact providers. If the cache is damaged, `bf build` reconstructs it from the files. See [exact completeness rules](retrieval.md#incomplete-answers-and-freshness) before automating absence checks.

## Continuations

When a reply contains `next_offset`, repeat the same request with that value. For example, if this first search returns `"next_offset":1`, continue with the second command:

```bash
bf search "product" --limit 1
bf search "product" --limit 1 --offset 1
```

Keep the query, scope, limit and brain selection unchanged. Stop when there is no `next_offset`; restart if the files change. Listings use the same `--offset` option.

Very large exact reads return chunks instead of a full reply. Follow the [chunk assembly and digest checks](retrieval.md#continuations) to reconstruct them; a preview or single chunk is not complete evidence.

## Retrieval cases

Save recurring questions as [retrieval cases](checks.md#retrieval-cases). When a useful question misses, improve the owning note or selected evidence and check that the answer stays reachable.
