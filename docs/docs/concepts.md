---
description: Understand projects, records, concepts and actions, the flow from evidence to decisions, and the terms BF uses.
---

# Core concepts

A **brain** is a folder of notes and collected evidence that you own. People and agents use it to remember why decisions were made, find current work and carry context into the next session.

## Four kinds of knowledge

| Kind    | What belongs here                                                | Example                              | Location    |
| ------- | ---------------------------------------------------------------- | ------------------------------------ | ----------- |
| Project | Goals, current state, decisions and next tasks.                  | “Build one page; draft it next.”     | `projects/` |
| Record  | What a selected source said, with its identity and provenance.   | A collected product brief.           | `memories/` |
| Concept | Knowledge reusable beyond one project, with evidence and limits. | “Explain before asking for signup.”  | `concepts/` |
| Action  | One work session's objective, results and next step.             | A website review to resume tomorrow. | `actions/`  |

Projects, concepts and action `ACTION.md` notes are Markdown with YAML metadata in the [OKF](brain.md#notes) format. Records are JSON files, one per collected item. A record shows what a source said, not that it is true.

## From source to decision

**Gather → normalize → organize and connect → act → learn.**

| Step                 | What happens                                                                                             | Who does it                                   |
| -------------------- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Gather               | A sensor reads a selected brief and prints a record.                                                     | A small program you configure.                |
| Normalize            | A field mapping adds a shared field such as `kind: document`.                                            | BF, during collection.                        |
| Organize and connect | A project links its decision to the brief.                                                               | You or your agent; BF indexes explicit links. |
| Act                  | Review the page and record the result in an action.                                                      | You or your agent, with authorized tools.     |
| Learn                | Update the project's conclusion, also when BF flags newer evidence; keep a reusable lesson as a concept. | You or your agent, after the outcome.         |

[Getting started](getting-started.md) runs the first three steps with one local file, then shows BF flagging the project when that file changes.

## Programs

A brain can hold two kinds of programs, both declared in `bf.yaml` and reviewed by you:

| Program | Purpose                                                                         | Runs when                                               |
| ------- | ------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Sensor  | Print selected evidence as records; its records form a **source**.              | `bf collect`, or `bf update` and `bf watch` when due.   |
| Routine | Any other deterministic brain program, such as a weekly review or a validation. | `bf run`, a hook such as Git's pre-commit, or when due. |

Each program keeps its recent output and errors in `logs/NAME.log`. [Sensors](sensors.md), [routines](routines.md) and [watch and schedule](schedule.md) explain their setup.

## Retrieval and connections

| Concept          | Plain meaning                                                   | Example                                             |
| ---------------- | --------------------------------------------------------------- | --------------------------------------------------- |
| Ref              | An exact address to read.                                       | `projects/new-website.md#decision`                  |
| Page             | A computed view of saved files.                                 | `bf read tasks` lists open checkboxes.              |
| Scope            | Where a search looks.                                           | `bf search "explanation" --scope projects`          |
| Identity         | An exact name for a subject, such as a declared alias.          | `repo:github.com/example/new-website`               |
| Link / backlink  | An outgoing reference and the incoming view at its destination. | A concept cites a project decision.                 |
| Relation / claim | A link's declared meaning, and the note or record asserting it. | An action `depends-on` a decision.                  |
| Knowledge graph  | All these explicit connections together.                        | Read a person to see records naming them as author. |

[Linking knowledge](links.md) shows how to add connections.

## What stays authoritative

- **Files** are the source of truth: notes you write and records sensors collect.
- **The cache** in `.bf/` is disposable SQLite data, rebuilt from the files.
- **Provider tools** stay authoritative for their own items; records keep what was collected and when.
- **Retrieval cases** in `evals/` check that useful answers stay findable.
- **Skills** are procedures an agent follows; **MCP** lets an agent host call BF's `search` and `read`.
- **Related brains** are local folders declared under `brains:`; retrieval includes them, and nothing downloads or runs them.

## What BF does not do

These boundaries hold everywhere; other pages link here instead of repeating them.

- **It runs no model.** Search matches words and exact identities, without embeddings, translation or generated answers.
- **It infers nothing.** Similar names never establish an identity, and prose never creates a relation. Links, aliases and field mappings are explicit.
- **It does not verify truth.** Validation checks structure and links; a passing retrieval case checks the chosen evidence. A record shows what its source said.
- **Retrieval never collects.** `bf search` and `bf read` never contact providers or run programs. A fresh cache can hold old evidence: check source coverage before calling evidence current.
- **An incomplete result proves no absence.** A reply with `problems` or `stale`, a failed source or a missing period cannot show that something does not exist. One page of results is not the whole result.
- **Reminders are not reviews.** A `review` flag comes from dates, file edits and linked evidence. It never marks a note as checked.
- **Programs run only when you ask.** Sensors and routines run through `bf collect`, `bf run`, `bf update`, `bf watch` or a schedule you install, in one selected brain. Registration and references never grant execution.
- **It is not a sandbox or a backup.** Programs run with your account's permissions; keep backups of your brain.

## Glossary

| Term             | Meaning                                                                                                                                                                                                                                        | Read more                                                              |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Action           | One session of work in `actions/YYYY-MM-DD_topic/ACTION.md`, with its inputs, outcome and next step.                                                                                                                                           | [Actions](brain.md#actions)                                            |
| Alias            | Another exact identity declared for the same subject, such as `repo:github.com/example/new-website`.                                                                                                                                           | [Identities](links.md#give-a-subject-a-stable-identity)                |
| Backlink         | An incoming link, grouped by relation at its destination.                                                                                                                                                                                      | [Connect two notes](links.md#connect-two-notes)                        |
| Broader relation | A relation whose relation page also lists narrower ones, such as `participant` for `organizer`.                                                                                                                                                | [Narrower relations](schema.md#narrower-relations-and-allowed-targets) |
| Cites            | The built-in relation of OKF `sources`: the note derives from its target.                                                                                                                                                                      | [Relation links](link-reference.md#relation-links)                     |
| Claim            | A relation plus the note or record asserting it, such as a commit crediting Alice as `author`.                                                                                                                                                 | [Relation links](link-reference.md#relation-links)                     |
| Coverage         | The collection windows this machine's run history retains for a source.                                                                                                                                                                        | [Backfills and coverage](sensors.md#backfills-and-coverage)            |
| Entity           | The subject a note explicitly represents, with its own `bf://` identity.                                                                                                                                                                       | [Identities](links.md#give-a-subject-a-stable-identity)                |
| Field            | A shared value declared under `fields:` in `bf.yaml`, set by sensor mappings or a note's `fields:`.                                                                                                                                            | [Fields](schema.md#shared-fields-and-sensor-mappings)                  |
| Freshness        | Whether a program succeeded recently on this machine, a sensor by a collection reaching the present: `fresh` (within twice its `refresh`), `overdue`, `never`, `manual` (`refresh: 0`) or `unknown` (a disabled program or historical source). | [Timing and health](schedule.md#timing-and-health)                     |
| Hook             | An event, such as Git's `pre-commit`, that runs the routines listing it through `bf run --hook`.                                                                                                                                               | [Hooks](routines.md#run-routines-from-hooks)                           |
| OKF              | Open Knowledge Format v0.2: Markdown and metadata conventions for projects, concepts and `ACTION.md`.                                                                                                                                          | [Notes](brain.md#notes)                                                |
| Overdue          | A scheduled program with no success on this machine within twice its `refresh`; one that never succeeded is `never`.                                                                                                                           | [Status](commands.md#status-sources-and-routines)                      |
| Relation         | The declared meaning of a link or field, such as `depends-on` or `author`.                                                                                                                                                                     | [Name a relation](links.md#name-a-relation)                            |
| Relation page    | Every item linking to a note, record or identity through one relation: `bf read REF --rel RELATION`.                                                                                                                                           | [Relation pages](retrieval.md#relation-pages)                          |
| Reprojection     | Applying a sensor's current mappings to the records it already collected, without running it.                                                                                                                                                  | [Reproject](schema.md#reproject-stored-records)                        |
| Retrieval case   | A saved question and the evidence that search or read must return.                                                                                                                                                                             | [Retrieval cases](checks.md#retrieval-cases)                           |
| Review flag      | `"review":true` on a project, or a note with `stale_after`: its review is due, or linked evidence is newer than its last edit; `newer` names that evidence.                                                                                    | [Review reminders](brain.md#review-reminders)                          |
| Source           | The records of one sensor, stored in `memories/SENSOR/`. Not to be confused with a note's OKF `sources` (see Cites).                                                                                                                           | [Records](brain.md#records)                                            |
| Stale            | A search or read reply served while another writer updates the cache; retry for newer results. In `bf status`, `"cache":"stale"` instead means an interrupted write awaits recovery.                                                           | [Incomplete answers](search.md#incomplete-answers-and-freshness)       |
| Tag              | An exact topic label in a note's `tags`; never an identity or a dependency.                                                                                                                                                                    | [Tag rules](link-reference.md#tag-rules)                               |
