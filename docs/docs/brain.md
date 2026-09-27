# Files and notes

<span id="brain-layout-and-knowledge"></span>

Keep a project's current state in `projects/`, reusable knowledge in `concepts/` and a session's stopping point in `actions/`. This page builds on the [New website walkthrough](getting-started.md) with complete examples you can adapt.

A brain is an ordinary directory, usually a private Git repository. Work inside it or select it with `--brain PATH`. You write notes with your editor or agent; sensors own collected records.

## Notes

Projects, concepts and each action's `ACTION.md` use [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md): Markdown with YAML metadata between `---` lines, called frontmatter. They need a nonempty `type`. `bf validate` checks their structure.

For example, expand `projects/new-website.md` to include its current state and tags:

```markdown
---
type: project
status: draft
updated: 2026-09-27
tags: [website, product]
aliases: [repo:github.com/team/new-website]
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

| Field                      | Effect                                                                                               |
| -------------------------- | ---------------------------------------------------------------------------------------------------- |
| `title`                    | Result title; otherwise the first H1, then the file name.                                            |
| `type`                     | Required for projects, concepts and `ACTION.md`; typically `project`, `concept` or `action`.         |
| `status`                   | `draft`, `stable` or `deprecated`; omitted means stable. Describes document maturity.                |
| `updated`                  | `YYYY-MM-DD`; used for period pages and project review reminders.                                    |
| `summary`, `description`   | The note's lead in results.                                                                          |
| `tags`, `aliases`, `links` | Searchable; `aliases` are exact identities that resolve to the note; `links` add explicit relations. |

Use `draft` while reviewing an idea, `stable` for reviewed knowledge and `deprecated` when it is no longer current. Put work progress in task lists:

```bash
bf read projects
bf read projects/new-website.md#decision
```

The project listing shows `"next":"Draft the product page."`; the second command returns only the Decision section. H2 and deeper headings become separately searchable passages.

Task list items (`- [ ]`, `- [x]`) are counted as `tasks`; the first open one becomes `next`. Keep the note's current state up to date. If you use Git, let it retain old versions instead of appending history sections.

The home and projects pages mark draft or stable projects for `review` when `updated` is missing, more than 14 days old or earlier than incoming linked evidence (`new_links`). Future-dated notes wait until their date; deprecated projects appear last. A note date means local midnight on the reading machine, and an undated note appears in no period. See [page behavior](retrieval.md#pages).

In OKF metadata, `sources` entries need a nonempty `resource`, and `verified` events need `by` and `at`. Other metadata remains available as data. Markdown links and source resources can point to notes, brain files or records; `bf validate` checks local targets. See the [concept example](#concepts) for a source link.

Two filenames have special roles in `projects/` and `concepts/`: `index.md` lists a directory's notes, and optional `log.md` groups changes under ISO date headings without frontmatter. A folder-root index permits only `okf_version` in its frontmatter; nested indexes permit none. Loose action helpers and action inputs or outputs may remain ordinary Markdown.

## Tags

Add `tags: [website, product]` to group notes across folders. Reuse labels from `bf read tags` and prefer a few lowercase, hyphenated words.

```bash
bf read tags
bf read bf://brain/tags/website
bf search "product page" --scope bf://brain/tags/website
```

With the sample note above, the `website` page includes New website. The scoped search considers only notes carrying that exact tag, while an unscoped search for `website` can also match prose. Tags need no concept note. Use your brain's configured name in BF addresses; see [tag rules](schema.md#tag-rules) for limits and pagination.

## Concepts

A concept captures knowledge beyond one project's current state. Create `concepts/explain-before-signup.md` after the [New website decision](getting-started.md#save-a-decision):

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

Add `[Explain before asking for signup](explain-before-signup.md)` to `concepts/index.md` so it is easy to browse. A concept remains searchable even without an index entry.

```bash
bf search "explain signup" --scope concepts
bf read concepts/explain-before-signup.md
bf validate
```

The search finds the concept, and the exact read retains both its advice and its Limits section. Reading `projects/new-website.md` also shows a backlink from the concept's `sources` metadata.

Keep the source and limits when you revise the lesson. Promote it to `stable` only after review supports doing so; validation checks the [metadata structure](#notes), not whether the advice is true.

## Actions

An action is one session of work. After drafting the product page for the New website project, create `actions/2026-09-27_website-review/ACTION.md` (use the session's date in the folder and metadata):

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

The first command lists the session; the second returns its saved stopping point. After the review, replace Outcome and Resume with what actually happened. For example, this fictional outcome records a gap without claiming a visitor test:

```markdown
## Outcome

The draft describes the product but does not name its intended audience. We have not tested it with visitors yet.

## Resume

Next: describe who the product helps, then test the explanation with a visitor.
```

Check tasks only when completed, and update the project's next task and `updated` date separately. Set `status: stable` after reviewing the note as a reliable account; that status alone does not mean its work is complete.

Keep reusable advice in a concept, session evidence in the action's `inputs/`, and its deliverables in `outputs/`. Link only files that exist. The [action template](https://github.com/fmind/brain-framework/blob/main/skills/bf-action/templates/action.md) adds working-context and decision sections for longer sessions.

Action folders require a real `YYYY-MM-DD` date, an underscore, a lowercase hyphenated slug and an `ACTION.md` with [valid OKF metadata](#notes). Objective, Tasks, Resume and Outcome are useful headings, not required schema fields. Loose files such as `actions/README.md` are allowed.

`bf read actions` lists actions newest first with open tasks. Reading an action folder returns `ACTION.md`, its file list, linked projects and backlinks. Markdown in its inputs and outputs is also searchable: keep it within the brain's intended audience.

## Records

A record preserves one source item. For example, the [sensor walkthrough](sensors.md#your-first-sensor) collects `brief.txt` and returns the ref `local-documents:website-demo/brief.txt`:

```bash
bf read local-documents:website-demo/brief.txt
```

That read returns the saved brief and its source details. You can cite it in the project with `[Product brief](local-documents:website-demo/brief.txt)`.

| Field        | Meaning                                                                                  |
| ------------ | ---------------------------------------------------------------------------------------- |
| `id`         | Stable per source; the record's ref is `<source>:<id>`.                                  |
| `title`      | Short, meaningful title.                                                                 |
| `text`       | The searchable body: the facts someone would ask about.                                  |
| `time`       | Event time with a timezone; places the record on period pages and in time scopes.        |
| `url`        | Where the item lives upstream.                                                           |
| `links`      | Explicit identities it relates to: `repo:github.com/owner/name`, `person:email/address`. |
| `aliases`    | Other exact identities of this item.                                                     |
| `fields`     | Normalized schema values; searchable, with typed relationship edges.                     |
| `attributes` | Structured details kept for exact reads, not searched.                                   |

Sensors print records; BF stores one JSON object per line in the record's month partition (`undated.jsonl` without a time, `snapshot.jsonl` for snapshot sources). Collecting an existing id updates that record. Put searchable facts in `title` and `text`, and additional exact-read details in `attributes`.

For revision rules, timestamps and recovery, see the [record reference](schema.md#record-revisions-and-provenance) and [file safeguards](limits.md#files).

## Directory reference

`bf init PATH` creates `projects/`, `concepts/`, `actions/`, `tests/` and a runnable `evals/retrieval.yaml`. Technical tests belong in `tests/`; retrieval cases belong in `evals/`. `--full` adds optional folders such as sensors, routines and assets. Create other folders as you need them:

```text
bf.yaml                                   # name, shared schema and sensor mappings
AGENTS.md                                 # instructions for agents working in the brain
projects/<project>.md                     # OKF project notes with OKF lifecycle statuses
concepts/index.md, concepts/<concept>.md  # reusable OKF v0.2 knowledge
actions/YYYY-MM-DD_slug/ACTION.md         # OKF session note, with inputs/ and outputs/
memories/<source>/<YYYY-MM>.jsonl         # collected items
assets/                                   # logos, images, audio and other media that notes link to
sensors/                                  # executable collectors declared in bf.yaml
routines/                                 # deterministic programs declared in bf.yaml, and other upkeep code
settings/, tests/                         # maintenance settings and technical tests
evals/*.yaml                              # retrieval acceptance suites
skills/                                   # workflow packages for agents
inputs/, originals/, logs/                # unversioned: imports to process, retained originals, routine logs
.bf/                                      # disposable search cache
```

Only Markdown under `projects/`, `concepts/` and `actions/` and JSON Lines under `memories/` are searchable. This includes Markdown in action inputs and outputs: keep them within the brain's intended audience. Everything else is ordinary brain code, configuration and media. `.bf/` is safe to delete while Brain Framework is idle.

Use `assets/` for media shared by several notes: `[logo](../assets/logo.svg)` lets validation catch a missing file. A single action's deliverables belong in its `outputs/`. Keep large media out of Git history, for example with Git LFS.

New brains ignore root `inputs/` (raw imports), `originals/` (retained source files) and `logs/` in Git. Back up `inputs/` and `originals/` with `memories/`; ignoring them does not make them disposable.

## Personal and team brains

Separate brains by audience. Keep personal records in a private brain; share reviewed project notes and team evidence in a [team brain](team.md).

### Related brains

Declare direct `brains:` paths in `bf.yaml` to include related knowledge in retrieval. References do not run programs or expand recursively. See [Brain selection](configuration.md#related-brains).

### Optional machine registration

Use `bf register PATH` to select a brain by name from elsewhere. Registration is optional and stores names and paths. See [Configuration](configuration.md#optional-machine-registration).

## Routines

The `routines/` folder holds [maintenance scripts](routines.md). Those registered in `bf.yaml` prepare review actions through `bf update`; run other upkeep scripts directly or through a scheduler. Technical tests belong in `tests/`; [retrieval cases](checks.md#retrieval-cases) belong in `evals/`.

## Decision workflows

Use `bf-action` to keep a session resumable and `bf-learn` to update knowledge after reviewing its outcome. For example: record the expected effect of the website change, compare it with visitor feedback, then revise the project and concept. These optional [agent workflows](agents.md#decision-workflows) use the existing file formats.
