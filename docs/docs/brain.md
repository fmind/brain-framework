# Brain layout and knowledge

A brain is an ordinary directory, normally a Git repository. `bf init PATH` creates it in a new, empty or freshly cloned directory without global registration. Its name defaults to the directory name; `--name NAME` chooses another stable namespace. Only explicit `--collect` registers it with collection trust. It creates `projects/`, `concepts/` and `actions/`; `--full` also creates every optional versioned folder below.

```text
bf.yaml                                   # name, shared schema and sensor mappings
AGENTS.md                                 # instructions for agents working in the brain
projects/<project>.md                     # one note per project
concepts/index.md, concepts/<concept>.md  # reusable OKF v0.2 knowledge
actions/YYYY-MM-DD_slug/ACTION.md         # one session of work, with inputs/ and outputs/
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

| Field                      | Effect                                                                                                                                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `title`                    | Result title; otherwise the first H1, then the file name.                                                                                                                |
| `type`                     | Shown in results and pages; defaults to `project`, `action` or `concept` from the folder.                                                                                |
| `status`                   | One of `draft`, `active`, `paused`, `blocked`, `done`, `stable`, `deprecated`, `archived`. Folder pages list active work first; deprecated and archived notes rank last. |
| `updated`                  | `YYYY-MM-DD`; interpreted at local midnight on the reading machine for periods such as `7d` or `2026-09`.                                                                |
| `summary`, `description`   | The note's lead in results.                                                                                                                                              |
| `tags`, `aliases`, `links` | Searchable; `aliases` are exact identities that resolve to the note; `links` add explicit relations.                                                                     |

Keep notes current rather than cumulative. `bf read projects` and the home page mark active or blocked project notes with `review` when their `updated` date is more than 14 days old or when items dated after it link to them (`new_links`), as a reminder rather than a failure. Task list items (`- [ ]`, `- [x]`) are counted as `tasks` and the first open one is shown as `next`. Git holds history, so replace outdated text instead of appending history sections. Each H2 or deeper section is its own search passage, and `path#section` reads exactly that section. Markdown links between notes, to brain files and to records (`[meeting](meetings:retention-1)`) are checked by `bf validate`.

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
| `fields`     | Normalized schema values; searchable, with typed relationship edges.                     |
| `attributes` | Structured details kept for exact reads, not searched.                                   |

Collecting the same id again replaces its line, moving it if its month changed; each id appears once per source. If both revisions declare `attributes.updated`, an older revision cannot overwrite a newer one. History lives in Git or in your backups, not in duplicate records.

If files already contain duplicate IDs, collection stops before changing that source. Run `bf validate`, preserve the conflicting revisions and reconcile them deliberately; collection never chooses which conflicting evidence to discard.

Three reserved attributes describe evidence quality: `updated` is the upstream modification timestamp, `observed` is when Brain Framework first collected this revision, and `partial: true` marks intentionally incomplete content. Timestamps require a timezone. `time` keeps its event meaning; period pages list items modified in the period under `changed` using `updated`, falling back to event time when unavailable. Collection preserves `observed` when content has not changed. Other attributes remain provider-specific.

Record writes use a recoverable transaction under `memories/.pending/`. It holds originals only while a write is incomplete; explicit `bf build`, collection or backup recovers it after an interruption; ordinary reads report the pending transaction without changing evidence. Keep this directory with the records when backing up a stopped brain, and never delete it as cache. A live backup must hold the same brain writer lock while copying files.

## Personal and team brains

Separate brains by who may read them. A directory is a context boundary, not an access-control system: use separate repositories and filesystem permissions for different audiences.

- **Personal brain**: private repository, laptop sensors (mail, calendar, Git, shell, browser, agent sessions), registered with `--collect`. Keep bulky or sensitive `memories/` out of the Git remote and back them up encrypted instead.
- **Team brain**: shared repository of projects, decisions, concepts and actions, plus records of team-scoped sources (organization issues and pull requests, shared meeting notes), created with a distinctive name and `--no-collect`. One CI job collects and commits reviewed sources, so no laptop runs shared sensor code. [Team brains](team.md) covers creation, joining, CI collection and review.

### Related brains

A brain declares its own read context in `bf.yaml`; no global configuration is required:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
version: 5
name: personal
brains:
  team:
    path: ../team
```

The key must match the target's `bf.yaml` name. Paths resolve relative to the declaring brain root, independently of the working directory; absolute paths and `~` are also supported. Prefer relative paths and agree on repository layout across machines. Use `bf://team/projects/platform.md#decision` in notes.

Search, read and retrieval evaluations include the root and its direct references only. References are bounded to 32 per brain; they never recurse, download repositories or run sensors or routines. Missing, inaccessible, invalid and mismatched references appear under `problems`; other results remain available. Multiple directories claiming the same name are excluded and reported. Repeated physical directories are searched once. An incomplete empty answer does not prove absence.

`--brain NAME` can select the enclosing brain or one of its declared names; `--brain PATH` works from anywhere. Selection chooses a new root, so its own direct references define that request's scope. Maintenance commands act on the selected root, not its references. `bf validate` still validates local evidence; foreign BF targets remain `unresolved` until explicitly read.

### Optional machine registration

`bf register PATH` is optional for discovery outside a brain. Running sensors and routines requires explicit machine trust (`bf register PATH --collect` or `bf init PATH --collect`), stored separately from shared references. Registered brains live in `~/.config/bf/config.yaml`:

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

Commands select `--brain NAME|PATH`, then `BF_BRAIN`, then the brain containing the working directory, then every registered brain. Search/read expand direct declarations of those roots; elsewhere the optional registry supplies roots. Promote personal knowledge to the team by writing a summary in the team brain and linking to what others can read.

`XDG_CONFIG_HOME` overrides `~/.config` for the registry; `XDG_STATE_HOME` overrides `~/.local/state` for private run history, locks and usage, kept in `bf/<sha256 of the brain path>/`. Any command that opens a brain creates that folder, so delete the folders of brains you remove; the state is disposable. Registration serializes updates and writes the registry atomically with owner-only file permissions; it keeps the file's leading comment lines but not other comments. Names are unique per machine: registering a second brain under a taken name fails, so choose distinctive names before sharing links. If renaming is unavoidable, update affected BF addresses explicitly; do not give clones of one shared brain different names. Automatic selection skips registered directories absent from this machine; selecting an absent brain explicitly fails. Use `--brain NAME` when a particular brain must be present for your answer.

Running `bf register PATH` again without `--collect` revokes collection trust while keeping the brain searchable. To stop selecting it automatically, remove its entry from the registry; the files remain in place. Set `enabled: false` to stop a single sensor while keeping its existing records searchable.

## Actions

An action is one session of work: `actions/YYYY-MM-DD_slug/ACTION.md` with its objective, TODO list, decisions, a Resume section with the exact next step, an Outcome once done, and links to its `inputs/` and `outputs/`. People start and resume actions explicitly, for example through the `bf-action` skill: `bf read actions` lists them newest first with their open tasks, and `bf read actions/YYYY-MM-DD_slug` returns ACTION.md with the action's files, the projects it links to and the notes that link to it. Action Markdown is searchable. `bf validate` reports an action folder whose name is not a valid date, an underscore and a lowercase hyphenated slug, or that lacks `ACTION.md`; loose files such as `actions/README.md` are allowed. Update the owning project note when an action changes its state.

## Routines

A routine is a deterministic program declared under `routines:` in `bf.yaml`, usually a script in `routines/`. `bf update` runs each due routine after the brain's sensors, with the same collection trust, direct argv, timeout, output bound and private log. Its standard output is Markdown that becomes that day's action, `actions/YYYY-MM-DD_NAME/ACTION.md`, for people and agents to review; empty output means there is nothing to review. See [routines](sensors.md#routines).

Technical checks belong in `tests/`; retrieval suites belong in `evals/`. See [retrieval cases](search.md#retrieval-cases).
