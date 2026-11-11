---
icon: lucide/search
description: Find saved evidence, read exact sources and check whether results are complete.
---

# Search and read

Run `bf search` to locate evidence and `bf read` to open exact refs or browse computed pages. Both run offline against `.bf/` and pick up note edits and collections automatically (run `bf build` only after manual edits inside `memories/`).

## Search

```bash
bf search "visitors clear explanation"
bf search "product page" --scope projects
```

Any word matches and passages matching more terms rank higher, so include synonyms or translations in one query (`signup "sign up" registration`):

| Query                                 | Matches                                                       |
| ------------------------------------- | ------------------------------------------------------------- |
| `visitors signup`                     | Either word via English stemming (`visitor`).                 |
| `"clear explanation"`                 | Exact phrase (also keeps quoted function words like `"son"`). |
| `synchro*`                            | Prefix expansion (`synchronizes`).                            |
| `repo:github.com/example/new-website` | The identity's owning note plus every item linking to it.     |

Replies include `unmatched` terms and spelling `suggestions` (`bf search 'explenation'` suggests `explanation`), deduplicate sections per note (`sections`) and shared URLs (`also`), and follow [ranking rules](retrieval.md#search).

Narrow with one `--scope`: a folder or note (`projects`), source (`memories/brief`), period (`7d`, `2026-09-21..2026-09-25`), identity, tag URI (`bf://brain/tags/website`), or a period plus one scope (`"7d repo:github.com/example/new-website"`; [scopes](retrieval.md#scopes)).

## Read a ref

```bash
bf read projects/new-website.md#decision
```

| Target   | Example                                       | Returns                                                                                                |
| -------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Note     | `bf read projects/new-website.md`             | Full note, `sha256`, `backlinks`, `claims`, plus tasks and review flags.                               |
| Section  | `bf read projects/new-website.md#decision`    | That section's text with note `title`, `type`, `status`, `date`, `sha256`.                             |
| Record   | `bf read brief:website-brief`                 | Full `record` payload, `collection` coverage, `backlinks` and `claims`.                                |
| Identity | `bf read repo:github.com/example/new-website` | Owning note or record, else a backlink page ([identities](links.md#give-a-subject-a-stable-identity)). |
| Relation | `bf read projects/new-website.md --rel cites` | Paginated [relation page](retrieval.md#relation-pages) of incoming links.                              |

Notes above 32 KiB page their text and include a section `outline` ([large exact reads](retrieval.md#large-exact-reads)). With multiple selected brains, pass the returned `uri` (`bf://brain/...`).

## Pages

Computed pages browse saved state offline ([all pages](retrieval.md#pages)):

- `bf read` (home): projects needing review first, open `draft` `actions`, `changed`, `activity`, `upcoming` and uniform `attention`.
- `bf read review`: triage queue of `due` and `newer_evidence` notes, open `draft` `actions` (with `blocked_by`) and `attention` ([review reminders](brain.md#review-reminders)).
- `bf read projects`, `concepts`, `actions`, `tasks`, `7d`, `memories` and `tags`.

## Completeness and continuations

Inspect `problems`, `stale`, source coverage (`overdue`, `never`, `failed`, `silent`, `degraded`) and `next_offset` before concluding anything is absent ([incomplete answers](retrieval.md#incomplete-answers-and-freshness)). When `next_offset` is present, repeat the exact command with `--offset N` until absent ([continuations](retrieval.md#continuations)):

```bash
bf search "project" --limit 1
bf search "project" --limit 1 --offset 1
```

Pin recurring questions in `evals/` so answers stay reachable ([retrieval cases](checks.md#retrieval-cases)).
