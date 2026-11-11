---
icon: lucide/notebook-pen
description: Write project notes, reusable concepts and resumable actions with complete Markdown examples.
---

# Write notes and actions

Keep live project state in `projects/`, reusable patterns in `concepts/` and tracked work sessions in `actions/`. You and your agents author notes; sensors own `memories/`.

## Notes

Projects, concepts and `ACTION.md` files use [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md) frontmatter checked by `bf validate` ([note format](schema.md#note-format)):

```markdown
---
type: project
status: draft
updated: 2026-09-27
tags: [website, product]
summary: Launch a product website that helps visitors understand the product.
---

# New website

## Now

The initial decision is recorded; implementation has not started.

## Decision

Start with a single product page because visitors need a clear explanation before signing up.

## Next actions

- [ ] Draft the product page.
```

| Field                           | Effect                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `type`                          | Required kind, such as `project`, `concept` or `action`.                                                    |
| `status`                        | `draft`, `stable` (default) or `deprecated` (closes tasks and removes the note from Home/review).           |
| `title`                         | Listing title (falls back to first H1, then filename).                                                      |
| `updated`                       | `YYYY-MM-DD` when meaning last changed; places the note on period pages.                                    |
| `summary`, `description`        | Preview lead in listings and search.                                                                        |
| `tags`                          | Case-sensitive labels (`bf read tags`; [tag rules](link-reference.md#tag-rules)).                           |
| `stale_after`                   | ISO 8601 timestamp with timezone (`2026-10-13T00:00:00+02:00`) scheduling review.                           |
| `entity`, `aliases`, `resource` | Exact subject identities ([identities](links.md#give-a-subject-a-stable-identity)).                         |
| `links`, `sources`, `fields`    | Frontmatter links, OKF `cites` provenance and declared `bf.yaml` [typed fields](links.md#set-typed-fields). |

H2+ headings become addressable `#section` targets (`bf read projects/new-website.md#decision`), and unchecked `- [ ]` items populate `bf read tasks` (`next` previews the first open box; [task rules](retrieval.md#tasks)).

### Review reminders

Projects fall `due` 14 days after their file `modified` time unless `stale_after` sets an explicit timestamp (which survives `cp` and `git clone` checkouts); other notes get deadlines only when `stale_after` is set. Linked items that occurred or updated upstream (`attributes.updated`) after the note's last edit raise `newer_evidence` and list up to 5 refs in `newer`. Run `bf read review` to triage every `due` or `newer_evidence` note, open `draft` action and `attention` item in one place ([review signals](retrieval.md#ordering-and-review-signals)).

## Concepts

Concepts capture reusable rules and syntheses that outlive one project:

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

## Limits

Hypothesis from the New website project; verify with visitors before promoting to `stable`.
```

```bash
bf search "explain signup" --scope concepts
bf read concepts/explain-before-signup.md
bf validate
```

`sources` records a `cites` backlink on the project. List concepts in `concepts/index.md` and promote `status: stable` once verified.

## Actions

Each tracked session lives in `actions/YYYY-MM-DD_topic/ACTION.md` (add a 12-hex suffix in shared Git brains; routines use 8-hex suffixes). Manage the lifecycle from the CLI or `bf-action` ([template](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-action/templates/action.md)):

```bash
bf action new website-review --title "Website review" \
  --summary "Review whether the product page explains the product before signup."
bf read review
bf read actions/2026-09-27_website-review/ACTION.md#resume
```

- **`draft` (open)**: appears in Home `actions` and `bf read review` (with `blocked_by` naming unresolved `draft` `depends-on` actions). Keep `Context`, `TODO` and `Resume` current as you work, and store session files in `inputs/` and `outputs/`.
- **`stable` (settled)**: once `Outcome` is recorded, open checkboxes are resolved and durable changes are synced back to the owning `projects/*.md` note, run `bf action status website-review stable`. Settled actions leave the active Home and `review` queues while staying searchable and readable in `bf read actions`.
- **`deprecated` (cancelled/retired)**: run `bf action status website-review deprecated` to retire the session and close its tasks.

## Records

Sensors store one JSON file per item at `memories/SOURCE/<sha256-id>.json`, addressed as `SOURCE:ID` (`id`, `title`, optional `text`, `time`, `url`, `links`, `aliases`, `attributes` and BF-mapped `fields`; [record revisions](schema.md#record-revisions-and-provenance)).

## Directory reference

`bf init PATH` creates the core layout (`--full` adds `memories/`, `assets/`, `sensors/`, `routines/` and `skills/`):

```text
bf.yaml                                  # name, fields, sensors, routines, assistants, channels, watch
AGENTS.md                                # instructions for agents in this brain
projects/<project>.md                    # project state, decisions and open tasks
concepts/index.md, concepts/<concept>.md # reusable OKF concepts
actions/YYYY-MM-DD_topic/ACTION.md       # tracked sessions (+ optional inputs/ and outputs/)
memories/<source>/<sha256-id>.json       # collected records and assistant run records (.forgotten alongside)
evals/*.yaml                             # retrieval suites for bf eval
sensors/, routines/, prompts/, channels/ # programs and prompts declared in bf.yaml
hooks/, skills/, assets/                 # host hooks, brain workflow skills and linked media
logs/, inputs/, originals/, .bf/         # Git-ignored logs, staging/captures and disposable SQLite cache
```

Back up `memories/`, `inputs/`, `originals/` and `$XDG_STATE_HOME/bf/runtime/` ([queue recovery](channels.md#queue-and-recovery)); only `.bf/` and `logs/` are disposable while idle. Separate private and shared brains with `brains:` ([team setup](team.md), [configuration](configuration.md#related-brains), [agent workflows](agents.md#decision-workflows)).
