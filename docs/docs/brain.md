---
icon: lucide/notebook-pen
description: Write project notes, reusable concepts and resumable actions with complete Markdown examples.
---

# Write notes and actions

Keep a project's current state in `projects/`, reusable knowledge in `concepts/` and a work session's stopping point in `actions/`. You write notes; sensors write records. This page extends the [Getting started](getting-started.md) example: run its commands inside your brain.

## Notes

Projects, concepts and each action's `ACTION.md` use [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md): Markdown with YAML metadata, called frontmatter, between `---` lines. `bf validate` checks their structure.

For example, expand `projects/new-website.md` with its current state and tags:

```markdown
---
type: project
status: draft
updated: 2026-09-27
tags: [website, product]
summary: Launch a product website that helps visitors understand the product.
---

# New website

Explain the product and give interested visitors a way to sign up.

## Now

The initial decision is recorded; implementation has not started.

## Decision

Start with a single product page because visitors need a clear explanation before signing up.

## Next actions

- [ ] Draft the product page.
```

| Field                           | Effect                                                                                                         |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `type`                          | Required, such as `project`, `concept` or `action`.                                                            |
| `status`                        | `draft`, `stable` (the default) or `deprecated`: the note's maturity, not work progress.                       |
| `title`                         | Result title; otherwise the first H1, then the file name.                                                      |
| `updated`                       | `YYYY-MM-DD`: the day the note's meaning last changed, which places it on period pages.                        |
| `summary`, `description`        | The note's lead in results.                                                                                    |
| `tags`                          | Exact topic labels; see [tag rules](link-reference.md#tag-rules).                                              |
| `stale_after`                   | A date-time with an offset, such as `2026-10-13T00:00:00+02:00`: the note needs review from then on.           |
| `aliases`, `entity`, `resource` | Exact identities of the note's subject; see [identities](links.md#give-a-subject-a-stable-identity).           |
| `links`                         | Link targets written as in body links, `?rel=` included, with the whole note as origin; see [links](links.md). |
| `sources`                       | OKF provenance: the note derives from each `resource` written as a [link](link-reference.md#relation-links).   |
| `fields`                        | Values of fields declared in `bf.yaml`, such as an `owner`; see [typed fields](links.md#set-typed-fields).     |

Use `draft` while an idea is under review, `stable` for reviewed knowledge and `deprecated` when it no longer applies. A deprecated note ranks last and leaves home, reminders and the task list. [Note format rules](schema.md#note-format) cover edge cases, such as invalid frontmatter.

H2 and deeper headings become separately searchable sections:

```bash
bf read projects
bf read projects/new-website.md#decision
```

The listing shows `"next":"Draft the product page."`, and the second command returns only the Decision section. Keep the current state in the note, and let Git keep old versions instead of history sections.

### Tasks

Checkboxes (`- [ ]`, `- [x]`) count as `tasks`, and the first open one becomes `next`. Check a task only when it is done. `bf read tasks` lists every open checkbox in projects, concepts and `ACTION.md` notes, with its section ref and line. It skips an action's other files, which can hold copied checkboxes; see [task rules](retrieval.md#tasks).

### Review reminders

A project needs review 14 days after its file was last edited. Any note can set its own deadline with `stale_after`:

```yaml
stale_after: 2026-10-13T00:00:00+02:00
```

From that instant on, folder listings, the home page and the note's whole read mark it `"review":true`, with `"review_reasons":["due"]` and `"review_source":"stale_after"`. `bf read` lists the projects needing review first. Other notes get reminders only with `stale_after`.

Projects and notes with `stale_after` also need review when linked evidence happened or changed upstream after their last edit (`newer_evidence`). `newer` names up to five such items, newest first: read them before relying on the note. [Getting started](getting-started.md#collect-your-first-source) shows this flag when a linked brief changes.

Editing the note clears `newer_evidence` and restarts a project's 14 days. Copying or cloning the file can do the same, because both change its modification time: set `stale_after` for a deadline that must survive. [Review signals](retrieval.md#ordering-and-review-signals) define every field.

## Tags

Add `tags: [website, product]` to group notes across folders, then browse them with `bf read tags`. [Tag rules](link-reference.md#tag-rules) owns the syntax, pages and scoped search.

## Concepts

A concept keeps knowledge that outlives one project's state. Create `concepts/explain-before-signup.md`:

```markdown
---
type: concept
status: draft
updated: 2026-09-27
sources:
  - resource: ../projects/new-website.md#decision
---

# Explain before asking for signup

Explain who a product helps and what it does before asking visitors to sign up.

## Use it

When reviewing a signup page, read only the text above the form. Can you say who the product helps and what it does?

## Limits

This is a hypothesis from the New website project. Check it with visitors before treating it as a reusable lesson.
```

```bash
bf search "explain signup" --scope concepts
bf read concepts/explain-before-signup.md
bf validate
```

The search finds the concept, and its exact read keeps both the advice and its limits. Reading the project now shows a backlink from the concept under `cites`, the relation of OKF `sources`. List the concept in `concepts/index.md` for browsing, and promote it to `stable` only after review supports it.

## Actions

An action is one session of work in its own folder, `actions/YYYY-MM-DD_topic/`, holding an `ACTION.md`. When teammates might start the same topic on the same day, add a 12-character suffix, such as `actions/2026-09-27_website-review-3f9a1c2e5b7d/`: separate folders never conflict in Git. Routines mark the actions they write with an 8-character suffix.

Create `actions/2026-09-27_website-review/ACTION.md`, using the session's date:

```markdown
---
type: action
status: draft
updated: 2026-09-27
summary: Review whether the product page explains the product before signup.
sources:
  - resource: ../../projects/new-website.md#decision
---

# Website review

Review the product page for the [New website project](../../projects/new-website.md#decision).

## Context

The project chose a single product page because visitors need a clear explanation before signing up. No draft has been reviewed yet.

## TODO

- [ ] Check whether visitors can explain the product before signing up.

## Decision

None yet: record each decision here with its date, reason and evidence ref.

## Resume

The project decision is saved. Next: read the product page draft and check whether it explains the product before the signup form.

## Outcome

Pending review.
```

```bash
bf read actions
bf read actions/2026-09-27_website-review/ACTION.md#resume
bf validate
```

The first command lists actions newest first, with open tasks, and the second returns the stopping point. After the review, replace Outcome and Resume with what actually happened, then update the project's next task. This fictional outcome records a gap without claiming a visitor test:

```markdown
## Outcome

The draft describes the product but does not name its intended audience. We have not tested it with visitors yet.

## Resume

Next: describe who the product helps, then test the explanation with a visitor.
```

Keep session evidence in the action's `inputs/` and deliverables in `outputs/`, within the brain's audience. Their Markdown files are searchable; other files, such as `inputs/request.txt`, are only listed with the action, so open them directly. Reading the action folder returns `ACTION.md`, its files, linked projects and backlinks. The headings follow the `bf-action` skill's [action template](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-action/templates/action.md); they are conventions, not requirements.

## Records

A record preserves one collected item. The [collection exercise](getting-started.md#collect-your-first-source) saves the product brief as `brief:website-brief`: the sensor name, a colon and the record's id.

| Field        | Meaning                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------- |
| `id`         | Stable per source; the ref is `SOURCE:ID`.                                                  |
| `title`      | Short, meaningful title.                                                                    |
| `text`       | The searchable body: the facts someone would ask about.                                     |
| `time`       | Event time with a timezone; places the record on period pages.                              |
| `url`        | Where the item lives upstream.                                                              |
| `links`      | Exact identities it relates to, such as `repo:github.com/owner/name`.                       |
| `aliases`    | Other exact identities of this item.                                                        |
| `fields`     | Shared fields that BF sets from the sensor's mappings; searchable, and typed for relations. |
| `attributes` | Structured details kept for exact reads, not searched.                                      |

BF stores each record as one JSON file in `memories/SOURCE/`, named by the SHA-256 of its id, and collecting an existing id updates that file. [Record revisions](schema.md#record-revisions-and-provenance) cover timestamps and provenance.

## Directory reference

`bf init PATH` creates `bf.yaml`, `AGENTS.md`, `.gitignore`, `projects/`, `concepts/`, `actions/` and `evals/retrieval.yaml`. `--full` also creates `memories/`, `assets/`, `sensors/`, `routines/` and `skills/`. Create other folders, such as `prompts/` and `channels/`, as you need them:

```text
bf.yaml                                  # name, fields, sensors, routines, assistants and watch preferences
AGENTS.md                                # instructions for agents working in the brain
projects/<project>.md                    # one OKF note per project
concepts/index.md, concepts/<concept>.md # reusable OKF knowledge
actions/YYYY-MM-DD_topic/ACTION.md       # one work session, with its inputs/ and outputs/
memories/<source>/<sha256-id>.json       # collected records, and assistant runs' replies
memories/<source>/.forgotten             # digests of records bf forget removed, never collected again
evals/*.yaml                             # retrieval cases for bf eval
sensors/, routines/, prompts/, channels/            # programs and prompt files declared in bf.yaml
hooks/                                   # agent host hooks copied from the examples
assets/                                  # images and media that notes link to
skills/                                  # agent procedures kept with the brain
logs/                                    # each program's recent output; ignored by Git
inputs/, originals/                      # imports to process and retained originals; ignored by Git
.bf/                                     # disposable search cache; ignored by Git
```

Search covers Markdown under `projects/`, `concepts/` and `actions/`, and records under `memories/`. An [assistant's](prompts.md#what-a-run-records) records hold model output, so they rank like a low-priority source. Everything else is configuration, programs and media.

Ignored by Git does not mean disposable:

- New brains also ignore `memories/` in Git; [Team setup](team.md#collect-on-a-laptop) shows how to share a reviewed source.
- Back up `memories/`, `inputs/` and `originals/`, and an assistant brain's [durable queue](channels.md#queue-and-recovery), which lives outside the brain.
- `.bf/` and `logs/` are safe to delete while BF is idle.

## Personal and team brains

Separate brains by audience: keep personal records in a private brain and share reviewed notes in a [team brain](team.md). Declare direct `brains:` paths in `bf.yaml` to search related brains together, or register a brain to select it by name. Neither runs a brain's programs; see [Configuration](configuration.md#related-brains).

With an agent, the `bf-action` skill keeps sessions resumable and the `bf-use` skill updates knowledge after an outcome; see [Agent workflows](agents.md#decision-workflows).
