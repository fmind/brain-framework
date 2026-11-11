---
icon: lucide/lightbulb
description: Understand projects, records, concepts and actions, the flow from evidence to decisions, and the terms BF uses.
---

# Core concepts

A **brain** is a plain folder of notes and collected evidence that you own. It runs one loop — **gather → normalize → organize and connect → act → learn** — across four knowledge layers and four reviewed program kinds:

| Layer or program | Role                                                                                                     | Location / config |
| ---------------- | -------------------------------------------------------------------------------------------------------- | ----------------- |
| **Project**      | Goals, live state (`Now`), decisions (`Decision`) and open tasks (`Next actions`).                       | `projects/`       |
| **Concept**      | Reusable patterns and syntheses promoted once outcomes confirm them.                                     | `concepts/`       |
| **Action**       | One tracked work session (`draft` open, `stable` settled, `deprecated` retired).                         | `actions/`        |
| **Record**       | Collected evidence (`SOURCE:ID`) with event `time`, `updated`, `observed` and mapped `fields`.           | `memories/`       |
| **Sensor**       | Stdlib script printing JSON/JSONL records from a tool (`bf collect`, `bf update`, `bf watch`).           | `sensors:`        |
| **Routine**      | Deterministic script run on `refresh`, wall-clock `at`, `when:` or Git `hooks` (`bf run`).               | `routines:`       |
| **Assistant**    | Agent harness invoked over `prompts/NAME.md` on channel `inputs`, `schedules` (`when:`/`gate`) or wakes. | `assistants:`     |
| **Channel**      | Adapter sending, batch-reading or streaming conversation envelopes for assistants (`bf channel`).        | `channels:`       |

## What stays authoritative

- **Files** are the source of truth: OKF Markdown notes ([notes](brain.md#notes)) and SHA-256-keyed JSON records ([records](brain.md#records)).
- **`.bf/`** is a disposable SQLite search and graph cache rebuilt automatically from the files.
- **Provider tools** own live state; records preserve what was collected and when.
- **`evals/`** suites verify recurring questions still retrieve expected refs ([checks](checks.md)).
- **Skills and MCP** connect agents: skills teach procedures, and `bf mcp` exposes read-only `search` and `read` ([agents](agents.md), [MCP](mcp.md)).
- **`brains:`** adds one-hop local folders to retrieval without downloading or executing anything ([configuration](configuration.md#related-brains)).

## What BF does not do

- **Never runs a model.** Search uses BM25 word and exact-identity matching without embeddings (`bf export --kind items` feeds external retrievers); [assistants](prompts.md) launch your harness.
- **Never infers links or identities.** Similar names or prose never merge subjects; aliases, links and `fields:` mappings are explicit ([links](links.md)).
- **Never verifies truth or collects during retrieval.** `bf search` and `bf read` stay offline; check source coverage before treating cached records as current.
- **Never proves absence from incomplete replies.** `problems`, `stale`, unfollowed `next_offset` pages or `overdue`/`failed`/`silent`/`degraded` sources leave absence unproven ([incomplete answers](retrieval.md#incomplete-answers-and-freshness)).
- **Never executes unasked or sandboxes code.** Only explicit execution commands (`bf collect`, `bf run`, `bf channel`, delivery `bf queue ID --retry`, `bf update`, `bf watch`) run configured programs, on one brain, with your user permissions. Keep backups of your brain and runtime queue.

## Glossary

| Term             | Meaning                                                                                                                                      | Reference                                                              |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Action           | Tracked session at `actions/YYYY-MM-DD_topic/ACTION.md` (`bf action new`, `bf action status`).                                               | [Actions](brain.md#actions)                                            |
| Alias / Entity   | Exact `scheme:value` identity or owned `bf://` subject declared on a note.                                                                   | [Identities](links.md#give-a-subject-a-stable-identity)                |
| Backlink / Claim | Incoming link grouped by relation at its target; outgoing typed assertion keeping its `origin`.                                              | [Relation links](link-reference.md#relation-links)                     |
| Broader relation | Parent relation whose `--rel` page includes narrower relations (`organizer` → `participant`).                                                | [Narrower relations](schema.md#narrower-relations-and-allowed-targets) |
| Coverage         | Contiguous windows and health recorded on this machine for a source.                                                                         | [Source coverage](retrieval.md#source-coverage)                        |
| Field / Ontology | Typed property or `relation: true` declared under `fields:` in `bf.yaml`.                                                                    | [Ontology](schema.md#shared-fields-and-sensor-mappings)                |
| Freshness        | `fresh`, `overdue`, `never`, `manual` or `unknown` on this machine.                                                                          | [Timing and health](schedule.md#timing-and-health)                     |
| Gate / `when:`   | Declarative `when:` rule (`sources`, `review`, `actions`, `attention`) or custom `gate` script filtering scheduled runs before any LLM call. | [Gates](prompts.md#gates)                                              |
| Harness          | External agent CLI (`claude`, `codex`, etc.) started by an assistant with configured `command` and trigger args.                             | [Assistants](prompts.md#how-the-harness-receives-the-prompt)           |
| OKF              | Open Knowledge Format v0.2 frontmatter (`type`, `status: draft\|stable\|deprecated`, `updated`, `sources`).                                  | [Notes](brain.md#notes)                                                |
| Propose          | `propose: true` on an assistant, allowing validated `status: draft` `ACTION.md` proposals in structured replies.                             | [Propose, then act](prompts.md#propose-then-act)                       |
| Queue            | Durable SQLite store outside the brain tracking assistant jobs, wakes and channel deliveries (`bf queue`).                                   | [Queue and recovery](channels.md#queue-and-recovery)                   |
| Reprojection     | `bf build --reproject SENSOR`: re-applying `fields:` mappings to stored records offline.                                                     | [Reproject](schema.md#reproject-stored-records)                        |
| Review page      | `bf read review`: notes with `due` or `newer_evidence`, open `draft` actions (`blocked_by`) and `attention`.                                 | [Retrieval](retrieval.md#pages)                                        |
| Stale            | Search/read reply served while a writer held the cache lock; in `bf status`, `"cache":"stale"` marks an unrecovered write.                   | [Incomplete answers](retrieval.md#incomplete-answers-and-freshness)    |
| Wake             | Per-slot follow-up timer (`wake:{assistant}:{slot}`) requested in a structured reply.                                                        | [Wakes](prompts.md#wakes)                                              |
