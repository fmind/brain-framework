---
description: Write project notes, reusable concepts and resumable actions with complete Markdown examples.
---

# Write notes and actions

Keep a project's current state in `projects/`, reusable knowledge in `concepts/` and a work session's stopping point in `actions/`. This page extends the [Getting started](getting-started.md) example. Run commands inside your brain; you write notes, and sensors write records.

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

| Field                           | Effect                                                                                                          |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `type`                          | Required, such as `project`, `concept` or `action`.                                                             |
| `status`                        | `draft`, `stable` or `deprecated`; omitted means `stable`. It describes the note's maturity, not work progress. |
| `title`                         | Result title; otherwise the first H1, then the file name.                                                       |
| `updated`                       | `YYYY-MM-DD`: the day the note's meaning last changed. It places the note on period pages.                      |
| `summary`, `description`        | The note's lead in results.                                                                                     |
| `tags`                          | Exact topic labels; see [tag rules](link-reference.md#tag-rules).                                               |
| `stale_after`                   | A date-time with an offset, such as `2026-10-13T00:00:00+02:00`: the note needs review from then on.            |
| `aliases`, `entity`, `resource` | Exact identities of the note's subject; see [identities](links.md#give-a-subject-a-stable-identity).            |
| `sources`                       | OKF provenance: each `resource` is a note, file, record or URL the note derives from.                           |
| `fields`                        | Values of fields declared in `bf.yaml`, such as an `owner`; see [typed fields](links.md#set-typed-fields).      |

Use `draft` while an idea is under review, `stable` for reviewed knowledge and `deprecated` when it no longer applies. A deprecated note ranks last and leaves home, reminders and the task list. [Note format rules](schema.md#note-format) cover edge cases such as invalid frontmatter.

H2 and deeper headings become separately searchable sections:

```bash
bf read projects
bf read projects/new-website.md#decision
```

The listing shows `"next":"Draft the product page."`; the second command returns only the Decision section. Keep the current state in the note. Let Git keep old versions instead of appending history sections.

### Tasks

Checkboxes (`- [ ]`, `- [x]`) count as `tasks`; the first open one becomes `next`. `bf read tasks` lists every open checkbox in projects, concepts and `ACTION.md` notes, with its section ref and line. An action's other files are excluded, since they can hold copied checkboxes. Check a task only when it is done.

### Review reminders

A project needs review 14 days after its file was last edited. Any note can set its own deadline with `stale_after`:

```yaml
stale_after: 2026-10-13T00:00:00+02:00
```

From that instant on, folder listings, the home page and the note's whole read mark it `"review":true`, with `"review_reasons":["due"]` and `"review_source":"stale_after"`. Other notes get reminders only with `stale_after`. `bf read` lists the projects needing review first.

A project, or a note with `stale_after`, also needs review when linked evidence is newer than its last edit (`newer_evidence`): an item linking to it, or a record it links to, happened or changed upstream since. `newer` names up to five of them, newest first: read them before relying on the note. [Getting started](getting-started.md#collect-your-first-source) shows this flag when a linked brief changes.

An edit clears `newer_evidence` until newer evidence arrives, and restarts a project's 14 days. Copying or cloning the file can do the same, since both follow its modification time: set `stale_after` for a deadline that must survive. See [review signals](retrieval.md#ordering-and-review-signals).

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

The search finds the concept, and its exact read keeps both the advice and its limits. Reading the project now shows a backlink from the concept under `cites`, the relation of OKF `sources`. Add the concept to `concepts/index.md` to make it easy to browse. Promote it to `stable` only after review supports it.

## Actions

An action is one session of work in its own folder, `actions/YYYY-MM-DD_topic/`, holding an `ACTION.md`. Add a 12-character suffix, such as `actions/2026-09-27_website-review-3f9a1c2e5b7d/`, when teammates might start the same topic on the same day: separate folders never conflict in Git. Routines mark the actions they write with an 8-character suffix.

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

## Objective

Review the product page for the [New website project](../../projects/new-website.md#decision).

## Tasks

- [ ] Check whether visitors can explain the product before signing up.

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

The first command lists actions newest first, with open tasks; the second returns the stopping point. After the review, replace Outcome and Resume with what actually happened, then update the project's next task. For example, this fictional outcome records a gap without claiming a visitor test:

```markdown
## Outcome

The draft describes the product but does not name its intended audience. We have not tested it with visitors yet.

## Resume

Next: describe who the product helps, then test the explanation with a visitor.
```

Keep session evidence in the action's `inputs/` and deliverables in `outputs/`; both are searchable, so keep them within the brain's audience. Reading the action folder returns `ACTION.md`, its files, linked projects and backlinks. Objective, Tasks, Resume and Outcome are useful headings, not required ones. The [action template](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/templates/action.md) adds context and decision sections for longer sessions.

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

BF stores each record as one JSON file in `memories/SOURCE/`, named by the SHA-256 of its id. Collecting an existing id updates that file. See [record revisions](schema.md#record-revisions-and-provenance) for timestamps and provenance.

## Directory reference

`bf init PATH` creates `bf.yaml`, `AGENTS.md`, `.gitignore`, `projects/`, `concepts/`, `actions/` and `evals/retrieval.yaml`. `--full` also creates `memories/`, `assets/`, `sensors/`, `routines/` and `skills/`. Create other folders as you need them:

```text
bf.yaml                                  # name, fields, sensors, routines and watch preferences
AGENTS.md                                # instructions for agents working in the brain
CLAUDE.md                                # optional: only @AGENTS.md, so Claude Code loads it
projects/<project>.md                    # one OKF note per project
concepts/index.md, concepts/<concept>.md # reusable OKF knowledge
actions/YYYY-MM-DD_topic/ACTION.md       # one work session, with its inputs/ and outputs/
memories/<source>/<sha256-id>.json       # collected records
evals/*.yaml                             # retrieval cases for bf eval
sensors/, routines/                      # programs declared in bf.yaml
hooks/                                   # agent host hooks copied from the examples
assets/                                  # images and media that notes link to
skills/                                  # agent procedures kept with the brain
logs/                                    # each program's recent output; ignored by Git
inputs/, originals/                      # imports to process and retained originals; ignored by Git
.bf/                                     # disposable search cache; ignored by Git
```

Search covers Markdown under `projects/`, `concepts/` and `actions/`, and records under `memories/`. Everything else is configuration, programs and media. New brains also ignore `memories/` in Git; [Team setup](team.md#collect-on-a-laptop) shows how to share a reviewed source. Back up `memories/`, `inputs/` and `originals/`: ignored does not mean disposable. `.bf/` and `logs/` are safe to delete while BF is idle.

## Personal and team brains

Separate brains by audience: keep personal records in a private brain and share reviewed notes in a [team brain](team.md). Declare direct `brains:` paths in `bf.yaml` to search related brains together, and register a brain to select it by name from elsewhere; see [Configuration](configuration.md#related-brains). Neither runs a brain's programs.

With an agent, the `bf-use` skill keeps sessions resumable and updates knowledge after an outcome; see [Agent workflows](agents.md#decision-workflows).
