# Changelog

All notable changes to Brain Framework (formerly FKF) are documented here. This project follows [Semantic Versioning](https://semver.org/) from its first public release.

## Unreleased

## [v18.1.2](https://github.com/fmind/brain-framework/releases/tag/v18.1.2) - 2026-10-04

A documentation release. The brain format (`version: 7`), `bf.yaml`, the reply schemas and the search cache are unchanged. The packaged `bf-setup` skill only changes its Claude Code guidance: run `bf skills DIR` to refresh installed copies.

### Documentation

- The README presents BF as a second brain on your own computer: agents opened in a brain start from your projects and evidence instead of a blank session. Four diagrams show the gather-to-learn loop, a blank session beside a brain session, how field mappings turn four tools' names into one knowledge graph and how a project is flagged for review when its linked evidence changes. New sections cover the questions a brain answers and the tools behind them, the sources one brain can gather, `bf.yaml` as the coordination hub, personal and team brains shared through Git and the trade-offs: local execution, provider setup and the agent harness's data terms. It also points agents to `llms.txt`.
- Claude Code 2.1.277+ loads a brain's `AGENTS.md` on its own, so the guides no longer ask for a `CLAUDE.md` holding `@AGENTS.md`; the agent guide keeps that import as the fallback for older versions, and an existing brain may keep its `CLAUDE.md`.

### Changed

- The release scripts carry a `.sh` extension: `scripts/push-and-tag.sh`, `scripts/release-notes.sh` and `scripts/verify-release-tag.sh`.

## [v18.1.1](https://github.com/fmind/brain-framework/releases/tag/v18.1.1) - 2026-10-03

A documentation release. The package, the brain format (`version: 7`), `bf.yaml`, the reply schemas and the search cache are unchanged; no upgrade step is needed.

### Documentation

- The documentation site gains offline search (Ctrl+K or `/`), header tabs for Start here, Guides, Reference and Privacy and legal, page icons, a light, dark and system color toggle, GitHub repository and edit links, and a footer with privacy, notices and license links. The changelog and license are site pages, so every navigation entry stays on the site. Search keeps the site free of accessibility violations in an axe-core audit: the closed dialog leaves the tab order, its controls are named and its result paths meet contrast.
- Agents can read the guides as Markdown through [`llms.txt`](https://fmind.github.io/brain-framework/llms.txt), which summarizes each guide, [`llms-full.txt`](https://fmind.github.io/brain-framework/llms-full.txt) or `index.md` beside each page, which each guide's HTML advertises as its Markdown alternate. The overview now links every guide.

## [v18.1.0](https://github.com/fmind/brain-framework/releases/tag/v18.1.0) - 2026-10-03

A review release of fixes. The brain format (`version: 7`), `bf.yaml` and the reply schemas are unchanged; listed `fields` are bounded, and the search cache rebuilds once. Upgrade with `uv tool upgrade brain-framework` (or update a brain's pin), then run `bf skills DIR`.

### Fixed

- `bf skills --force` keeps a file you edited that the new version no longer ships, as it keeps every other file BF does not ship, instead of deleting it.
- A record commit whose completion marker landed before a final directory sync failed reports success instead of "nothing was written".
- A registered brain whose folder exists but cannot be reached fails with `cannot reach the brain registered as NAME` instead of advice to register it again, which `bf register` then refused.
- `bf run --hook ""`, as an unset variable gives it, is invalid input instead of running the first argument as a routine; `bf status --check --watch` names `--watch` in its invalid-input line.
- `bf eval --baseline=~/eval.json` expands `~`, like every other path option. Without a home directory, commands fail with one `bf:` line naming `HOME` instead of a traceback.
- `bf schedule` installs systemd units below `XDG_CONFIG_HOME` when it is set, where the user manager reads them.
- `bf watch` runs updates for a `pip install --user` installation, and `bf watch --json` reports unavailable desktop alerts with a `bf:` line on stderr instead of a JSON object.
- A claim footnote's label can no longer change a note's structure: `[^```]` or `[^<pre>]` used to open a block that hid the sections and links after it. Links in a footnote's continuation paragraph, indented by four spaces or a tab, now support the section that cites it, and labels match regardless of case, as on GitHub.
- A copied document whose headings repeat an explicit anchor, or whose title's slug another heading anchors, stays searchable: the later heading takes a generated slug. OKF notes still fail `bf validate`.
- An OKF `sources` entry whose `?rel=` names an undeclared relation still cites its target in reads; `bf validate` still reports the relation.
- Validation messages count only the distinct problems they do not name.
- The example sensor, routine and hook guides make a downloaded script executable with `chmod +x` before configuring it as a command, since a download drops the executable bit and `bf validate` then rejects the program; `github-history.md` copies from the release tag matching `bf --version` and says when to raise `timeout` for a large backfill.
- Programs no longer inherit `ZDOTDIR`, `PHPRC` or `PHP_INI_SCAN_DIR`, which make zsh or PHP run code before the program starts.
- The guarded-write helper follows a linked brain folder and links above it, such as `/home` on Fedora Atomic or `/tmp` on macOS, as bf does, instead of refusing every absolute path through them; links inside the brain are still refused.
- `bf-use` asks before updating the skills for a newer `bf`, and names the folder to pass to `bf skills`.
- Quitting `bf watch` while an update is still stopping prints `bf: stopping the active update; this can take up to 60 seconds` instead of leaving a silent terminal.
- An exact read across several brains returns the brain that holds the ref with a `problems` entry for another brain that fails, such as on a malformed record of a shared source or a busy writer, instead of failing; when no brain holds it, the failure stands.
- A page's address as `--scope`, such as `bf://brain/projects`, `bf://brain/7d` or `bf://brain/memories/gmail`, is invalid input instead of silently matching nothing; use its brain-relative form.
- Listed `fields` stop at 2 KiB of JSON per item, in field-name order, so a shared brain declaring hundreds of fields cannot push home, a listing or a note's backlinks past the reply limit; exact reads keep every field.
- The retrieval reference states that a relation page's `total` includes the claims of its narrower relations.

## [v18.0.0](https://github.com/fmind/brain-framework/releases/tag/v18.0.0) - 2026-10-03

A review release. Review signals now follow upstream changes and the evidence a note cites, the home page and exact reads summarize what needs attention, and every interface reports invalid input the same way. The brain format (`version: 7`) and `bf.yaml` are unchanged apart from accepting empty sections; stricter identity rules and bounds are listed below. Reply schemas only gain optional fields, and the search cache rebuilds once.

### Breaking changes

- **Invalid input.** A usage error prints one stderr line, `bf: invalid input: ARGUMENT: reason (see bf COMMAND -h)`, with exit 2, instead of Typer's usage text and boxed panel; help is unchanged. MCP clients receive the same rejections as `invalid input: ARGUMENT: reason` naming the tool argument (`query`, `scope`, `ref` or `rel`), including malformed refs, periods, addresses, scopes and relations that used to return bare messages.
- **Exit codes.** These are invalid input (exit 2) instead of failures (exit 1): `--rel` on a page written with a trailing slash (`projects/`) or on an existing folder page, `--rel` with a `#section`, a section of a page (`bf read tasks#x`, `bf read bf://NAME/7d#x`), an invalid period below `memories/SOURCE/`, a ref that is not UTF-8 and `bf watch --poll-interval nan`.
- **Empty values.** An explicit empty `--brain` is invalid input instead of falling back to `BF_BRAIN`, the enclosing brain or every registered brain, and so are an empty `PATH` for `init` and `register`, `DIR` for `skills` and `schedule --output`.
- **Scopes and retrieval cases.** A note or page section as `--scope`, such as `projects/atlas.md#next-actions`, is invalid input instead of silently matching nothing. `bf eval` checks `expect` and `forbid` refs as `bf read` checks a ref, so a malformed or unnormalized ref fails the suite instead of a `forbid` that always passed, and a malformed identity query fails when the suite loads.
- **Identities.** An identity holding a Unicode format character (category Cf, such as U+200B, U+200C, U+200D, U+202E, U+FEFF or a soft hyphen) is rejected: two identities that look alike would silently differ. A BF entity, alias or resource must reach its note or record: another brain's namespace, a `#section`, a note file path, a page address or a `SOURCE:ID` address fails `bf validate`, and the note or record leaves the search cache like one with a foreign alias. A malformed BF `resource` makes its note invalid, like a malformed alias.
- **Bounds.** Note frontmatter `aliases` and `links` hold at most 1,000 items, like tags and records, and a record's `fields` at most 1,000 names.
- **Routine replies.** `bf run` rejects a disabled routine before running anything, with a stderr diagnostic and no JSON reply, as it rejects an unknown one. A `--dry-run` reply no longer names an `action` path, which the real run picks anew, and reports `skipped` when the real run would skip.
- **Skills.** `bf skills` reports `newer`, exits 1 and leaves the folder when a newer `bf` installed a different copy of a skill, such as a brain's pinned runtime sharing the skills directory; `--force` installs this version's copy. The packaged skills search, read and check every brain with the installed `bf`, never through a brain's pinned runtime, which installs and runs code the brain supplies.
- **Heading slugs.** A heading whose title spans several lines, or whose `{#id}` sits inside emphasis, a code span, link text or an escape, gets a new slug, such as `#title-line-one-line-two`; `bf validate` reports links to the old one.

Upgrade: run `uv tool upgrade brain-framework` (or update a brain's pin), then `bf skills DIR`; the skills now require Brain Framework 18. Drop empty `--brain ""` arguments from scripts and host configurations, and match the new `bf: invalid input:` line where a script parsed usage errors. Run `bf validate`: retype the identities it names for invisible characters or unreachable declarations, point links at renamed heading slugs, and fix a sensor whose records it reports, then collect again, since collection replaces its own records. Re-copy the example `git-history.py`, `github-history.py`, `local-documents.py`, `prompt-context.py`, `session-context.py` or `weekly-review.py` if your brain uses one; remove `timeout: 3600` from a `github-history.py` sensor, and note that `git-history.py --skip` with any worktree's path now skips its whole repository.

### Added

- Review signals follow evidence that changed: a project linked from a record whose upstream `updated` is after the note's last edit, such as an issue closed or a pull request merged, needs review, and so does a note citing a record that happened or changed since. The optional `newer` field names up to five of those items, newest first. A future event counts once it happens, and an edit always clears the flag.
- Exact reads summarize their note: a section read states the note's `title`, `type`, `status` and `date`, and a whole read of a project, or of a note with `stale_after`, carries `tasks`, `next` and its review signals.
- The home page reviews every current project, lists those needing review first, ends near the 32 KiB page budget and reports `projects_total`. Its actions, changed and upcoming items carry the same review signals, so an action or concept past `stale_after` shows there, and deprecated notes leave every home section.
- `bf validate` warns, without failing, about record `links` that are neither identities nor URLs and about a temporary file an interrupted write left behind, and checks programs launched through a command, such as `[uv, run, ..., sensors/brief.py]`.
- `bf schedule` warns when its interval is at or above the shortest selected `refresh`; `bf status` usage windows gain `since` when heavy use cut them.
- `bf register` at a moved brain's new place replaces the entry whose folder no longer exists, or which reaches the same brain through another path, and reports `replaced`.
- A Python whose SQLite is older than 3.35 or lacks FTS5 or JSON functions fails with a message naming the requirement and the fix, instead of a misleading cache error.

### Fixed

- Search keeps the English word `comment`, and a quoted word is always a search term. `one`, `ones`, `doing`, `having`, `ours`, `yours`, `theirs` and `hers` drop like other function words: stemming made them match nearly every passage.
- Note leads and task text keep identifiers such as `MAX_FILE_SIZE`, `deploy_all.sh`, `2*3` and link refs such as `_drafts/`.
- Reading a record, and its relation page, lists links to its `SOURCE:ID` from other selected brains, as search did; review signals skip names that several items claim, as backlinks do.
- A large read's `outline` lists only the headings inside it, so an H1-only note or a section without subsections fills its first page instead of pointing back to itself. With several brains, pages fit their items within the 32 KiB budget once refs are named by address.
- A refresh loads `bf.yaml` once: a `bf.yaml` briefly invalid during an editor save no longer leaves the files indexed meanwhile skipped until `bf build`. A killed build's `.bf/index.sqlite.new` is removed, and each skipped file's error is cut to 1 KiB so a few hostile files cannot push replies past their limit.
- Parsing a note with many repeated headings, or a long run of `[^`, takes linear time instead of stalling search, validation and section reads.
- A heading anchor is read from the heading's source, and generated `-N` slugs skip explicit anchors. An indented OKF footnote definition keeps its link, which supports the section that first cites it.
- A link Python cannot parse, such as `http://[your-host]/`, or a relative link holding a backslash no longer makes a copied document unsearchable; link errors name the line or frontmatter key.
- `bf validate` reports each problem of a note once, names where a link problem first occurs, no longer turns links to a note's entity into false "unresolved BF target" problems, and reports a record link whose scheme case differs from its source, such as `Mail:m1`.
- A huge hex or octal YAML integer is invalid YAML instead of a crash of every search over the brain and its references. Validation messages name at most five problems, then how many more, and sensor output is checked record by record, so malformed output or frontmatter cannot build megabytes of error text.
- An unreadable top-level folder is skipped and reported while the rest of the brain answers, Unix sockets are reported as special files, and empty `brains:`, `fields:`, `sensors:`, `routines:` or `watch:` sections read as empty.
- A `--brain` path without `bf.yaml` fails naming the selection before anything runs, a registered brain whose folder is absent fails with the registry's message, and `bf register` keeps a name whose folder exists but cannot be reached.
- A window collection that ends before it runs, such as a backfill chunk, extends coverage without counting as a fresh collection. A `log` routine is no longer killed past `max_bytes`; its log keeps the last 256 KiB.
- A program ended by a signal fails with `program was killed by SIGKILL` instead of `exited with status -9`. An action folder left with only a killed write's temporary file no longer makes the routine skip for the day.
- Collection checks projected records against the record bounds and validates each record once.
- `bf schedule` previews copy from `~/.config/bf-schedules`, where its warning suggests writing, and `--output DIR` no longer fails when DIR holds the state directory.
- A disabled program fails with `sensor NAME is disabled in bf.yaml; set enabled: true to run it` instead of suggesting its own name. `bf run --hook EVENT` reads standard input only when a routine lists the hook, so an unlisted hook succeeds at once in agent shells.
- Diagnostics and MCP error text escape control and format characters from file names and keys, and replies escape every Unicode format character, so a shared brain can neither send terminal escape sequences nor hide text. MCP error text hides brain roots, the registry and the home directory.
- A closed or full standard output fails with one line and exit 1 instead of a traceback or exit 120; closing the terminal under `bf watch` exits 130.
- `bf watch` shows bf's own diagnostic when `bf.yaml` becomes invalid or drops a selected program, `notifications: success` and `all` alert while another program keeps failing, and the details panel shows local time.
- `bf skills` finishes an interrupted install or update instead of reporting its own partial copy as `unmanaged` or as your edits.
- MCP integer arguments accept integral floats such as `5.0`; `tags/LABEL/` and `bf eval --path evals/` accept a trailing slash; default brain names fold accents (`Équipe produit` becomes `equipe-produit`).
- The example `git-history.py` collects repositories whose scanned checkouts are only worktrees, once each; `github-history.py` pages by modification time and reads merge state from the issue listing, one request per 100 items instead of one more per pull request; `local-documents.py` skips Office owner files; the example hooks and weekly review name items by `bf://` address across brains.
- The skills run helpers with an installed Python 3.14 when `python3` is older (`uv python find --system --no-config --no-project 3.14`), never through `uv run`, which adopts a `.venv` above the working directory, and they review a brain's `.venv/`, `uv.toml` and `.python-version` before running its pin. The guarded-write helper names its temporary file like bf's own writes.

### Changed

- `bf mcp` validates its brain selection at startup, then resolves it again for each call, so registry changes need no restart.
- Faster: a full build creates its schema in one transaction, a search after an edit scans the brain once, problem checks use an index, record commits count files without a stat each, home computes review signals in one query, and validation parses each note once.
- Identity search no longer repeats an untyped link beside a typed one from the same origin; usage history keeps 30 days; help and errors name arguments `QUERY`, `REF`, `SENSOR`, `PATH` and `DIR`.
- The contract gate checks configuration schemas for narrowing and reply schemas from the reader's side; `mise run check` validates the changelog, and a hung test ends the run after 300 seconds.
- The documentation, skills and examples describe this release: identity rules, review signals, invalid-input lines, the pinned-runtime review and the new limits. Reference pages own each contract and guides link to them.

## [v17.0.0](https://github.com/fmind/brain-framework/releases/tag/v17.0.0) - 2026-10-02

A review release. The brain format (`version: 7`), `bf.yaml` and the search and read reply schemas are unchanged. The command line is stricter, so input that was silently misread now fails, and failures that took down a whole brain are now contained.

### Breaking changes

- **Routine input.** `bf run ROUTINE` passes piped input only with `--stdin`. It used to read any open standard input and failed after 10 seconds in agent shells that hold it open. `bf run --hook EVENT` still passes what Git pipes, such as the refs of a push.
- **Repeated options.** An option that takes one value may appear once: `bf search launch --scope projects --scope concepts`, a second `--brain`, `--rel` or `--hook` exits 2 naming it. The last value used to win, so the answer silently ignored the others. `--sensor` and `--routine` still repeat to select several programs.
- **Routine arguments.** `bf run` rejects an unknown option before `--`, such as `--dryrun`, with exit 2. It used to pass it to the routine, which then ran for real.
- **Read relations.** `bf read PAGE --rel RELATION` is invalid input (exit 2), as a page is recognizable before any brain is read.
- **MCP arguments.** Tool arguments are validated strictly: a `limit` of `true` or `"5"` is an error instead of a coerced value.
- **Retrieval cases.** A plain `expect` or `forbid` ref in `bf eval` names a file of the evaluated brain. A related brain's note at the same path used to satisfy it; name that note by its `bf://NAME/...` address.

Upgrade: run `uv tool upgrade brain-framework` (or update a brain's pin), then `bf skills DIR`; the skills now require Brain Framework 17. Add `--stdin` where a script pipes input into a direct `bf run ROUTINE`, and put `--` before routine arguments that start with a dash. Re-copy the example `git-history.py`, `prompt-context.py`, `session-context.py` or `weekly-review.py` if your brain uses one. Then run `bf validate` and `bf eval`.

### Added

- `bf status` lists each brain's `attention`: the scheduled programs that failed or are `overdue` or `never` succeeded, as the home page does. A brain reported `"healthy": false` with `"problems": []` and left the reason to a scan of every source and routine.

### Fixed

- Search keeps a function word written as an acronym. `EU AI Act` searched only `act`, and `AI`, `IT` or `US` dropped from any query, because they spell French or English function words. Lowercase function words and the habitual `AND` and `OR` still drop. The example `prompt-context.py` hook mirrors the rule.
- Searching a note's title finds the note before a section that mentions it: a section's note title and parents rank below a heading. `Atlas next actions` still finds that section.
- One unreadable folder, such as a source restored with another owner, no longer fails every search, status, validation and build of the brain: it is skipped and reported, and `bf validate` names it as an unreadable folder.
- Collection stops below the 100,000-entry scan limit of its source and fails like any other collection, so a growing window source can no longer make every search and read of the brain fail. A commit changing exactly as many records as the limit no longer leaves a pending transaction behind.
- An action routine's relative links and headings are checked before writing, like its metadata and relations: an action that `bf validate` rejects, such as one linking `projects/x.md` instead of `../../projects/x.md`, is never written.
- `bf validate` reports every problem of a note, and links to a note it cannot parse are no longer reported as unresolved: one invalid date or undeclared relation hid the note's other problems and broke the links of other notes.
- A far-future `stale_after`, such as `9999-12-31T23:00:00Z`, no longer crashes the home and projects pages in timezones east of UTC.
- `bf mcp` stops at once on SIGTERM or Ctrl-C with exit 130, instead of waiting for another input line.
- Reading a note alias named like a source, such as `jira:ATL` beside a `jira` source, no longer fails on another malformed record of that source or while a writer holds the brain.
- Filesystems reporting 64-bit hashed inode numbers, such as mergerfs, no longer fail every cache refresh with a traceback.
- A query starting with `bf:`, such as `bf: how to configure sensors`, searches its words instead of failing as an invalid address.
- The example `git-history.py` skips linked worktrees, which collected their repository's whole history again; records already collected stay. The example routine and session hook read a task's Markdown link as its label.
- The meeting-notes recipe has the agent draft GitHub issues for review instead of creating them from calendar text that anyone who invites you can write.

### Changed

- Packaged skills no longer carry `metadata.version`, so a release changes a skill's files only when its content changes; `.bf-skill.json` still records the installing version.
- Adding `broader` or `targets` to a relation no longer rebuilds the search cache: relation pages read `broader` at query time, and collection and validation check `targets`.
- The schedule preview suggests `--output ~/.config/bf-schedules`, outside the brain, like the documentation: the generated files hold this machine's paths.
- The documentation states exact reads across brains as they behave: while a brain is unavailable, a found ref returns with a `problems` entry and a missing one fails as incomplete.

## [v16.1.1](https://github.com/fmind/brain-framework/releases/tag/v16.1.1) - 2026-09-29

A patch release from a review of 16.1.0: no format or reply change. Upgrade with `uv tool upgrade brain-framework`, then `bf skills DIR` to update unedited skills.

### Fixed

- A declared `number` field set to `.nan` or `.inf` in a note's frontmatter passed `bf validate` and then failed every search or read reply listing the note with a traceback. `bf validate` now reports the frontmatter as invalid, and retrieval skips the note with a problem like any other invalid note.
- A path starting with an unknown `~user`, such as `bf search --brain ~typo/brain`, `bf init`, `bf register`, `bf skills` or `bf schedule --output`, fails with `bf: a path's ~ or ~user home directory cannot be resolved` instead of a traceback.
- An exact read of two records that name each other as aliases no longer recurses when both files disappear between the cache check and the read.
- `bf skills` treats a file you added at a path a newer version starts shipping as `modified` and keeps it, instead of overwriting it.
- A routine whose action was written but whose run history could not be saved reports `wrote PATH but local run history could not be saved` instead of claiming no action was written.

## [v16.1.0](https://github.com/fmind/brain-framework/releases/tag/v16.1.0) - 2026-09-29

A review release: no format or reply change is required. It fixes the published reply schemas, which rejected some valid replies, and several failure paths; the skills, examples and documentation are clearer and match the code.

Upgrade: `uv tool upgrade brain-framework`, then `bf skills DIR` to update unedited skills. Re-copy the example `weekly-review.py`, `github-history.py`, `git-history.py` or `local-documents.py` if your brain uses one, and merge the new `AGENTS.md` guidance as [Refresh brain instructions](https://fmind.github.io/brain-framework/docs/agents/#refresh-brain-instructions) shows. From 15.x, apply the 16.0.1 steps first: `bf skills` reports skill copies made before 16.0 as `unmanaged` and leaves them; back up any edits and rerun it with `--force`.

### Fixed

- The published reply schemas (`bf schema --kind read-reply`, MCP `outputSchema`) accept every valid reply: an empty period page, such as `bf read 7d` in a new brain, matched two page shapes, so MCP clients that validate structured output failed the read. Reply shapes are now alternatives (`anyOf`), and tests validate every reply against the published schemas too.
- A record's `attributes` and `fields` are returned exactly as stored: replies no longer convert instants inside them to local time or drop an attribute named `time` beside `date`.
- A collection that would change more than 100,000 records fails like any other collection error, recorded in history with its backoff, instead of exiting as invalid input and stopping `bf update`.
- An unrecoverable cache error prints `the search cache is unavailable; run bf build` in the CLI and MCP instead of a traceback.
- A read waiting for another writer to build a missing cache fails after one wait, naming the writer, instead of retrying for up to six minutes.
- A program that cannot start is reported as such even when its log cannot be written.
- YAML files other than UTF-8, such as UTF-16 with a byte-order mark, are rejected naming the file.
- `bf skills --check` reported a skill with a deleted file as `current`: it is now `modified` and lists the file under `edited`; `--force` restores it.
- A reply names an unavailable brain once, worded as its pages report it.
- `bf search` and `bf read` reject `--offset` above 2^53−1 as invalid input (exit 2), like the MCP tools.

### Changed

- `bf update --sensor` and `--routine` suggest close names for an unknown program.
- `bf init` writes the same `AGENTS.md` for every brain (`bf://NAME/...`), so comparing it with a scratch brain shows only template changes. It asks agents to update notes when the user asks or the task authorizes it, and never to edit `memories/`.
- Help text states that `watch` options override `bf.yaml`, that `status --check` also fails on programs that never succeeded and which items `export --kind identities` lists.
- Skills: helpers run from any directory as `python3 "$SKILL_DIR/scripts/NAME.py"`, and through `uv run --project PATH --locked` in a brain that pins its runtime. Version checks follow each skill's `compatibility` instead of a written version. `guarded-write.py` refuses a symbolic link anywhere in the note's path; run it from inside the brain with a relative path. `new-action.py` removes what it created when its write fails. `bf-maintain` explains each `bf status` field, and duplicated rules now live in one guide each.
- Examples: `weekly-review.py` shows times with their offset instead of labelling local times as UTC. `github-history.py` skips a pull request updated during its run, which the next window collects. `git-history.py` requires Git 2.37 and says so. `local-documents.py` keeps the text read before an Office part's element or nesting limit and marks the record `partial` instead of failing the snapshot. The sensors README adds a Calendar agenda configuration, and example brains ignore `logs/`.
- Documentation: Install and update and the four-tool walkthrough move to Start here, Troubleshooting to Reference. The upgrade steps separate the version upgrade from the manual steps of a major release and explain `modified` and `unmanaged` skills. The docs now say that `overdue` means no success within twice the refresh, that a status `cache` of `stale` or `missing` awaits recovery, and that an exact read across brains fails while one of them is unavailable. They also document the state-directory symlink error and the corrected page sizes, log limits and missed-schedule behavior.

### Development

- `scripts/push-and-tag X.Y.Z` pushes `main`, waits for CI on that commit and tags it only after success, whatever shell runs it; `tests/test_release.py` covers the release scripts, and CI validates the release notes on every push.
- The weekly security workflow also installs the package on the next Python release, and scans through the gate's tasks without caches.
- Dependency updates: uv 0.12.20, zensical 0.0.66, sse-starlette 3.5.0, pyjwt 2.15.1.

## [v16.0.1](https://github.com/fmind/brain-framework/releases/tag/v16.0.1) - 2026-09-29

First published 16.0 release. The v16.0.0 tag stopped at verification before publication and remains unchanged: two cache-recovery tests expected a rebuilt cache to get a new inode number, which Linux CI filesystems reuse; they now check the rebuilt cache's integrity instead.

Brain Framework 16 settles the contract so that later releases only add to it. One brain format number covers `bf.yaml` and evaluation suites, one word names each concept, dates and datetimes are separate, routines become general brain programs with hooks and logs, the agent skills ship with the package, and published reply schemas accept added fields while a release gate rejects anything else. Search, the graph, validation and interrupted writes are also more robust.

### Breaking changes

- **Brain format 7.** `bf.yaml` and every `evals/*.yaml` declare `version: 7`. The `schema:` key of `bf.yaml` is now `fields:`: `fields:` declares shared fields and relations, a sensor's `fields:` maps them, a record stores them and a note sets them.
- **Dates and datetimes.** A note states its `updated` day as `date` (`2026-09-29`), never as the UTC instant of its local midnight; claims, exports and backlinks asserted by notes carry `date` too, and `review_due` is a date.
- **OKF review deadline.** Note frontmatter `review_due` and `review_after` are replaced by OKF's `stale_after`, an instant with its timezone: a note is due for review once it is stale. Without it, projects fall due 14 days after their last edit, as before; replies name the deadline's origin as `review_source: stale_after` or `modified`. An OKF note's `resource` URI is one of its identities, like an alias. Every datetime in a reply shows the local offset, to the second (`2026-09-29T09:00:00+02:00`); files, run history and the cache keep UTC.
- **Routines.** A routine is any deterministic brain program declared under `routines:`. `output: log` (the default) keeps its stdout in its log; `output: action` turns its Markdown into a dated action as before. `hooks: [pre-push]` lets `bf run --hook pre-push` run it, and `bf run ROUTINE [ARGS]...` runs one now; both pass arguments and piped input through. Action folders written by routines end in an 8-character suffix.
- **Logs.** Sensors and routines log each run to `logs/NAME.log` in the brain, newest last and bounded to 1 MiB; `bf init` ignores `/logs/` in Git and retrieval never reads it. Status and errors name these brain-relative logs.
- **Freshness.** A scheduled program late by more than twice its refresh is `overdue` (was `stale`); status no longer repeats it as a `stale` boolean. `stale` now only marks results served from a cache a writer is updating. Status reports that cache as `busy`, and an interrupted transaction as `pending_transaction` with its recovery command, instead of failing.
- **Search.** Quoted phrases and `word*` prefixes are honored; results matching more of the query rank higher; tags rank like headings; a nested section's title reads `Note — Parent — Child`; equal scores list the newest first. Replies add `unmatched` (words found nowhere) and `sections` (other matching sections of a note). Periods accept `2026-09-21..2026-09-25` as a scope and a page. The search cache rebuilds itself once.
- **Reads.** A note above 32 KiB opens with its outline, graph context and first 4 KiB; read its sections by ref or follow `next_offset`. Task text keeps its links as `[label](ref)` with brain-relative refs. Listed items and backlink previews show declared single-value `fields`, such as a status, and backlink previews add an `excerpt` of at most 160 characters. Outgoing claims keep up to 20 per relation and 50 in all, so a crowded relation never hides another.
- **Export.** `bf export` prints edges; `bf export --kind identities` lists each note or record that declares names beyond its own address, with every name it answers to. The `edges` positional argument is gone.
- **Skills.** Seven skills become three and ship inside the package: `bf-use` (find, write and track work), `bf-setup` (onboarding, discovery and imports) and `bf-maintain` (integrations and operations). `bf skills DIR` installs or updates them without overwriting edited copies; `--check` reports drift. Helper scripts use paths relative to their skill.
- **Initialization.** `bf init` no longer creates `tests/`, `--full` no longer creates `settings/`, and the generated `AGENTS.md` teaches the new search syntax, `date`/`time` and `bf run`.
- **Limits.** Continuation offsets stop at 2^53−1, the largest integer every JSON client represents exactly.

Upgrade: stop the watcher, rename `schema:` to `fields:` and set `version: 7` in `bf.yaml` and each `evals/*.yaml`, replace note `review_due`/`review_after` with `stale_after`, add `output: action` to routines that write actions, add `/logs/` to `.gitignore`, replace `bf export edges` with `bf export`, read `freshness: overdue` and a note's `date` in scripts, reinstall skills with `bf skills DIR` after removing `bf-learn`, `bf-action`, `bf-scan` and `bf-import`, regenerate native schedules with `bf schedule`, then run `bf validate` and `bf eval`.

### Added

- `bf run` and routine `hooks` for Git and other event hooks; `bf update` names the manual programs it skipped.
- Typed note claims: an OKF note's `fields:` sets declared fields, such as `owner: [person:email/bob@example.test]`, validated like a record's.
- `bf skills DIR [--check] [--force]`.
- `bf export --kind identities`.
- Published reply schemas accept added fields, and `tests/contract/` keeps each schema of the major release: any other change fails the gate.
- MCP tools publish their reply schemas as `outputSchema`; errors name the `search` and `read` tools.
- `bf validate` checks that each enabled program in `sensors/` or `routines/` is an executable file, that declared relations are not written at the top level of frontmatter, and that `?rel=` appears only on `bf://` links.
- Lookup errors name what exists: a missing section lists the note's sections, and a mistyped page, note, sensor or routine suggests close names.
- Status reports each source's `bytes`.

### Changed

- A link naming an undeclared relation keeps its note or record searchable as an untyped link; `bf validate` names the relation.
- `bf validate` accepts a record's provider alias as a link target, checks a relation's `targets` against every identity the target's owner declares, and accepts OKF `sources` whose `resource` describes a population.
- Record commits back up replaced files with hard links and roll back only the files they changed, so an interrupted commit recovers in about a second; commits need about half the fsyncs.
- Exact reads during a commit return within about 3 seconds, marked `stale`, instead of waiting up to 2 minutes; a waiting cache rebuild is no longer starved by readers; a damaged cache is discarded and rebuilt; watch and generated schedules allow 60 seconds to stop.
- `bf eval` ranks each case among the first 50 results, even below its limit.
- `bf --help` lists commands from setup to repair.

### Fixed

- A note dated in a timezone east of UTC no longer shows the previous day.
- An unwritable state directory is named, with `XDG_STATE_HOME`, instead of blaming the brain.
- A completed commit whose cleanup was interrupted no longer blocks reads.

## [v15.0.0](https://github.com/fmind/brain-framework/releases/tag/v15.0.0) - 2026-09-29

Brain Framework 15 reshapes replies for agents: smaller, bounded, described by published JSON Schemas and easier to continue. It ranks sections by their note, collapses duplicate records across sources, pages relationships, adds graph settings and exports, and hardens collection and cache rebuilds. The brain storage format remains `version: 6`; the search cache rebuilds once after the upgrade.

### Breaking changes

- Watch preferences now live in the optional `watch` mapping in `bf.yaml`. `settings/watch.yaml` is no longer read, and `bf schema --kind watch` and `watch.schema.json` are removed; `bf schema` includes watch preferences. Defaults and CLI precedence are unchanged. All commands loading `bf.yaml` validate the section, even when CLI options override it.
- Exact reads above 32 KiB return their note or record text in pages (`text` or `record.text`, with `offset`, `next_offset` and `total_characters`; the first page adds the section `outline` and the graph context) instead of JSON `chunk` strings. Every exact read adds `sha256` (of the whole file) and `modified`.
- Backlink groups always name their `relation` (`links` for untyped links) and preview their 5 newest items with `ref`, `title`, `time`, `kind` and `source`, `status` or `type` only. `bf read REF --rel ROLE` lists a whole group.
- OKF `sources` entries now claim the built-in `cites` relationship instead of an untyped link: backlinks list them under `cites`. `links` and `cites` join `tagged-with` as reserved schema names.
- With one selected brain, result and listing entries omit `brain` and `uri`; records omit `type`, which `kind` already states.
- Search `sources` lists the sources of returned records and those needing attention (failed, `stale` or `never`), with `sources_omitted` counting the rest; replies without items and `memories` scopes keep the full list.
- Search shows one result per URL when several sources hold it, such as a Drive file and its catalog entry; the result's `also` lists up to five other refs, as `bf://` addresses when several brains are selected. One source's records, such as highlights of one document, stay separate.
- Pages and searches end early near 32 KiB instead of 2 MiB, counting a page's summaries such as `changed`; follow `next_offset`. Result and listing titles are previews of at most 200 characters ending in `…`, and home `changed` and `upcoming` previews omit excerpts.
- Period pages (`today`, `7d`, dates) and home `upcoming` omit records of `priority: low` sources; `sources` and `activity` keep their counts, marked `"priority":"low"`, with the page listing them.
- Collection rejects a record whose fields other than `text` exceed 2 MiB serialized, and `bf validate` reports such stored files, so every record reads back; keep bulky content in `text`.
- `bf init` writes a much shorter `AGENTS.md` (about 280 words instead of about 960); authoring rules live in the `bf-learn` skill.
- The `bf-action` handoff check reports `characters` instead of `reply_characters`.

Manual upgrade:

1. Stop the watcher, move the keys from `settings/watch.yaml` beneath `watch:` in your existing `bf.yaml` (indent them two spaces), remove the old file, run `bf validate` and restart the watcher. Remove the empty `settings/` directory only if nothing else uses it. Brains without custom watch preferences need no change.
2. Rename any `schema` field named `links` or `cites`, then run `bf validate`.
3. Update scripts that parse replies: concatenate exact-read text pages while `next_offset` is present and require an unchanged `sha256`; list a backlink group with `bf read REF --rel ROLE` and explain a link with `bf search 'IDENTITY'`; read OKF sources under `cites`; build `bf://NAME/REF` from the reply's brain when you need an address with one brain selected; follow `also` where you expected every copy of a URL; read `memories/SOURCE/PERIOD` to list a low-priority source's records. Validate parsed replies with `bf schema --kind search-reply` and `bf schema --kind read-reply`.
4. Re-copy the upstream `bf-use`, `bf-learn` and `bf-action` skills and any copied hook (`examples/hooks/session-context.py`), which read the new replies.
5. Optionally compare your brain's `AGENTS.md` with a scratch `bf init` and keep what your brain still needs.

```yaml
# https://fmind.github.io/brain-framework/docs/schedule/#watch-preferences
watch:
  interval: 300
  notifications: off
```

### Added

- Role pages: `bf read REF --rel ROLE [--offset N]` and MCP `read(ref, rel, offset)` list every item linking to a note, record or identity through one relationship, 50 per page; an undeclared role exits 2 naming the declared ones, and a `bf://` address with an undeclared `?rel=` reports a problem.
- `broader: ROLE` on a relation field: the parent's role page also lists its narrower roles' items, each keeping its own `relation`.
- `targets: [PREFIX, …]` on a relation field: collection rejects mapped values and typed record links outside them without changing evidence, and `bf validate` reports stored records and note links outside them.
- `bf validate` lists non-failing `warnings` for identities that differ only by letter case; `bf status` lists `warnings` for a record source or authored folder above 80% of the scan limit. Neither changes the exit code.
- `bf build --reproject SENSOR [--dry-run]` re-applies a sensor's current mappings to its stored records without running it or removing records.
- `bf export edges` prints every claim of the selected brains as JSON Lines, offline, for DuckDB or networkx.
- `sensors.NAME.priority: normal|low` in `bf.yaml`: low ranks a source at half weight in search and keeps its records out of period and home lists; applied at query time without `bf build`.
- `bf eval` reports each search case's `rank` and the run's `mrr`; `bf eval --baseline FILE` lists `regressions` and `improvements` against a saved reply.
- Published JSON Schemas for `bf search` and `bf read` replies: `bf schema --kind search-reply|read-reply`, `docs/search-reply.schema.json` and `docs/read-reply.schema.json`. The test suite validates every reply against them.
- Typed claims carry the asserting item's `time`.
- MCP tools gain titles, and the server sends a title and instructions with the orient, find, verify and answer loop, role pages and completeness checks.
- Opt-in `examples/hooks/prompt-context.py` for Claude Code and Codex lists up to three matching note refs per prompt, counts records without quoting them, and stays silent after 2 seconds or on incomplete retrieval.
- The `bf-learn` `scripts/guarded-write.py` helper replaces a note from stdin only while it still has the `sha256` of the agent's read.
- Add an explicit `f` refresh shortcut to `bf watch` (`u` remains an alias): reload configured sources and check due work now, retaining one follow-up request during an active update while respecting pause, selectors and retry timing.
- Add a reviewed GitHub history sensor and backfill walkthrough: selected-branch commits (one year of `main` by example), all-age issues and pull requests, incremental refresh and bounded pagination that preserves evidence on failure.

### Changed

- The 100,000-entry scan limit applies per source directory under `memories/` and per authored folder, and its error names the crowded directory with a remedy.
- `bf build` and automatic rebuilds fill `.bf/index.sqlite.new` beside the live cache and replace it atomically: searches and collections continue meanwhile, and an interrupted build leaves the live cache intact. Readers hold a brief cache lock while connected, so a replaced cache never attaches the new one's write-ahead log.
- `bf update` and `bf watch` recover an interrupted record transaction before refreshing the cache; reads still refuse and name `bf build` or `bf update`.
- Rewording a schema field's `description` or `examples` no longer rebuilds the cache; adding `broader` or `targets` does.
- Rolling back an interrupted transaction syncs the source directory once instead of once per record.
- Mapping errors at collection name the record's zero-based position.
- The skills describe the new replies, role pages, low-priority sources, rank-aware evaluations, scan warnings and graph settings.
- Synchronize the project description across the README, documentation, package metadata, CLI help and GitHub About to “🧠 Brain Framework: from information to informed actions.”

### Fixed

- A section now ranks under its note's title as well as its heading, so "Atlas next actions" finds Atlas's own Next actions before other notes that mention Atlas; a query matching only a note's title still returns the whole note.
- The snapshot shrink guard counted the catalog left by an interrupted commit, letting a truncated listing remove most records; recovery now runs first.
- A routine retried after its run history failed to save no longer writes a second action for the same day.
- Temporary `.write-*` files left by killed writes are removed by the next transaction or recovery of their source.
- Reject ambiguous GitHub pagination and PR details that move outside the requested window, preserving saved evidence instead of accepting an incomplete or out-of-scope collection.

## [v14.0.0](https://github.com/fmind/brain-framework/releases/tag/v14.0.0) - 2026-09-28

Brain Framework 14 is the first stable release. It settles the brain format, the CLI and MCP reply contracts and the execution boundaries so that later releases can extend them without breaking them. Collection only runs in one explicitly selected brain, stored evidence resists partial provider failures, retrieval replies share one shape, and the guides, skills and examples are tested against the release they document.

### Breaking changes

- **Formats:** `bf.yaml` (format 6) and evaluation suites (format 5) require `version`. YAML follows the 1.2 core schema: `yes`, `no`, `on` and `off` are strings, `017` is decimal and `1:30` is text.
- **Notes:** in projects, concepts and `ACTION.md` notes, only `deprecated` closes a note: it ranks last and leaves home, tasks and review reminders. Aliases must be namespaced identities such as `repo:github.com/owner/name`, titles are limited to 4,096 characters, and page paths (home, folder roots, `tasks`, periods, `tags/*`, `memories/*`) cannot be entities or aliases. Other Markdown, such as action inputs and outputs, is ordinary: only valid `title`, `type`, `status`, `updated`, `summary` and `description` apply, and it declares no entity, alias, tag, frontmatter link or review date.
- **Links and identities:** body links keep their written form, so BF links must percent-encode spaces and `%`. A note alias equal to a record ref is an ambiguous identity, an entity or alias whose first path segment contains `:` is invalid, `tagged-with` is reserved for tag membership, and periods accept ASCII digits only. `bf://NAME/source:id` always names a record.
- **Records:** aliases must be namespaced identities, ids must fit a BF address (7,988 characters once percent-encoded), URLs are limited to 8,192 characters without control characters, and lone surrogates are rejected. `bf validate` reports stray files and hidden `.json` files under `memories/`.
- **Selection and execution:** a bare `--brain NAME` or `BF_BRAIN=NAME` resolves through your registry first and fails when the enclosing brain or its references claim that name for another directory. `update`, `collect`, `watch` and `schedule` act on exactly one brain (`--brain`, `BF_BRAIN` or the enclosing brain) and never fall back to all registered brains; outside a brain they fail. For them, a name must be registered or be the enclosing brain's own: a referenced brain or a directory below the working directory fails with `neither registered nor the enclosing brain`. An enclosing `bf.yaml` must be a regular file owned by you, and the user registry `~/.config/bf/config.yaml` must be a regular file. Program executables must be bare command names or normalized `sensors/` or `routines/` paths.
- **Collection:** a snapshot that would remove more than half of an existing catalog and more than 10 records fails without changing evidence, like an empty snapshot; `bf collect SENSOR --allow-removal` accepts one such run. Failed sensors and routines retry after 1, 2, 4… minutes, capped at their `refresh`, instead of every cycle; `bf collect` retries a sensor at once. A concurrent `bf update` of the same brain waits up to 10 minutes before failing, and a program whose background process keeps its stdout open fails one second after it exits.
- **Replies:** `problems` is always a list of objects with `error` and optional `brain` and `file`. `bf build` and `bf update` report skipped files as `skipped` and exit 1 when files were skipped; `bf status` reports its cache as `cache`, and its source and routine entries use `state`, `last_collected`, `window`, `last_run`, `last_success`, `failed`, `failures` and `log` instead of the removed `enabled`, `configured`, `run`, `success`, `start` and `end` keys. `bf update` replies with one brain object whose `sensors` and `routines` lists are always present. Search no longer returns `more`; `next_offset` alone signals continuation. All instants are canonical UTC ending in `Z`; absent ones are omitted, except in `bf watch --json` rows, where `success` and `next_due` are then empty strings. A source without dated records has no `latest`. `bf watch --json` no longer emits `observing`.
- **Reads and searches:** exact replies longer than 65,536 characters are JSON chunks from offset 0, and a non-zero offset on a smaller reply fails. The MCP `read` tool has no `brain` argument; use a `bf://NAME/...` address. Both MCP tools reject unknown arguments, such as that `brain` or a misspelled `scope`, instead of ignoring them. Identity scopes include the identity's owning note. An unknown `memories/SOURCE` scope is not found, and a segment below it other than a period, `undated` or a record file is invalid. Malformed refs, `bf://NAME` without its trailing slash and queries without any word exit 2, including a search query shaped like a malformed BF address. Retrieval needs write access to `.bf` and rejects a `.bf` that is a symlink, not a directory or owned by someone else.
- **Evaluation:** `bf eval` validates every case while loading, including scopes (a `0d` window is invalid), read refs, `bf://` addresses and query words, and fails the whole run on an invalid suite.
- **Scheduling:** a relative `bf schedule --output` resolves against the brain, and generated systemd units no longer cap the whole update at 45 minutes.
- **Removed legacy:** JSON Lines record storage handling, the `active`, `paused`, `blocked`, `done` and `archived` statuses, the `/logs/` entry in new brains' `.gitignore` and the hand-written systemd example.

### Added

- Sort `bf watch` and `bf status --watch` by name, last success, returned items, state, next due time, changes, duration, output bytes, refresh interval or kind, with reverse order, first/last navigation, an item-count column and a keyboard guide. JSON watch rows expose `records`, the last successful sensor run's returned count.
- `bf schema --kind` exports offline editor schemas for brain, watch, registry and evaluation files, generated and checked from the runtime models.
- `bf eval` assembles chunked exact reads and verifies their digest before checking read cases.
- `bf validate` names the file of every problem, flags capped lists with `problems_truncated` and `unresolved_truncated`, checks embedded images and accepts page addresses (home, folders, periods, tags, sources and record files) as link targets. Project notes resolve a leading `/` from `projects/`, as concepts already did.
- Registered brains that are absent on this machine appear in search and read `problems`, and `bf status --check` fails on them and on `brains:` references that retrieval cannot include.
- `memories/SOURCE/undated` is a search scope, like the page of that source's undated records.
- The bf-learn evidence helper reads and assembles chunked exact replies with `read REF --brain BRAIN`.
- Documentation for shell completion, refreshing a brain's `AGENTS.md` from the installed template, and every enforced limit.
- The package declares `Development Status :: 5 - Production/Stable`.

### Changed

- Search matches words in one Unicode compatibility form, so `ß`, ligatures, full-width letters and decomposed accents are found, while excerpts keep the source's characters. The search cache rebuilds itself.
- Collection parses only the record files it replaces or removes and syncs each directory once per transaction, and exact reads of absent records reuse a ready cache.
- Every lock follows the physical brain directory, so different paths to one brain share update, watch and program locks, and a busy program names itself.
- Sensors and routines receive `BF_BRAIN` set to the executing brain, so nested `bf` calls read that brain.
- For scheduled window sensors, a manual `bf collect` after a pause no longer moves the resume point past the uncollected gap.
- The `AGENTS.md` written by `bf init` states execution authority, one-brain selection, incomplete-result boundaries, `deprecated`-only closing, chunked reads and multi-brain `uri` reads. New brains' starter evaluation reads `bf://NAME/concepts/welcome.md`, and new `bf.yaml` files omit redundant defaults.
- The watch dashboard keeps the selected row, program states and failure details visible from 80 columns, shows the sort field and direction in the panel title, applies batched keys in order and redraws only on change or once a second.
- Documentation is organized around first use, everyday tasks, collection and reference. Guides install with `uv tool install --python 3.14 brain-framework`, copy examples from the release tag matching `bf --version`, and name action folders `YYYY-MM-DD_topic-SUFFIX`; `tests/test_guides.py` runs the getting-started guide and checks every reply it shows.
- Skills describe the 14 contracts, use `## Decision {#decision}` in project and action templates, and are tested for the commands, options, anchors and Python 3.11 standard library they rely on.
- Example sensors stay within the record limits, map provider fields explicitly and print content-free reasons for their failures. The Calendar sensor keeps cancelled events as `Cancelled: SUMMARY` at their original time, the Git history sensor reads branches, tags, remotes and `HEAD` and skips unreadable folders, and the team example is a `bf` and `git` walkthrough.
- Ordinary Markdown whose foreign frontmatter is not valid YAML stays searchable without it, a block that never closes is searched as text, and Emacs `.#NAME` lock files beside authored files are ignored, so none of them fails `bf build`, `bf update` or `bf validate`. A routine that already wrote today's action skips its rerun while that folder holds any file, even an editor lock.
- Collection replaces, or a snapshot removes, a stored record file that holds its own id but breaks the current record rules, such as a display-name alias or an over-long title; misnamed files still fail it.
- Diagnostics name what to fix: duplicate YAML keys give their position, unknown watch settings and undeclared field mappings name their key, search errors name `QUERY` or `--scope`, `bf register` names a missing directory or `bf.yaml`, unavailable or unloadable brains are named once by their registered or `brains:` name, and `bf init` asks for `--name` when the directory name cannot form one.
- `bf schedule` without `--output` warns that its install commands name files it did not write. When an update fails only because the search cache skipped files, `bf watch` says so and sends no collection-failure alert. JSON watch rows are documented key by key.
- The bf-action helper accepts a symlinked brain root, the bookmark inventory helper distinguishes a reached limit from an unsupported export, and example sensors name the repository, folder or limit behind a failure; the Calendar sensor keeps very large events within the 1,000-link record bound. Guides write GitHub identities in lowercase and install skills and the watch demo from the release tag.
- Development uses `uv run --locked`, hermetic UTC tests under pytest 9 strict mode, a 95% branch-coverage floor, generated third-party notices (`mise run generate:notices`) and a dashboard screenshot rendered from the fictional demo (`mise run generate:screenshot`), each checked by the gate. CI runs shared checks once and tests on all four platforms; the weekly security workflow also installs the package with the newest allowed dependencies, and the package check completes an MCP stdio handshake with the installed package.

### Fixed

- Body links to identities or records with accents, spaces or `%` now match their frontmatter form in backlinks, reads, scopes and validation.
- Removing or retyping a schema field no longer hides the stored records that carry it.
- A note whose H1 is followed by a `---` rule, or that starts with a byte order mark, keeps its frontmatter; link validation compares exact file names on case-insensitive disks; lead and anchor parsing is linear on adversarial notes.
- Period and source pages report each problem once; previews order ties like their continuations; deep search offsets no longer compute skipped excerpts; reading a typed link to an unowned identity works.
- `bf eval` names the suite and case of an invalid case before any retrieval; JSON replies are UTF-8 whatever the locale; a non-UTF-8 note is reported as such.
- Collection survives files that vanish during a scan, names linked intermediate folders and the directory that exceeds the depth limit, and ignores empty or relative `XDG_*` values.
- A successful sensor whose background helper inherits stderr no longer times out; clock corrections no longer delay scheduled programs; malformed usage events no longer interrupt status.
- The watch dashboard no longer hides the selected row when programs outnumber the screen or truncates states and counts on 80–128 column terminals.
- The example hook, routine and sensors run on Python 3.11 again; example Calendar and Drive sensors reject malformed continuation tokens; the local documents sensor names unreadable, damaged or encrypted files.
- A file or folder name that is not valid UTF-8, such as a Latin-1 name from an old archive, or that holds a backslash, is reported as `file name is not valid UTF-8 or contains a backslash; rename it` instead of failing search, read, status, build and validate for the whole brain.
- Documentation navigation and wide tables are keyboard accessible, and release notes reject empty or duplicate sections.
- Remove a breaking-change bullet copied by mistake into historical release sections.

### Security

- Brain names resolve through the owner's registry before the working directory, an enclosing `bf.yaml` is trusted only when you own it, and registered brains are selected for retrieval only, so an untrusted checkout cannot run its programs under your name.
- Program environments also drop `SHELLOPTS`, `BASHOPTS` and `PS4`.
- The private state root is created with mode 0700; the search cache is opened through one no-follow descriptor, created with mode 0600 and read with SQLite's defensive settings.
- The example session-context hook never prints collected record refs, and a non-interactive `bf status --watch` no longer suggests the program-executing `bf watch --json`.
- A search cache that bf did not create in place, such as one copied, cloned or extracted with a brain, or one holding triggers or views, is rebuilt from the brain's files, so rows no file supports never reach retrieval. The cache is bound to its file's inode number, which a remount keeps.
- CLI JSON replies, MCP tool text and `bf watch --json` rows write DEL and C1 control characters from collected text as JSON escapes, so a terminal cannot act on them; the decoded values are unchanged.
- Collection errors, run history and `bf status` no longer quote an undeclared link relation from sensor output.

## [v13.0.2](https://github.com/fmind/brain-framework/releases/tag/v13.0.2) - 2026-09-27

First published 13.0.x release. The v13.0.0 and v13.0.1 tags stopped at verification before publication and remain unchanged. CI and scheduled security checks now pin mise 2026.9.15. Terminal-restoration checks exercise line input before comparing all settings, accounting for macOS kernel state while retaining cancellation and restoration assertions.

Brain Framework 13 adds continuous collection, independent record files, complete retrieval and practical workflows from evidence to decisions. Before upgrading from 12, follow the manual steps below on a backed-up copy with collection stopped.

### Changed

- Use the same product description in package metadata, CLI help, the README and documentation: 🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.

- **Breaking storage change:** `bf.yaml` uses format 6. Each source record lives at `memories/SOURCE/SHA256_ID.json`, replacing monthly JSONL catalogs. Record IDs and `source:id` refs stay stable; source pages replace the `partitions` inventory with paginated records. Retrieval evaluation files remain format 5.
- **Breaking execution change:** remove machine collection permissions and sensor content-trust settings. Registration stores names and paths only; explicit collection/update/watch commands run enabled programs in selected roots, never referenced brains. Remove obsolete registry `collect` fields, sensor `trust` fields and `--collect` options. Retrieved content remains untrusted evidence.
- **Breaking note contract:** projects, concepts and canonical action `ACTION.md` notes require OKF structure, a nonempty `type`, and `draft`/`stable`/`deprecated` lifecycle statuses. Work progress belongs in the body and checkboxes; attachments may remain ordinary Markdown. Routine output is validated before an action is created.
- **Breaking reply contract:** remove `collect` from init, registration and status replies, `trust` from source metadata, and `external` from retrieval and evidence-helper replies. Pages include record excerpts consistently; hooks and routines reference collected records without copying their text into authored context.
- Review reminders use local file modification time, optional `review_after` days or a `review_due` date. Replies explain deadlines and newer evidence without claiming a review occurred. File copies/checkouts can reset modification times; explicit deadlines remain portable.
- Routine actions use unique session suffixes, with daily suppression local to each collecting clone. Shared brains can merge independent records and actions independently; conflicting evidence still requires review.
- Rewrite guides around a runnable decision example, first sensors, schema mappings, team setup and expected results. Keep the README concise and expand advanced reference pages, privacy boundaries and recovery instructions.
- Refresh compatible Python dependencies and keep task output concise while retaining diagnostics, security findings, test counts and coverage totals.

### Added

- `bf watch` follows collection in a full-screen terminal dashboard with keyboard navigation, pause/cancellation and last-run changes. `bf status --watch` observes without execution; `watch --json` streams snapshots. A second interactive watcher observes the active collector.
- Brain-owned `settings/watch.yaml` controls check/display periods and generic desktop failure/recovery alerts, with optional success notifications and cooldowns.
- `bf schedule` generates inspectable systemd, launchd or cron definitions and installation/removal commands without activating them. Repeatable `--sensor` and `--routine` options select programs for update, watch and schedule while retaining enabled/refresh rules.
- Window sensors can use a separate `reconcile` cadence to revisit older evidence. Collection/status replies report requested windows, reconciliation, elapsed seconds, output bytes and record change counts.
- `bf read tasks` lists open checkboxes with source sections, line locations, complete counts and pagination. Weekly reviews summarize tasks without duplicating the original checkboxes. `bf://NAME/tasks` is reserved for this computed page.
- Browse tags, follow brain-qualified tag pages and search explicit tag membership with `--scope bf://NAME/tags/LABEL`. Tags retain their originating notes and use bounded, validated labels.
- `bf-setup`, `bf-scan` and `bf-import` guide onboarding, explicitly scoped discovery and selected imports. New brains include technical-test and retrieval-evaluation folders with immediately runnable starter cases.
- Action helpers create independent sessions and check Context/Resume budgets before handoff. Learning guides cover reviews, evidence retention, sharing and conflict resolution.
- Runnable fictional context-hub, watch and two-contributor examples demonstrate integrations and collaboration. A highlights sensor imports selected passages with URLs, locators and annotations kept separate from source text.
- Deterministic retrieval evaluations under `evals/` join the full gate without models or providers. Package checks exercise wheel and source archives outside the checkout; the manual benchmark validates every measured result.

### Fixed

- Search/listing continuations expose remaining results. Oversized exact replies return lossless JSON chunks with SHA-256 verification; outgoing graph previews mark omitted claims. Empty searches retain failed/never-collected source coverage.
- Symlinks and special files are skipped and reported without following their targets or hiding other evidence. Incomplete exact reads fail visibly; pending recovery journals still block retrieval until repaired.
- Invalid YAML identifies the file, line and column without quoting private content. Persisted-record diagnostics redact provider-controlled keys. Status reports a broken brain while retaining reports for other selected roots.
- Reject unresolved Markdown merge markers and foreign-namespace note aliases during validation/indexing. Preserve source links on every canonical note type and resolve typed links to tag pages correctly.
- Preserve existing evidence-capture destinations, including dangling symlinks. Session hooks constrain metadata to its owning brain; weekly reviews follow all project continuations and reject incomplete retrieval.
- Keep Git history collection within selected directories and half-open time windows, including in-window commits with out-of-order dates.
- Keep starter absent-evidence evaluations stable as notes grow. A malformed optional registry no longer prevents operations on an explicitly selected brain path.
- Retry failed scheduled programs on the next cycle even when an earlier success is still within its refresh interval. The watch display shows these retries as immediately due.
- Remove empty code-line links from the documentation's keyboard navigation and accessibility tree.

### Manual upgrade from 12

1. Stop all writers. With version 12, run `bf build` and `bf validate`, then back up the whole brain, including ignored memories and attachments. Git alone is not a full backup. Work on a separate copy with version 13 installed in a separate environment.
1. Convert each nonblank line of `memories/SOURCE/*.jsonl` into `memories/SOURCE/SHA256_ID.json`, using the lowercase SHA-256 of the UTF-8 record ID. Preserve every field and reject duplicate IDs, duplicate JSON keys, symlinks and unexpected files. Write to a fresh directory, verify counts, then replace only the copy's `memories/`; never install partial output. Brains without collected records skip this step.
1. Set `version: 6` in the copied `bf.yaml`; retrieval suites stay at format 5. Remove sensor `trust` fields. Explicit collection/update/watch commands now run enabled programs in selected roots; review their configured argv before execution.
1. Give projects, concepts and canonical action notes a nonempty `type` and `draft`, `stable` or `deprecated` status. Move work states into the body/task list; preserve OKF sources and verification metadata. Replace aliases claiming another brain's namespace with explicit links.
1. Back up the user registry, then remove registration `collect` fields and obsolete `--collect` host flags. Update installed skills and restart hosts. Reply consumers must follow `next_offset`, verify/reassemble exact-read chunks and stop expecting removed `external`, `trust`, `collect` or source `partitions` fields. Routine action paths now include a UUID suffix.
1. On the copy, run `bf build`, `bf validate`, `bf eval` and technical tests. Compare record counts, search a known decision and read its reason and supporting record. Resolve `problems`/`stale` and check source freshness separately before switching collectors/hosts and resuming the authorized schedule. If checks fail, retain the original version-12 runtime and backup together; do not open format-6 data with version 12.

## [v13.0.1](https://github.com/fmind/brain-framework/tree/v13.0.1) - 2026-09-27

Unpublished tag. The macOS terminal tests compared a transient kernel flag before processing the next input. See v13.0.2 for the full release notes and portable terminal checks.

## [v13.0.0](https://github.com/fmind/brain-framework/tree/v13.0.0) - 2026-09-27

Unpublished tag. See v13.0.2 for the full release notes and corrected CI toolchain.

## [v12.0.2](https://github.com/fmind/brain-framework/releases/tag/v12.0.2) - 2026-09-25

First published 12.0.x maintenance release. The v12.0.1 tag stopped at the CI gate before publication and remains unchanged.

A maintenance release for reliable absence checks, documentation and release verification. Brain configuration and retrieval suites remain at version 5; no manual upgrade is needed.

### Fixed

- Schema checks use their own scratch directory, so a concurrent clean documentation build cannot delete their intermediate files.
- Collection bounds saved coverage by its successful run and the current observation time. A future window left in run history no longer keeps successful scheduled collection permanently stale.
- A missing read no longer proves absence when a directly referenced brain is unavailable or identity lookup skipped invalid evidence. Retrieval evaluations expecting an empty answer fail visibly until the incomplete scope is repaired.
- Future-dated notes appear in the home page's upcoming items without displacing recent changes.
- PDF converter process tests allow interpreter startup on busy machines while still asserting output limits, timeouts and process cleanup.

### Changed

- CI and release publication share the four-platform verification workflow. PyPI and GitHub receive the exact distributions that passed isolated installation tests, with hash comparisons before and after publication.
- Wheel and source-distribution checks install locked runtime dependencies outside the checkout and exercise initialization, validation, search, exact reads, status and retrieval evaluation.
- Development tasks and hooks use native tools with separate import sorting and formatting, a configured coverage floor, strict rendered documentation links and disposable schema generation.
- Refresh the README, documentation overview and branding; clarify which sensor integrations ship as reviewed examples and how to create others.

## [v12.0.0](https://github.com/fmind/brain-framework/releases/tag/v12.0.0) - 2026-09-25

Brain Framework 12 ranks notes and records in one search query and answers several times faster; BF links carry only a relationship. On a personal brain of 52,000 records, full cache builds take 13 s instead of 69 s, searches and reads 0.3–0.4 s instead of up to 1.8 s, and the cache shrinks from 357 MB to 239 MB. The `bf.yaml` and retrieval suite formats stay at version 5, but notes that use removed link attributes or frontmatter `fields` need the manual upgrade below. The search cache rebuilds automatically.

### Changed

- **Breaking**: search ranks every item containing any of the words in one BM25 query. The former all-words pass let long records that happened to contain every word fill the results before a note matching most of them; BM25 already ranks fuller matches higher.
- Only projects, concepts and each action's `ACTION.md` receive the note ranking boost. `concepts/index.md`, `concepts/log.md` and an action's `inputs/` and `outputs/` rank like evidence.
- A query shaped like an identity (`re:invent`, `python:3.14`) that no item is, names or links to ranks its words instead of returning a silent empty answer; the reply carries `"identity": "unknown"`.
- Record field values are searchable words without their JSON keys, so a word such as `author` or `kind` no longer matches every record with that field.
- **Breaking**: a BF link accepts only `?rel=ROLE`. The `subject`, `evidence` and `asserted-by` query keys and typed link attributes are removed; a link's subject is its note's `entity`, otherwise its file, or the collected record. Claims in `relations` and `claims` are `subject`, `relation`, `target` and `origin`: the `evidence`, `asserted_by` and `attributes` members are gone.
- **Breaking**: note frontmatter `fields` no longer declares relationships; it is ordinary frontmatter data.

### Fixed

- Searches no longer compute an excerpt for every matching passage before ranking: only returned passages get one. A four-word query over 50,000 records took 1.9 s in excerpts alone.
- `bf.yaml` is parsed once per content within a command, with libyaml when available, instead of several times per search and once per indexed file during a rebuild.
- The relationship cache no longer copies an eight-column key into each of its indexes.
- Backlinks, identity searches and project review signals look up links and relationships through indexes instead of scanning every link of the brain for each relationship group.

### Upgrade

1. Find removed link attributes: `grep -rnoE 'bf://[^) ]*\?[^) ]*' projects concepts actions | grep -vE '\?rel=[a-z0-9-]+(#[^) ]*)?$'`. Keep `?rel=ROLE`; move a relationship of another entity into that entity's note, and keep supporting references as ordinary links in the same section.
1. Find frontmatter relationships: `grep -rln '^fields:' projects concepts actions`. Replace each identity value with a `[label](bf://NAME/path?rel=FIELD)` link in the note body.
1. Consumers of `relations[].evidence`, `asserted_by` or `attributes` read `relations[].origin` instead.
1. Run `bf validate`: remaining removed attributes are reported as invalid BF links.

## [v11.1.1](https://github.com/fmind/brain-framework/releases/tag/v11.1.1) - 2026-09-25

First published 11.1 release. The v11.1.0 candidate stopped at the release test gate before publication; its tag remains unchanged.

Brain Framework 11.1 adds optional decision workflows to the agent skills and hardens collection, retrieval and validation after a full review. The brain format is unchanged: `bf.yaml` and retrieval suites stay at version 5.

### Added

- Decision workflows in the `bf-action` and `bf-learn` skills, loaded on demand: a small working context in each action (`## Context {#context}` of at most 300 words, six refs and 4 KiB, and `## Resume {#resume}`), decisions with alternatives and expected outcomes, conditional intentions, explicit unknowns, belief revision with a declared `supersedes` role, dependency review bounded to two hops and ten dependents, procedures learned from outcomes and reviewed transfer to another brain.
- `skills/bf-learn/scripts/evidence.py`, a standard-library helper for Python 3.11 or later: `capture` retains one exact `bf read` with its digest and limitations, and `compare` returns a compact `changed`, `unchanged` or `unknown` verdict for a new read. It reads stdin only and never opens a brain, runs a provider or uses a model.
- An [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) page covers the skills, the everyday loop, resuming actions, decision workflows and evidence captures.
- The example brain demonstrates a decision review: a fictional policy, a superseded decision, an intention, an unknown, a draft procedure, a prepared transfer and retrieval cases for each.

### Changed

- Search excerpts are one line and start below a section's heading, which the result title already names. The search cache rebuilds automatically.
- Exact record reads and search coverage report the same freshness as `bf status` and source pages: a trusted scheduled sensor that never succeeded is `never`. An unreadable registry grants no trust and leaves freshness `unknown`.
- An invalid `--scope`, `--since` or `--until` exits 2 as invalid input, like other command-line errors. A closed terminal (SIGHUP) cancels like SIGTERM and kills running providers; `nohup` keeps it ignored.
- `bf status --check` fails only for scheduled sensors this machine runs, as documented; a failed manual collection is still reported with its log.
- Registered brains win over a same-named directory below the working directory for `--brain NAME`.

### Fixed

- The brain writer lock follows the brain directory's device and inode, so bind mounts and differently spelled paths of one brain no longer commit concurrently. Writers must share one private state directory.
- A state directory below a linked ancestor, such as `/home` linked to `/var/home`, no longer breaks every command; the state root itself still may not be a link.
- A registry entry with a relative path is rejected instead of granting collection trust to whatever that path means in the working directory.
- Relative `PATH` entries no longer let a bare command resolve inside the brain, and every `PYTHON*` variable plus Java, Lua and `GCONV_PATH` startup variables are removed before a sensor or routine starts. Brain executables may exceed 1 MiB.
- Invalid sensor output is reported by record position and field name only: keys the provider printed no longer reach errors, logs or run history.
- An empty snapshot no longer erases a non-empty catalog; the run fails and keeps the records. Delete `memories/SOURCE/` to clear a source deliberately.
- A backfill whose `--until` lies in the future no longer blocks later scheduled successes: coverage never extends past the run.
- A routine run skipped because today's action exists no longer loses its window; the next written action covers it.
- A stored revision claiming a modification after it was first observed, such as a file with a future mtime, no longer freezes a record against newer collections.
- A note dated `0001-01-01` or `9999-12-31` is invalid instead of crashing searches and pages; a damaged cache reports "run bf build" instead of a traceback.
- `read` keeps answering from the other brains when one selected brain has an invalid `bf.yaml`, and reports it under `problems`.
- An alias claimed by several notes no longer merges their backlinks in scoped searches and identity pages.
- Identity searches include links to a note's sections, like scoped searches and backlinks.
- Period pages order `changed` items by modification time across brains.
- `memories/SOURCE/PERIOD` for an unknown source is a missing page instead of an empty answer.
- Reading an identity or an absent source no longer waits for a running writer.
- A record id too long for a BF address still reads, with its backlinks reported as unavailable.
- Headings without word characters get an addressable `section` slug; explicit anchors cannot end in `.md`; a link to a non-Markdown file whose name contains `#` validates.
- The session hook and the weekly review show local dates, and the weekly review writes one paragraph per line and fences record ids so that none can become a link.
- The exit-code test accepts colored usage errors, as CI terminals print them.

## [v11.0.0](https://github.com/fmind/brain-framework/releases/tag/v11.0.0) - 2026-09-25

Brain Framework 11 turns listings into pages, gives actions a resumable page and schedules deterministic routines next to sensors. Search takes words and one scope; everything else is a page that `read` resolves. The brain format changes: `bf.yaml` and retrieval suites move to version 5.

### Changed

- `bf read` without a ref returns the home page: active and blocked projects, the latest actions, notes changed in 7 days, record activity per source in 24 hours, items in the coming 7 days, and scheduled sensors or routines that need `attention`. Projects are marked for `review` when their note is older than 14 days or items dated after it link to them (`new_links`), and show open `tasks` with the `next` one.
- `read` resolves pages: folders (`projects`, `concepts`, `actions` and subfolders), periods (`today`, `yesterday`, `YYYY-MM-DD`, `YYYY-MM`, `12h`, `7d`, `2w`) with items modified in the period and each source's share, `memories`, `memories/SOURCE` with its partitions, and `memories/SOURCE/PERIOD`, `snapshot` or `undated`. Pages combine the selected brains, stay bounded (50 period or source items, 200 notes per folder, 20 per section) and report totals; `bf://NAME/` and `bf://NAME/PAGE` select one brain.
- Whole note and record reads include `backlinks` across the selected brains, grouped by explicit relationship with claim explanations, and `claims` whose explicit subject is the item. An identity without an owning note reads as a page of what links to it. `bf read actions/FOLDER` returns ACTION.md with the action's files and the projects it links to.
- **Breaking**: `bf search QUERY [--scope SCOPE] [--limit N]` requires words or an identity. A scope is a folder or file, a period or an identity. `--since`, `--until`, `--source`, `--type`, `--status`, `--recent`, `--changed-since`, `--current`, `--relation`, `--target` and `--subject` are removed. MCP `search` takes `query`, `scope` and `limit`; MCP `read` accepts an empty ref for the home page.
- **Breaking**: `bf.yaml` version 5 declares `routines:`, deterministic programs that `bf update` runs after the sensors with the same trust, process boundary, timeout, output bound and private log. A routine's Markdown is validated and written as `actions/YYYY-MM-DD_NAME/ACTION.md`; an existing action is never replaced, empty output writes nothing, and a failure writes nothing and keeps it due. Routine names are action slugs distinct from sensor names. `bf status` reports routines, and `--check` fails on stale or failed ones.
- **Breaking**: retrieval suites use version 5. A case is a search (`query`, optional `scope` and `limit`) or a read (`read`: a page, note, record or identity), checked with `expect`, `forbid`, `text` and `empty`; a read that finds nothing is empty.
- Sensors declare `trust`: `owner` for text the brain's owner writes, `external` (the default) for mail, chat, invitations, issues, feeds and other third-party text. Pages show external records by title and ref without excerpts; searches, exact reads and backlinks label them `external`; source coverage reports each source's trust, and undeclared historical sources are external.
- `bf status` no longer lists `review`; project review moved to pages. Process errors name the program rather than a collector, since sensors and routines share the runner. The search cache rebuilds automatically.

### Added

- The `bf-action` skill starts, resumes and closes one action, a single session of work, when the user asks for it; the action template moves there from `bf-learn`.
- `examples/routines/weekly-review.py` renders a weekly review action from `bf read` pages, with a fake-`bf` test and a README contract. It names external items by ref only.
- `examples/hooks/session-context.py` prints a short context for the current repository at agent session start (project, review signal, next task, linked evidence), and nothing when unavailable.

### Fixed

- A manual backfill that ends before a sensor's recorded coverage no longer replaces that coverage, moves its resume point or marks the sensor fresh; a contiguous backfill extends coverage backwards without counting as a fresh success.

### Manual upgrade from 10

1. Pause scheduled writers. Install the same Brain Framework 11 build for every CLI, MCP host, project environment and scheduled writer, and restart long-running MCP readers.
1. Change `bf.yaml` to `version: 5`. Every sensor is now `external` by default: add `trust: owner` to the sensors whose text you write yourself, such as local Git history or your own documents.
1. Change every suite under `evals/` to `version: 5`. Rewrite cases that used removed fields: a time window or filter becomes a `read` of a page (`7d`, `2026-09`, `projects`, `memories/SOURCE`) or a search `scope`; a `relation`, `target` or `subject` case becomes `read: IDENTITY` with `expect`, and `text` naming the role when it matters.
1. Replace scripts, routines, skills and agent instructions that call removed search options: pages for listings and timelines, `bf read IDENTITY` for backlinks and claims, `--scope` for bounded searches. Update the `AGENTS.md` that earlier `bf init` generated, and reinstall `bf-use`, `bf-learn`, `bf-maintain` and the new `bf-action`.
1. Optionally move scheduled review scripts into `routines/` and declare them under `routines:`. Review them like sensors before a trusted machine runs them.
1. Run `bf build`, `bf validate` and `bf eval`; compare the home, `projects` and period pages with your previous listings, then resume the scheduled writer.

No migration tooling or legacy command surface is included. Runtime dependencies are unchanged.

## [v10.0.0](https://github.com/fmind/brain-framework/releases/tag/v10.0.0) - 2026-09-25

### Changed

- Declare directly related brains in `bf.yaml` with stable names and relative, absolute or home-relative paths. Search, read, MCP and evaluations include direct references without recursive discovery or required global configuration. Missing references report incomplete scope; conflicting names are excluded.
- `bf init` defaults to no global registration or collection trust; explicit `--collect` opts in. Maintenance commands do not expand references, and references never authorize sensors. Retrieval suites accept qualified BF addresses to distinguish same-named files across brains.
- Add portable `bf://brain/path#section` addresses, explicit note `entity` identities and typed note `fields`. BF link queries encode declared relationships and attributes, with explicit subject/evidence/attribution and an immutable origin in the derived SQLite projection.
- Add selected-brain alias federation, incoming `target` and outgoing `subject` filters, and relationship explanations through CLI, MCP and retrieval suites. Ambiguous aliases do not merge, external URL queries stay opaque, and foreign links never broaden brain selection.
- Headings support stable `{#anchor}` identifiers. Validation checks local BF addresses and reports foreign unresolved targets. New brains include a small relationship vocabulary and agent instructions for links.

- Brain format 4 restores explicit `schema` fields in `bf.yaml`: descriptions, strict types, cardinality, validated examples and typed relationships. Sensors map their record output with JSON Pointers or constants; normalized values are stored in record `fields` and included in offline search.
- Typed relationships form a disposable, evidence-backed SQLite graph. CLI, MCP search and retrieval cases accept `relation` and `target` together, with exact identities and explicit owner aliases. Schema changes invalidate the cache; record replacement and snapshot deletion remove obsolete edges.
- `bf eval` discovers YAML suites recursively under `evals/`; `--path` selects a suite or directory. Technical tests remain in `tests/`. `bf init --full` creates both folders.
- Git and Calendar example sensors preserve author/repository and organizer/attendee roles for explicit mapping.

### Manual upgrade from 9

1. Preserve the brain files, configuration, machine state and previous runtime; pause all collection writers and finish or recover pending transactions.
1. Install the same Brain Framework 10 build for every CLI, MCP host, project environment and scheduled writer. Restart long-running readers after the switch.
1. Change `bf.yaml` to `version: 4`. Declare shared fields under `schema` and each sensor's `fields` mapping. Keep provider transformations in sensors; do not infer missing relationships from names or flattened links.
1. Create `evals/`, move `queries.yaml` to `evals/retrieval.yaml`, change suite `version` to 4, and update paths in tasks, docs and skills. Multiple named suites may share the directory.
1. Existing records without normalized fields remain readable. Preserve originals before explicitly backfilling fields from retained structured evidence; unavailable historical roles remain unknown. Mapping changes alone do not rewrite history or contact providers.
1. Add directly related brains under `brains: {team: {path: ../team}}` in `bf.yaml`; ensure target names match. Readers now include these direct references, so review the intended audience. Use paths or local names for discovery. Existing optional registrations remain usable; newly initialized brains need explicit `--collect` before collection.
1. Keep the brain `name` stable across clones; use it as the BF URI authority. Declare roles before adding BF link queries. Give entity notes explicit `entity` identities and reviewed aliases; do not reinterpret external URL queries or fabricate historical relationships. Refresh separately installed skills and brain agent instructions.
1. Run `bf build`, `bf validate`, technical tests and `bf eval`; check role-specific queries and exact reads, then resume the scheduled writer. Keep originals until recovery is verified. Rollback requires the prior runtime and original configuration and records, not only a cache rebuild.

No migration tooling or legacy command surface is included. Runtime dependencies are unchanged.

## [v9.2.0](https://github.com/fmind/brain-framework/releases/tag/v9.2.0) - 2026-09-24

Brain Framework 9.2 starts new brains smaller, checks action folders and repairs the PyPI project page. The brain format is unchanged: existing version 3 brains need no conversion.

### Added

- `bf init --full` also creates the optional versioned folders: `memories/`, `assets/`, `sensors/`, `routines/`, `settings/`, `skills/` and `tests/`.
- `bf validate` reports action folders that are not named `YYYY-MM-DD_slug` or lack `ACTION.md`. Rename such folders with `git mv`; validation then reports any links to update.
- Document the optional brain folders: versioned `assets/` for media that notes link to, and the unversioned `inputs/`, `originals/` and `logs/` that new brains already ignore.

### Changed

- `bf init` creates only `projects/`, `concepts/` and `actions/`; other folders appear when first needed.
- `bf register` keeps the registry's leading comment lines when it rewrites the file.
- Search, MCP and retrieval cases resolve relative times in one place. An invalid search time exits 2 as invalid input, and an invalid retrieval case names the case.
- `bf status` builds its report in the health service; the CLI stays a thin adapter.
- PyPI publication accepts deployments only from `v*` tags.

### Fixed

- README links are absolute, so the PyPI project page reaches the documentation, skills and examples; a test keeps every link resolvable offline.
- `bf eval` names a missing `queries.yaml` instead of reporting an inaccessible file.
- Claude Code discovers the repository's `bf-contribute` skill through `.claude/skills`.

## [v9.1.0](https://github.com/fmind/brain-framework/releases/tag/v9.1.0) - 2026-09-24

Brain Framework 9.1 prepares team deployments. The brain format is unchanged: existing version 3 brains need no conversion.

### Added

- `bf init --no-collect` registers a shared brain for search without collection trust, and `init` accepts a fresh clone of an empty repository.
- A [team brains](https://fmind.github.io/brain-framework/docs/team/) guide: distinctive names, joining, CI collection with preserved run state, allowlisted `memories/` publication, required review of sensor code, and data retention.
- A documented compatibility policy: the brain, `bf.yaml` and `queries.yaml` formats change only in a major release, with manual upgrade steps.

### Fixed

- `bf init` anchors `.gitignore` patterns to the brain root, so `actions/*/inputs/` stay versioned and a teammate's clone validates like the author's copy. Existing brains that copied the old patterns should prefix `inputs/`, `logs/`, `memories/`, `originals/` and `.bf/` with `/`.
- `bf init` names a brain after its directory instead of `knowledge`, avoiding registry name collisions between personal and team brains, and explains how to resolve a taken name.
- Unsupported platforms fail with a clear message instead of an import traceback.

### Changed

- Call executable collectors sensors in command help, documentation and the `bf-maintain` skill; record refs, `--source`, health `sources` and OKF `sources` keep their provenance names. `search --source` now has help text.

## [v9.0.1](https://github.com/fmind/brain-framework/releases/tag/v9.0.1) - 2026-09-24

First published Brain Framework release. The v9.0.0 candidate stopped at the macOS ARM64 concurrent-registration check before publication; its tag remains unchanged. This release includes the complete identity and format transition described below.

### Fixed

- Recover when a competing process creates the shared lock during its initial open, while retaining the same confined parent and no-follow checks. A missing or unsafe lock still fails explicitly.
- Cover initial lock creation races, persistent missing locks and unsafe contenders without relaxing writer serialization.

### Breaking changes

- Rename FKF to Brain Framework: install `brain-framework`, import `bf`, and run `bf`. The repository and documentation move to `fmind/brain-framework`.
- Use format 3 for `bf.yaml` and `queries.yaml`, with `sensors:`, `{{brain}}`, `--brain`, `BF_BRAIN`, and a `brains:` registry under `~/.config/bf/`. Private runtime state lives under `~/.local/state/bf/`.
- Organize authored knowledge in `projects/`, `concepts/`, and `actions/` with `ACTION.md`; collected records in `memories/`; collection code in `sensors/`; maintenance in `routines/` and `settings/`; disposable search in `.bf/`.
- Rename workflows to `bf-use`, `bf-learn`, `bf-maintain`, and `bf-contribute`. CLI and MCP results identify their brain; collection reports identify sensors while record provenance retains `source:id` and OKF `sources`.
- Keep one current format without legacy aliases. Preserve original evidence and per-machine collection trust during any one-time transition, and rebuild caches from the migrated files.

### Preserved

- Offline search and exact reads, two read-only MCP tools, plain-file evidence, bounded trusted collection, atomic recovery and external scheduling.
- Stable record IDs, provider provenance, explicit concept types, and searchable Markdown in action inputs and outputs.

## [v9.0.0](https://github.com/fmind/brain-framework/releases/tag/v9.0.0) - 2026-09-24

Unpublished transition candidate. Publication stopped at the macOS ARM64 concurrent-registration gate; the tag remains unchanged. The corrected transition is released in v9.0.1.

## [v8.2.2](https://github.com/fmind/fkf/releases/tag/v8.2.2) - 2026-09-24

### Fixed

- Refuse collection into a source with duplicate stored IDs before changing evidence, including snapshot replacement.
- Serialize concurrent base registrations and write the registry durably with owner-only permissions.
- Preserve `#` and percent escapes in note filenames and local links; require exact record IDs in retrieval cases.

### Changed

- Explain the value of shared, file-based knowledge through concrete questions and the search, read, work and update loop.
- Add runnable team-pilot retrieval cases, routine upgrade instructions and host connection checks.
- Align documentation and skills on base selection, skill discovery, collection trust, recovery and native scheduling.

## [v8.2.1](https://github.com/fmind/fkf/releases/tag/v8.2.1) - 2026-09-23

First published 8.2 release. The v8.2.0 candidate stopped at the Intel macOS test gate before publication; its tag remains unchanged.

FKF 8.2 makes incomplete results visible, preserves evidence through interrupted writes, and helps distinguish recently edited knowledge from historical records. Existing version 2 bases need no conversion; disposable search caches rebuild automatically.

### Added

- `search --changed-since` and `--current` in the CLI, MCP and retrieval cases, with active, disabled and historical source coverage, freshness and change counts.
- Reserved record attributes `updated`, `observed` and `partial` distinguish upstream revision, collection time and incomplete content from event time.
- Scoped local document extraction and optional Calendar agenda snapshots in the standalone collector examples.

### Fixed

- Select test timezones before Python starts, preserving all DST and shared-cache checks on builds without `time.tzset()`, including Intel macOS.
- Recover interrupted multi-partition commits from durable originals through explicit `build`, collection or backup. Serialize source runs and state updates, preserve directory entries before journal writes, and bound partitions before writing.
- Preserve newer upstream revisions during backfills, repair duplicates and corrupt caches, and retry concurrent cache replacement.
- Report skipped files and unavailable bases in search; reject incomplete retrieval-case answers. Fail stale-cache health checks and skipped-file updates, reject malformed partition paths, confine link validation, and distinguish unreadable record sources from missing records.
- Correct recent and identity ordering, per-item passage selection, local-date and DST handling, and malformed-note isolation. Expose identity ambiguity and OKF provenance links.
- Validate Drive snapshot response identity before accepting empty catalogs, bound provider output while subprocesses run, and cancel collector descendants on SIGTERM.

### Changed

- Rewrite onboarding around a runnable first decision and a small team pilot. Clarify offline and security limits, scheduler cadence and example isolation.
- Expand failure tests, French retrieval cases and the benchmark's changed and unchanged record paths. No runtime dependencies or services added.

## [v8.1.0](https://github.com/fmind/fkf/releases/tag/v8.1.0) - 2026-09-23

### Added

- `fkf status` lists active or blocked project notes whose `updated` date is more than 14 days old under `review`, as a reminder rather than a failure.
- `search` and `read` keep local usage counts in the private state directory (time, operation and result count, never the query), summarized by `fkf status` over 7 and 30 days so an owner can see whether agents use a base. Retrieval cases are not counted.

### Changed

- The example Git collector skips hidden repositories, repositories named with `--skip`, and bot or reserved-test-domain authors.

## [v8.0.1](https://github.com/fmind/fkf/releases/tag/v8.0.1) - 2026-09-23

### Changed

- Match English word forms with the FTS5 Porter stemmer, so `meetings` finds `meeting` and `decided` finds `decide`. Existing caches rebuild on the next search.

## [v8.0.0](https://github.com/fmind/fkf/releases/tag/v8.0.0) - 2026-09-22

FKF 8 focuses on the loop that makes a knowledge base useful: collect on a schedule, search from anywhere, read the exact source, keep notes current. It removes machinery that made bases hard to run and notes hard to read.

### Breaking changes

- Store records as monthly JSON Lines, `records/<source>/<YYYY-MM>.jsonl`, with one line per source item upserted by id; snapshot sources keep one `snapshot.jsonl`. Immutable per-run capture files, capture hashes, latest/historical snapshots and `--history` are removed; Git or backups keep history. This also removes the 20,000-file ceiling that hourly collection reached within two months.
- Replace `find` and `context` with one `search [QUERY] [--since] [--until] [--source] [--type] [--status] [--limit] [--recent]`. An empty query lists a time window newest first. Byte budgets are removed; `--limit` bounds results.
- Use readable refs everywhere: `path`, `path#section` and `source:id`. Base ids, `fkf://` qualified references and `record:<hash>` URIs are removed.
- `fkf.yaml` is `version: 2` with a `name` and no `id`; `fkf.local.yaml` is removed. Bases are registered per user in `~/.config/fkf/config.yaml`, which also grants collection trust per machine.
- `collect SOURCE [--since] [--until] [--dry-run]` replaces positional windows and `--preview`. `build` always rebuilds from scratch; `--check` and `--if-stale` are removed.
- Remove containers, `parents`, `--within` and `indexes/structures.json`; link folders and labels with ordinary `links`. Remove note supersession, the special `## History` heading, trust-signal computation, `reviewed` and `effective`; note status is one of draft, active, paused, blocked, done, stable, deprecated or archived, and `updated` dates a note.
- MCP exposes `search` and `read`.
- Convert a v7 base with a one-off script in that base; see the upgrade notes in the documentation.

### Added

- Search every registered base from any directory, or only the enclosing base; results name their base.
- `fkf register PATH [--collect]` adds a cloned team base; a base never runs collectors on a machine that has not trusted it.
- The search cache refreshes itself incrementally before each query, skips and reports unparsable files, and keeps answering, marked `stale`, while another writer holds the base.
- Relative times: `now`, `today`, `yesterday`, `12h`, `7d`, `2w` and local `YYYY-MM-DD` dates.
- `fkf status [--check]` reports each source's last run, success, error and private stderr log, and fails when a trusted scheduled source is stale.
- `fkf update` covers every trusted base, resumes each window source from its last success with overlap, catches up at most 30 days, and keeps run state outside the base.
- `fkf validate` reports every problem at once, including broken links, missing headings, cited records that do not exist and misplaced or duplicate records.

### Changed

- Rank exact identities first, then items matching all words, then any word; notes above records; deprecated and archived notes last; one result per note through its best section.
- Collectors run from the base root with the user's environment minus loader-injection variables, and write stderr to a bounded private log.
- Retrieval cases use `version: 2` with `expect`, `forbid`, `text`, `empty` and time filters.

## [v7.0.1](https://github.com/fmind/fkf/releases/tag/v7.0.1) - 2026-09-21

First published v7 release. The v7.0.0 candidate stopped at the macOS package gate before publication; its tag remains unchanged.

### Fixed

- Resolve temporary smoke environments to physical paths, including macOS `/var`, so strict private-state symlink protection remains enabled during distribution checks. Copy packages explicitly when temporary environments and the cache use different filesystems.

### Breaking changes

- Replace the Go implementation with one typed Python package and the `fkf` command. Python 3.14 or newer on Linux or macOS is required; install with `uv tool install --python 3.14 'fkf==7.0.1'`.
- Use one current base format: `fkf.yaml`, authored `projects/`, `wiki/` and `tasks/`, immutable normalized `records/`, and disposable `.fkf/` and `indexes/`. Preserve a v6 base and executable separately; there is no in-place migration or compatibility command.
- Remove bundled provider integrations, presets, harness installation, execution approval registries and learning proposal machinery. Bases own collectors, schedules and knowledge editing. Reusable Markdown skills and three source examples are maintained separately from the Python distribution.
- Require an explicitly built, ready cache for indexed retrieval. Missing, stale or corrupt caches name the `fkf build` recovery command; direct authored and capture-file reads remain available.

### Added

- Explicit `update` with a side-effect-free dry-run, per-source refresh policy and durable successful automatic checkpoints. Failed windows stay due, manual collections do not move automatic progress, and `build --if-stale` skips a ready index.
- Base-qualified exact evidence references, explicit source structure and membership filters, current versus historical capture retrieval, and authored section references.
- OKF v0.2 wiki structure validation, provenance and asserted verification signals, lifecycle filters, and resumable task folders with inputs and outputs.
- A runnable fictional base with a credential-free collector and retrieval acceptance cases; focused `fkf-use`, `fkf-learn`, `fkf-maintain` and repository-local `fkf-contribute` skills.

### Changed

- Keep CLI and the three read-only MCP tools on shared services. Context includes the complete JSON and final newline within four UTF-8 bytes per budget unit; MCP also counts its response wrapper.
- Match literal lexical terms with case and diacritic folding, explicit identities and authored-note preference. Keep event-time filtering separate from capture recency and decision validity.
- Confine filesystem access, bound subprocess output and runtime, sanitize collection environments, and preserve atomic immutable evidence and a tested rebuild/recovery path.
- Publish a documented installation and upgrade path, complete command reference, skill setup example, and contributor release checklist. Remove obsolete implementation proposals from the current tree.

## [v6.0.2](https://github.com/fmind/fkf/releases/tag/v6.0.2) - 2026-09-09

### Fixed

- Share a fifteen-second deadline across passive-hook children while allowing individual context calls up to ten seconds. Expired budgets prevent new children, and timed-out process groups are terminated.
- Create every missing parent directory with owner-only permissions during atomic writes, without changing existing directory modes.
- Isolate the security-rule checkout from Git environment variables inherited by linked-worktree hooks, while retaining rejection of local rule edits.

## [v6.0.1](https://github.com/fmind/fkf/releases/tag/v6.0.1) - 2026-09-09

### Fixed

- Keep passive hooks compatible with system Python 3.9 and newer, independently of the FKF package’s Python 3.14 environment. The formatter now preserves that syntax boundary and a regression test checks it.
- Consolidate the hook’s validated direct-argv dispatch through the fixed system `env` executable.

## [v6.0.0](https://github.com/fmind/fkf/releases/tag/v6.0.0) - 2026-09-09

### Breaking changes

- Consolidate source execution and improve offline retrieval. Base-owned helpers now live in `sources/`, retrieval evaluations in `checks/queries.yaml`, and optional app scripts in `clients/`. Update declarations and reinstall managed harness hooks, review the resulting execution plan, and renew trust before collecting. Stored evidence remains readable without re-collection.

### Changed

- Select a persistent launcher explicitly with `harness print/install --executable` when package-manager PATH entries disagree.
- Reuse status narrative pages for briefing commitments instead of reading and parsing task/project files again.
- Rename base collection helpers from `bin/` to `sources/` and retrieval acceptance to `checks/queries.yaml`.
- Declare single-script uv app clients under `clients:`; hash and disclose their separate execution tree.
- Make CLI context receipt persistence opt-in with `--save-receipt`, keeping ordinary context reads lock-free.
- Surface explicit project next actions, review dates, deadlines, and blockers in the offline brief.
- Improve multi-term lexical ranking and excerpts, reuse Unicode-preserving text analysis, and reduce exact-budget packing work.
- Let retrieval evaluations require answer-bearing excerpts and verified offline reads, beyond URI recall.
- Describe harvested lessons as trace citations rather than a knowledge-quality measure.

### Fixed

- Report exact missing completed dates per enabled event source using the configured collection window, including in briefing attention.
- Reuse task pages for fallback selection and the global learned backlog, and skip a second Markdown parse when rendered headings contain no Learned section.
- Keep conjunctions out of question scoring and select commitment and body excerpts independently so metadata cannot displace the answer.
- Give body-cache manifests an independent 8 MiB bound, retaining the 4,096-entry and 512 MiB content limits.
- Select prompt transcripts through bounded archive metadata and resolve bodies from stored lineage, generation, and turn provenance without changing evidence IDs or deleting history.
- Search project commitments and preserve active handoffs in compact identity context; unify indexed and fallback lesson backlog semantics.
- Compile canonical fragment validation once per process and report hook timeouts without exposing child output.
- Compare GitHub commit bounds as instants, project safe fallback titles for untitled browser visits, and discard RSS stylesheet metadata without fetching it.

- Open Chromium-family browser roots and profile directories through retained no-follow descriptors so linked path components cannot redirect local history or bookmark collection.

## [v5.0.1](https://github.com/fmind/fkf/releases/tag/v5.0.1) - 2026-09-07

### Fixed

- Prevent long-lived MCP stdio servers on Python 3.14 from accumulating timeout callbacks during cancellation polling, eliminating age-correlated CPU and memory growth.
- Let local release verification ignore only uv's exact one-byte `dist/.gitignore` marker while continuing to reject every other unexpected asset.
- Parse RSS, Atom, and OPML with a DTD-rejecting XML parser so valid CDATA and predefined or numeric references retain their text without enabling entity expansion.
- Preserve provider-formatted Gmail recipient names while continuing to derive normalized participant identities from mailbox addresses.
- Keep agent session hooks non-blocking when invoked with terminal standard input.

## [v5.0.0](https://github.com/fmind/fkf/releases/tag/v5.0.0) - 2026-09-07

### Highlights

- Reimplement FKF as one typed Python 3.14 package while preserving the `fkf` command, `fkf: 1` configuration and evidence envelopes, trust digests, rebuildable graph and lexical cache contracts, ranking version 7, offline reads, and bounded read-only MCP surface.
- Publish a wheel and source distribution for `uv tool install fkf` and one-shot `uvx fkf` use, with locked uv development, strict Ruff and ty checks, hermetic branch-coverage tests, PyPI trusted publishing, and GitHub build-provenance attestations.
- Consolidate provider execution behind one direct-argv boundary, package presets and skills as runtime resources, and retain deterministic differential coverage against the final Go implementation.
- Load the MCP SDK only for MCP commands so ordinary CLI startup does not pay for its server and transport stack.
- Replace the Hugo module with a locked, self-contained Zensical documentation build while preserving the published Pages routes.

### Breaking changes

- Replace native release archives, `install.sh`, and the self-replacing `fkf upgrade` command with standard Python packaging. Use `uv tool upgrade fkf` for a persistent uv installation.
- Narrow the existing `?jq=` and `--where` selector spelling to the safe field-path grammar plus optional terminal `| length`; arbitrary jq programs are rejected instead of running an embedded evaluator.

### Upgrade notes

- Existing bases and collected evidence need no migration or re-collection. After installing v5, refresh official helpers, review and renew execution trust, then rebuild derived caches: `fkf config helpers --refresh`, `fkf trust --all`, and `fkf build all`.
- Use a persistent `uv tool install fkf` launcher for harness and schedule integrations. Reserve `uvx` for one-shot commands.

## [v4.0.1](https://github.com/fmind/fkf/releases/tag/v4.0.1) - 2026-09-04

### Fixed

- Make Ruff linting independent of contributor-level configuration and use the correct exception types in the Gmail body helper, restoring the clean four-platform release gate.
- Keep historical agent-session collection available as the append-only store grows by bounding in-window identities before selecting their newest complete generation, while rejecting a partial manifest scan.

## [v4.0.0](https://github.com/fmind/fkf/releases/tag/v4.0.0) - 2026-09-04

### Highlights

- Make every derived-cache check read-only with `build [graph|index|wiki|all] --check`, add selective body-cache pruning by source and age, and report lexical-index integrity alongside graph health.
- Add reviewed Gmail and Calendar body helpers plus four disabled Google Workspace metadata presets, while keeping provider diagnostics private and bounding provider output before it can fill memory or temporary storage.
- Keep append-only agent-session stores collectible without a lifetime generation ceiling, normalize Git commit titles without discarding raw messages, and refresh the compact embedded usage skill.
- Harden installation, upgrade, and release delivery with exact dirty-build version handling, portable macOS installation, a four-platform CI gate, draft-before-attestation publication, and exact-head Pages deployment.
- Rewrite the README around the concrete benefit—owned, inspectable memory shared across coding agents—and tighten the command, source, graph, privacy, and contributor guides.

### Breaking changes

- Remove the documented but ineffective `fkf status --all` flag; bare `status` already performs the complete offline check.
- Emit one `{wiki, projects, records, lint?, ok}` document from bare structured `fkf validate` instead of concatenating several top-level JSON reports.
- Bound `receipt.consulted_bodies` under the requested pack budget and expose the complete count as `consulted_bodies_total`.
- Aggregate text `find --count` output across the selected window, bound text `who` neighbours per relation kind, and make body-prune text output explicit.
- Treat a corrupt lexical cache as a status error. A missing or stale rebuildable cache remains a warning.
- Remove the unused exported `services.StatusRequest.All` field and add build/prune request types plus cache-health and receipt fields. Go source consumers using unkeyed literals or strict JSON decoders must update.

### Fixed

- Preserve body-cache crash consistency and confinement across selective pruning, corrupt or missing cache entries, malformed timestamps, symlinked roots, source/age no-ops, and newest-event restore markers.
- Keep command output inside exact byte budgets, make graph-generation state a published confined URI, and prevent stale or corrupt derived caches from masquerading as current.
- Refuse unsafe response-file-style body arguments, disclose the real body execution policy during trust review, and retain one finite shutdown path for oversized or uncooperative body providers.
- Prevent an equal or newer Git-describe development build from being replaced by an older release, and ensure all release archives carry the README, license, notices, checksums, and build-provenance attestations.

### Upgrade notes

- Existing `fkf: 1` configuration and evidence remain valid; no re-collection or data migration is required.
- Refresh any installed official helpers, review and renew execution trust, then rebuild derived caches: `fkf config helpers --refresh`, `fkf trust --all`, and `fkf build all`.
- Update consumers of bare structured `validate` and the removed `status --all` flag before upgrading automation.

## [v3.0.2](https://github.com/fmind/fkf/releases/tag/v3.0.2) - 2026-09-03

### Fixed

- Keep the cross-process writer-lock test helper alive without triggering Go's deadlock detector, removing a timing-dependent macOS CI failure without changing runtime behavior.

## [v3.0.1](https://github.com/fmind/fkf/releases/tag/v3.0.1) - 2026-09-03

### Fixed

- Make schedule CLI tests select the native fake scheduler and managed-file layout, restoring the hermetic CI contract on macOS without changing runtime behavior.

## [v3.0.0](https://github.com/fmind/fkf/releases/tag/v3.0.0) - 2026-09-03

### Highlights

- Add deterministic `brief`, `day`, `timeline`, `who`, and `eval` workflows, temporal query grammar, declared identity aliases, and compact text and structured retrieval receipts.
- Add digest-bound lexical and constant-time graph caches while keeping durable evidence authoritative, offline reads reproducible, and indexed and fallback retrieval semantically identical.
- Add login-aware opportunistic sync, hourly systemd and launchd scheduling, and idempotent harness integration for Claude Code, Codex, Gemini CLI, Copilot CLI, Antigravity, OpenCode, Grok, Cursor, Kiro, and Cline.
- Add bounded, ignored, manifest-verified body caching with per-source `none`, `cache`, and `sync` policies; first-class meeting-note and local agent-memory sources can prefetch searchable text without copying it into durable evidence.
- Expand and harden the reviewed personal presets, session traces, staged learning workflow, MCP surface, provider pagination, process isolation, trust revalidation, and graph generation consistency.

### Breaking changes

- Every collected record must now project one meaningful, control-free `title`; update custom source schemas and field mappings before the next sync.
- Structured `find` and `context` results omit raw provider records and internal day selections by default; pass `--raw` only when those diagnostic fields are required.

### Upgrade notes

- Existing evidence remains valid and requires no re-collection. Run `fkf build all --base <base>` to create the new derived graph and lexical caches.
- Refresh FKF-owned helpers and harness integrations, review the resulting execution plan, and renew trust before running changed collectors: `fkf config helpers --refresh`, `fkf harness install --all`, then `fkf trust --all`.
- Body caching stays opt-in per source. The default `bodies: none` fetches only on an explicit `read --body`; `cache` retains an explicitly fetched body and `sync` prefetches it after evidence is written.

## [v2.1.0](https://github.com/fmind/fkf/releases/tag/v2.1.0) - 2026-08-30

### Highlights

- Add a dedicated base `tests/` execution tree for source verification hooks, recursively covered by trust and prepended to `PATH` only for `fkf test`; collection and body commands cannot see test fixtures or shadows.
- Report source-hook readiness separately from ordinary `requires:`, disclose `bin/` and `tests/` as distinct trust items, and carry the new layout through init, permissions, schemas, documentation, and bundled skills.
- Preserve v2 compatibility: bases without `tests/` keep their existing trust digest, hooks can still resolve from `bin/`, and an empty optional selection remains a successful 0/0 report. Completion gates should name mandatory sources.

### Fixed

- Restrict repository metadata projected by bundled session, Git, and agent-hook helpers to GitHub remotes, while continuing to strip credentials and reject malformed paths.
- Open Atuin history read-only in batch mode, omit deleted rows and command text, and declare the Git dependency used by the agent-sessions preset.

### Upgrade notes

- A pre-existing base `tests/` directory is now reserved, recursively trust-covered execution material and must contain no symlinks. Move source hooks and their support files there, keep generic repository tests elsewhere, then review and renew trust.

## [v2.0.1](https://github.com/fmind/fkf/releases/tag/v2.0.1) - 2026-08-29

### Fixed

- Make verified release archives the documented v2 installation path and explain that Go's major-version import rules keep the unchanged module path's `go install ...@latest` resolution on v1.

## [v2.0.0](https://github.com/fmind/fkf/releases/tag/v2.0.0) - 2026-08-29

### Highlights

- Add optional, trust-covered source `test:` argv and `fkf test`, with enabled-source defaults, explicit disabled-source selection, bounded timeouts, stable reports, and provider-stderr privacy.
- Publish `graph.meta.json` schema version 2 with separate SHA-256 inputs for events, index, projects, tasks, wiki, and edge-relevant schema semantics, plus a framed aggregate and exact `graph.tsv` output digest.
- Give every bundled shell helper an explicit `.sh` extension and require `.sh` or `.py` when scaffolding a helper, including the interpreter in the generated readiness contract when needed.

### Breaking changes

- Existing derived graph metadata must be rebuilt with `fkf build graph`; collected evidence remains readable and requires no re-collection.
- Base configurations and harness integrations using bundled extensionless helper names must move to the corresponding `.sh` names before refreshing helpers.

## [v1.1.2](https://github.com/fmind/fkf/releases/tag/v1.1.2) - 2026-08-27

### Highlights

- Flatten the documentation sidebar on desktop and mobile so Overview and every guide are peers, while preserving all published routes and enforcing the rendered navigation contract in tests.

## [v1.1.1](https://github.com/fmind/fkf/releases/tag/v1.1.1) - 2026-08-27

### Highlights

- Make failed collection diagnostics actionable with the source, date or window, safe substituted command, neutral working directory, timeout, and exit class while keeping provider stderr and body-derived arguments private.
- Stage release installation beside the destination before an atomic replacement, preserving an existing binary if staging fails; cover every published Linux and macOS architecture tuple hermetically.
- Add a dedicated configuration-schema guide, present every documented agent harness at the same level, refresh vendor hook contracts, simplify root help and contributor instructions, and keep the Overview first in the documentation tree.
- Scope toolchain drift checks to project pins, refresh the embedded usage skill and supported-version policy, and retain a strict, generated, link-checked documentation contract.

## [v1.1.0](https://github.com/fmind/fkf/releases/tag/v1.1.0) - 2026-08-27

### Highlights

- Add `fkf upgrade`, which selects the current platform archive, verifies its published SHA-256 checksum and reported version, and atomically replaces the running executable.
- Send the documentation root directly to the Overview, expose Overview in the navigation, and remove the intermediate "Read the docs" landing page.
- Explain repeated `fkf sync` safety and how coding agents learn from project and wiki content through the read-only MCP server and embedded skills.

## [v1.0.0](https://github.com/fmind/fkf/releases/tag/v1.0.0) - 2026-08-26

The initial Fmind Knowledge Framework release: one Go binary that collects developer activity into an owned, inspectable base of JSON and Markdown, links it as a graph of relative URIs, and gives coding agents a deterministic context pack under a token budget.

### Highlights

- Collect complete daily events and point-in-time indexes from local tools, GitHub, Google Workspace, and Google Cloud presets, plus arbitrary reviewed provider commands.
- Compose each source from direct argv or a trust-digested helper whose shebang selects its interpreter, with curated preset helpers for provider boundaries that need shared pagination, completeness, or privacy handling.
- Declare one root semantic `schema:` with descriptions, cardinality, examples, and relation roles; sources associate those shared fields with provider paths, and stored documents retain the exact schema subset they used.
- Build an open, transcription-only graph: any non-reserved lowercase entity scheme is valid, relation field names are base-defined, and edges come only from declared fields, authored links, tags, and explicit `relations:` frontmatter.
- Read, find, rank, and graph stored knowledge entirely offline; the read-only MCP server cannot collect, write, shell, or fetch bodies.
- Share one base across Claude Code, Codex, Gemini CLI, OpenCode, Copilot CLI, Antigravity, Cursor, Kiro, Cline, and other harnesses through portable Agent Skills and documented hooks.
- Keep execution explicit with a canonical-plan trust digest covering enabled commands, body-bound paths, helper scripts, executable bits, executable search directories, retry, pacing, and collection policy without re-arming on YAML presentation, inherited environment, or retrieval-only metadata.
- Use one explicit `fkf: 1` marker value for strict base configuration and the separate additive evidence envelope.
- Store plain JSON and Markdown in five typed layers, with rebuildable `graph.tsv` and `graph.meta.json` at the base root, strict schemas, bounded and atomic I/O, path confinement, owner-only permissions, and untrusted-content framing.
- Reproduce lexical retrieval under a hard whole-pack budget with a selection receipt that records scores, reasons, counted exclusions, rejected pins, evaluation day, ranking version, and semantic-input digest.
- Ship synthetic demos, personal and team presets, a strict Hugo documentation site, hermetic race-tested Go suites, security scans, reproducible archives, checksums, and build-provenance attestations.

### Supported platforms

Release archives are provided for Linux and macOS on amd64 and arm64. Native Windows is intentionally out of scope; WSL2 uses the Linux archive.
