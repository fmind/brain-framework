# Brain layout and knowledge

A brain is an ordinary directory, normally a Git repository. `bf init PATH` creates it in a new, empty or freshly cloned directory and registers it for this user under the directory's name; `--name NAME` chooses another name, and `--no-collect` withholds collection trust. It creates `projects/`, `concepts/` and `actions/`; `--full` also creates every optional versioned folder below.

```text
bf.yaml                                   # name and sensors
AGENTS.md                                 # instructions for agents working in the brain
projects/<project>.md                     # one note per project
concepts/index.md, concepts/<concept>.md  # reusable OKF v0.2 knowledge
actions/YYYY-MM-DD_slug/ACTION.md         # resumable work, with inputs/ and outputs/
memories/<source>/<YYYY-MM>.jsonl         # collected items
assets/                                   # logos, images, audio and other media that notes link to
sensors/                                  # executable collectors declared in bf.yaml
routines/, settings/, tests/              # maintenance code, its settings and its tests
skills/                                   # workflow packages for agents
inputs/, originals/, logs/                # unversioned: imports to process, retained originals, routine logs
.bf/                                      # disposable search cache
```

Only Markdown under `projects/`, `concepts/` and `actions/` and JSON Lines under `memories/` are searchable. This includes Markdown in action inputs and outputs: keep them within the brain's intended audience. Everything else is ordinary brain code, configuration and media. `.bf/` is safe to delete while Brain Framework is idle.

Create the other folders when you need them. `assets/` holds versioned media shared by several notes, such as a logo; link each file from the notes that use it (`[logo](../assets/logo.svg)`) so `bf validate` catches a missing one. Files produced by one action belong in its `outputs/` instead. Keep large media out of Git history, for example with Git LFS. New brains ignore root `inputs/` (raw imports waiting to be processed), `originals/` (source files kept as provenance after processing) and `logs/` in Git; back up `inputs/` and `originals/` with `memories/`.

## Notes

A note is Markdown with optional YAML frontmatter. Brain Framework reads a few fields and leaves the rest as data.

```markdown
---
type: project
status: active
updated: 2026-09-22
tags: [retention]
aliases: [repo:github.com/team/archive]
summary: Keep original evidence so decisions stay explainable.
---

# Archive

What this project is for, in two sentences.

## Now

The current state, in a short paragraph.

## Decisions

- 2026-09-10: keep originals, because providers delete content ([meeting](meetings:retention-1)).

## Next actions

- [ ] Publish the retention guide.
```

| Field                      | Effect                                                                                                                                                      |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `title`                    | Result title; otherwise the first H1, then the file name.                                                                                                   |
| `type`                     | Filter with `--type`; defaults to `project`, `action` or `concept` from the folder.                                                                         |
| `status`                   | One of `draft`, `active`, `paused`, `blocked`, `done`, `stable`, `deprecated`, `archived`; filter with `--status`. Deprecated and archived notes rank last. |
| `updated`                  | `YYYY-MM-DD`; interpreted at local midnight on the reading machine for time windows such as `--since 7d`.                                                   |
| `summary`, `description`   | The note's lead in results.                                                                                                                                 |
| `tags`, `aliases`, `links` | Searchable; `aliases` are exact identities that resolve to the note; `links` add explicit relations.                                                        |

Keep notes current rather than cumulative. `bf status` lists active or blocked project notes whose `updated` date is more than 14 days old under `review`, as a reminder rather than a failure. Git holds history, so replace outdated text instead of appending history sections. Each H2 or deeper section is its own search passage, and `path#section` reads exactly that section. Markdown links between notes, to brain files and to records (`[meeting](meetings:retention-1)`) are checked by `bf validate`.

`concepts/` follows [OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md): each concept declares a `type`; lifecycle is `draft`, `stable` or `deprecated`; `sources` are mappings with a `resource`; `verified` events carry `by` and `at`. `concepts/index.md` may only declare `okf_version`, and an optional `concepts/log.md` groups changes under ISO date headings. `bf validate` checks this structure; it does not verify claims.

## Records

A record is one source item. Sensors print them; Brain Framework stores one JSON object per line in the partition of the record's month (`undated.jsonl` without a time, `snapshot.jsonl` for snapshot sources).

| Field        | Meaning                                                                                  |
| ------------ | ---------------------------------------------------------------------------------------- |
| `id`         | Stable per source; the record's ref is `<source>:<id>`.                                  |
| `title`      | Short, meaningful title.                                                                 |
| `text`       | The searchable body: the facts someone would ask about.                                  |
| `time`       | Event time with a timezone; drives `--since` and `--until`.                              |
| `url`        | Where the item lives upstream.                                                           |
| `links`      | Explicit identities it relates to: `repo:github.com/owner/name`, `person:email/address`. |
| `aliases`    | Other exact identities of this item.                                                     |
| `attributes` | Structured details kept for exact reads, not searched.                                   |

Collecting the same id again replaces its line, moving it if its month changed; each id appears once per source. If both revisions declare `attributes.updated`, an older revision cannot overwrite a newer one. History lives in Git or in your backups, not in duplicate records.

If files already contain duplicate IDs, collection stops before changing that source. Run `bf validate`, preserve the conflicting revisions and reconcile them deliberately; collection never chooses which conflicting evidence to discard.

Three reserved attributes describe evidence quality: `updated` is the upstream modification timestamp, `observed` is when Brain Framework first collected this revision, and `partial: true` marks intentionally incomplete content. Timestamps require a timezone. `time` keeps its event meaning; `--changed-since` uses `updated`, falling back to event time when unavailable. Collection preserves `observed` when content has not changed. Other attributes remain provider-specific.

Record writes use a recoverable transaction under `memories/.pending/`. It holds originals only while a write is incomplete; explicit `bf build`, collection or backup recovers it after an interruption; ordinary reads report the pending transaction without changing evidence. Keep this directory with the records when backing up a stopped brain, and never delete it as cache. A live backup must hold the same brain writer lock while copying files.

## Personal and team brains

Separate brains by who may read them. A directory is a context boundary, not an access-control system: use separate repositories and filesystem permissions for different audiences.

- **Personal brain**: private repository, laptop sensors (mail, calendar, Git, shell, browser, agent sessions), registered with `--collect`. Keep bulky or sensitive `memories/` out of the Git remote and back them up encrypted instead.
- **Team brain**: shared repository of projects, decisions, concepts and actions, plus records of team-scoped sources (organization issues and pull requests, shared meeting notes), created with a distinctive name and `--no-collect`. One CI job collects and commits reviewed sources, so no laptop runs shared sensor code. [Team brains](team.md) covers creation, joining, CI collection and review.

Registered brains live in `~/.config/bf/config.yaml`:

```yaml
# https://fmind.github.io/brain-framework/
brains:
  brain:
    path: /home/me/brain
    collect: true
  team:
    path: /home/me/team-knowledge
    collect: false
```

Commands select `--brain NAME|PATH`, then `BF_BRAIN`, then the brain containing the working directory, then every registered brain. Within a brain, agents therefore stay in that brain; elsewhere they search everything you registered. Promote personal knowledge to the team by writing a summary in the team brain and linking to what others can read.

`XDG_CONFIG_HOME` overrides `~/.config` for the registry; `XDG_STATE_HOME` overrides `~/.local/state` for private run history, locks and usage, kept in `bf/<sha256 of the brain path>/`. Any command that opens a brain creates that folder, so delete the folders of brains you remove; the state is disposable. Registration serializes updates and writes the registry atomically with owner-only file permissions; it keeps the file's leading comment lines but not other comments. Names are unique per machine: registering a second brain under a taken name fails, so rename your own brain rather than a shared one. Automatic selection skips registered directories absent from this machine; selecting an absent brain explicitly fails. Use `--brain NAME` when a particular brain must be present for your answer.

Running `bf register PATH` again without `--collect` revokes collection trust while keeping the brain searchable. To stop selecting it automatically, remove its entry from the registry; the files remain in place. Set `enabled: false` to stop a single sensor while keeping its existing records searchable.

## Actions

Create `actions/YYYY-MM-DD_slug/ACTION.md` before delegating substantial work: objective, TODO list, decisions, a Resume section with the exact next action, and links to `inputs/` and `outputs/`. Action Markdown is searchable. `bf validate` reports an action folder whose name is not a valid date, an underscore and a lowercase hyphenated slug, or that lacks `ACTION.md`; loose files such as `actions/README.md` are allowed. Update the owning project note when an action changes its state.
