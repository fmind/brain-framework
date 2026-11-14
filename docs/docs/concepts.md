---
icon: lucide/lightbulb
description: Understand projects, records, concepts and actions, the flow from evidence to decisions, and the terms BF uses.
---

# Core concepts

A **brain** is a plain folder of notes and collected evidence that you own. It runs one loop — **gather → normalize → organize and connect → act → learn** — across four knowledge layers and one unified `jobs:` configuration:

| Layer or job kind   | Role                                                                                                                | Location / `emit:`           |
| ------------------- | ------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| **Project**         | Goals, live state (`Now`), decisions (`Decision`) and open tasks (`Next actions`).                                  | `projects/`                  |
| **Concept**         | Reusable patterns and syntheses promoted once outcomes confirm them.                                                | `concepts/`                  |
| **Action**          | One tracked work session (`draft` open, `stable` settled, `deprecated` retired).                                    | `actions/`                   |
| **Record**          | Collected evidence (`SOURCE:ID`) with event `time`, `updated`, `observed`, `@type`, `relations` and `attributes`.   | `memories/`                  |
| **Record job**      | Script (`run:`) or JSON mapper (`map:`) collecting records (`bf collect`, `bf update`, `bf watch`).                 | `emit: records`              |
| **Prompt / script** | Deterministic script (`run:`) or agent harness (`prompt:`) triggered manually, on schedule, hooks or events.        | `emit: log\|action\|message` |
| **Message job**     | Polling or `daemon` stream (`emit: messages`) and outbound `send:` jobs delivering replies (`bf post`, `bf queue`). | `emit: messages` / `send:`   |

## What stays authoritative

- **Files** are the source of truth: OKF Markdown notes ([notes](brain.md#notes)) and SHA-256-keyed JSON records ([records](brain.md#records)).
- **`.bf/`** is a disposable SQLite search and graph cache rebuilt automatically from the files.
- **Provider tools** own live state; records preserve what was collected and when.
- **`evals/`** suites verify recurring questions still retrieve expected refs ([checks](checks.md)).
- **Skills and MCP** connect agents: skills teach procedures, and `bf mcp` exposes read-only `search` and `read` ([agents](agents.md), [MCP](mcp.md)).

## What BF does not do

- **Never runs a model.** Search uses BM25 word and exact-identity matching without embeddings (`bf export --kind items` feeds external retrievers); [prompt jobs](prompts.md) launch your harness.
- **Never infers links or identities.** Similar names or prose never merge subjects; aliases, links and `relations:` are explicit ([links](links.md)).
- **Never verifies truth or collects during retrieval.** `bf search` and `bf read` stay offline; check source coverage before treating cached records as current.
- **Never proves absence from incomplete replies.** `problems`, `stale`, unfollowed `next_offset` pages or `overdue`/`failed`/`silent`/`degraded` sources leave absence unproven ([incomplete answers](retrieval.md#incomplete-answers-and-freshness)).
- **Never executes unasked or sandboxes code.** Only explicit execution commands (`bf collect`, `bf run`, `bf post`, `bf wake`, delivery `bf queue ID --retry`, `bf update`, `bf watch`) run configured jobs, on one brain, with your user permissions. Keep backups of your brain and runtime queue.

## Glossary

| Term             | Meaning                                                                                                                                     | Reference                                                              |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Action           | Tracked session at `actions/YYYY-MM-DD_topic/ACTION.md` (`bf action new`, `bf action status`).                                              | [Actions](brain.md#actions)                                            |
| Alias / Entity   | Exact `scheme:value` identity or owned `bf://` subject declared on a note.                                                                  | [Identities](links.md#give-a-subject-a-stable-identity)                |
| Backlink / Claim | Incoming link grouped by relation at its target; outgoing typed assertion keeping its `origin`.                                             | [Relation links](link-reference.md#relation-links)                     |
| Broader relation | Parent relation whose `--rel` page includes narrower relations (`organizer` → `participant`).                                               | [Narrower relations](schema.md#narrower-relations-and-allowed-targets) |
| Coverage         | Contiguous windows and health recorded on this machine for a source.                                                                        | [Source coverage](retrieval.md#source-coverage)                        |
| Field / Ontology | Optional `description`, `broader` or `targets` constraint under `fields:` in `bf.yaml`.                                                     | [Ontology](schema.md#shared-fields-and-sensor-mappings)                |
| Freshness        | `fresh`, `overdue`, `never`, `manual` or `unknown` on this machine.                                                                         | [Timing and health](schedule.md#timing-and-health)                     |
| Gate / `on:`     | Custom `gate:` script (exit 0 to proceed, nonzero to skip as `gated`) or `on: { event: ... }` rule filtering runs before calling a harness. | [Gates](prompts.md#gates)                                              |
| Harness          | External agent CLI (`claude`, `codex`, etc.) started by a `prompt:` job with configured `harness` (or `defaults.harness`).                  | [Prompts](prompts.md#how-the-harness-receives-the-prompt)              |
| OKF              | Open Knowledge Format v0.2 frontmatter (`type`, `status: draft\|stable\|deprecated`, `updated`, `sources`).                                 | [Notes](brain.md#notes)                                                |
| Queue            | Durable SQLite store in `.bf/runtime.sqlite3` tracking job state, active runs, events, wakes and deliveries (`bf queue`).                   | [Queue and recovery](channels.md#queue-and-recovery)                   |
| Reprojection     | `bf build --reproject JOB`: re-checking stored records against `bf.yaml` relation constraints offline.                                      | [Reproject](schema.md#reproject-stored-records)                        |
| Review page      | `bf read review`: notes with `due` or `newer_evidence`, open `draft` actions (`blocked_by`) and `attention`.                                | [Retrieval](retrieval.md#pages)                                        |
| Stale            | Search/read reply served while a writer held the cache lock; in `bf status`, `"cache":"stale"` marks an unrecovered write.                  | [Incomplete answers](retrieval.md#incomplete-answers-and-freshness)    |
| Wake             | Bounded per-job follow-up scheduled via `bf wake` (`--in` / `--at`).                                                                        | [Wakes](prompts.md#wakes)                                              |
