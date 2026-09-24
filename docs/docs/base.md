# Base layout and knowledge

A base is an ordinary directory, normally a Git repository. `fkf init PATH --name NAME` creates it and registers it for this user.

```text
fkf.yaml                         # name and collectors
AGENTS.md                        # instructions for agents working in the base
projects/<project>.md            # one note per project
wiki/index.md, wiki/<concept>.md # reusable OKF v0.2 knowledge
tasks/YYYY-MM-DD_slug/TASK.md    # resumable work, with inputs/ and outputs/
records/<source>/<YYYY-MM>.jsonl # collected items
sources/                         # collectors
scripts/, configs/, tests/       # maintenance code, its settings and its tests
skills/                          # workflow packages for agents
.fkf/                            # disposable search cache
```

Only Markdown under `projects/`, `wiki/` and `tasks/` and JSON Lines under `records/` are searchable. This includes Markdown in task inputs and outputs: keep them within the base's intended audience. Everything else is ordinary base code and configuration. `.fkf/` is safe to delete while FKF is idle.

## Notes

A note is Markdown with optional YAML frontmatter. FKF reads a few fields and leaves the rest as data.

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
| `type`                     | Filter with `--type`; defaults to `project`, `task` or `wiki` from the folder.                                                                              |
| `status`                   | One of `draft`, `active`, `paused`, `blocked`, `done`, `stable`, `deprecated`, `archived`; filter with `--status`. Deprecated and archived notes rank last. |
| `updated`                  | `YYYY-MM-DD`; interpreted at local midnight on the reading machine for time windows such as `--since 7d`.                                                   |
| `summary`, `description`   | The note's lead in results.                                                                                                                                 |
| `tags`, `aliases`, `links` | Searchable; `aliases` are exact identities that resolve to the note; `links` add explicit relations.                                                        |

Keep notes current rather than cumulative. `fkf status` lists active or blocked project notes whose `updated` date is more than 14 days old under `review`, as a reminder rather than a failure. Git holds history, so replace outdated text instead of appending history sections. Each H2 or deeper section is its own search passage, and `path#section` reads exactly that section. Markdown links between notes, to base files and to records (`[meeting](meetings:retention-1)`) are checked by `fkf validate`.

`wiki/` follows [OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md): each concept declares a `type`; lifecycle is `draft`, `stable` or `deprecated`; `sources` are mappings with a `resource`; `verified` events carry `by` and `at`. `wiki/index.md` may only declare `okf_version`, and an optional `wiki/log.md` groups changes under ISO date headings. `fkf validate` checks this structure; it does not verify claims.

## Records

A record is one source item. Collectors print them; FKF stores one JSON object per line in the partition of the record's month (`undated.jsonl` without a time, `snapshot.jsonl` for snapshot sources).

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

If files already contain duplicate IDs, collection stops before changing that source. Run `fkf validate`, preserve the conflicting revisions and reconcile them deliberately; collection never chooses which conflicting evidence to discard.

Three reserved attributes describe evidence quality: `updated` is the upstream modification timestamp, `observed` is when FKF first collected this revision, and `partial: true` marks intentionally incomplete content. Timestamps require a timezone. `time` keeps its event meaning; `--changed-since` uses `updated`, falling back to event time when unavailable. Collection preserves `observed` when content has not changed. Other attributes remain provider-specific.

Record writes use a recoverable transaction under `records/.pending/`. It holds originals only while a write is incomplete; explicit `fkf build`, collection or backup recovers it after an interruption; ordinary reads report the pending transaction without changing evidence. Keep this directory with the records when backing up a stopped base, and never delete it as cache. A live backup must hold the same base writer lock while copying files.

## Personal and team bases

Separate bases by who may read them. A directory is a context boundary, not an access-control system: use separate repositories and filesystem permissions for different audiences.

- **Personal base**: private repository, laptop collectors (mail, calendar, Git, shell, browser, agent sessions), registered with `--collect`. Keep bulky or sensitive `records/` out of the Git remote and back them up encrypted instead.
- **Team base**: shared repository of projects, decisions, wiki and tasks, plus records of team-scoped sources (organization issues and pull requests, shared meeting notes). Collect them in CI, for example a nightly job running `fkf register . --collect && fkf update` and committing `records/`, so no laptop runs shared collector code. New bases ignore `records/` by default: explicitly opt reviewed team-source paths into version control before relying on CI publication.

Registered bases live in `~/.config/fkf/config.yaml`:

```yaml
# https://fmind.github.io/fkf/
bases:
  brain:
    path: /home/me/brain
    collect: true
  team:
    path: /home/me/team-knowledge
    collect: false
```

Commands select `--base NAME|PATH`, then `FKF_BASE`, then the base containing the working directory, then every registered base. Within a base, agents therefore stay in that base; elsewhere they search everything you registered. Promote personal knowledge to the team by writing a summary in the team base and linking to what others can read.

`XDG_CONFIG_HOME` overrides `~/.config` for the registry; `XDG_STATE_HOME` overrides `~/.local/state` for private run history, locks and usage. Registration serializes updates and writes the registry atomically with owner-only file permissions. Keep names unique. Automatic selection skips registered directories absent from this machine; selecting an absent base explicitly fails. Use `--base NAME` when a particular base must be present for your answer.

Running `fkf register PATH` again without `--collect` revokes collection trust while keeping the base searchable. To stop selecting it automatically, remove its entry from the registry; the files remain in place. Set `enabled: false` to stop a single source while keeping its existing records searchable.

## Tasks

Create `tasks/YYYY-MM-DD_slug/TASK.md` before delegating substantial work: objective, TODO list, decisions, a Resume section with the exact next action, and links to `inputs/` and `outputs/`. Task Markdown is searchable. Update the owning project note when a task changes its state.
