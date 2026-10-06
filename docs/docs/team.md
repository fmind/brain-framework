---
icon: lucide/users
description: Set up a private team brain, share reviewed evidence, coordinate collection and evaluate a pilot.
---

# Team setup

A team brain is a **private Git repository**, with one local clone per teammate. BF needs no server or hosted account: Git shares the files, and your repository permissions decide who reads them.

## Agree on the basics

| Decision         | Recommended starting point                                                                                        |
| ---------------- | ----------------------------------------------------------------------------------------------------------------- |
| Audience         | Only information every repository member may read and retain.                                                     |
| Brain name       | One stable name, such as `team-brain`, in every clone.                                                            |
| Knowledge owner  | A maintainer for each project's decisions and next tasks.                                                         |
| Collection owner | One laptop per shared source, with reviewed programs and local credentials.                                       |
| Contributions    | Focused changes reviewed through your usual Git process.                                                          |
| Runtime          | The same BF release on every clone and collecting laptop, [pinned](upgrades.md#pin-a-brains-runtime) if you like. |

Keep personal mail, chat and laptop history in personal brains. A private repository does not lift your organization's rules on retention or sharing.

## Create it

Create an empty **private** repository, without a README, license or `.gitignore`: `bf init` accepts a folder holding only `.git`. Clone and initialize it, replacing `example-org`:

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
bf init ~/team-brain --name team-brain
cd ~/team-brain
```

Save a first decision as in [Getting started](getting-started.md#save-a-decision), and add the team's questions as [retrieval cases](checks.md#retrieval-cases). Then review and share the files:

```bash
bf validate
bf eval
git add AGENTS.md bf.yaml .gitignore projects concepts actions evals
git diff --cached
git commit -m "feat: create the team brain"
git push
```

Validation returns `"valid":true` and `bf eval` returns `"passed":true`.

## Join it

[Install BF](getting-started.md#install-and-create-a-brain), then:

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
cd ~/team-brain
bf read projects
bf validate
bf eval
```

Use `git pull` to receive changes; BF refreshes its search cache on its own. Review reminders follow each file's modification time on your machine, so a pull that rewrites a note restarts them: set `stale_after` for a deadline the team shares. To search the team brain from anywhere, register it with `bf register ~/team-brain`, then pass `--brain team-brain`. Registration and reading never run the brain's programs. Review `bf.yaml`, `sensors/`, `routines/` and any [runtime pin](upgrades.md#pin-a-brains-runtime) before running them inside the clone, as with `bf update --brain ~/team-brain`.

## Collect on a laptop

Keep provider credentials and scheduled collection on the designated owner's laptop. Use CI only for offline `bf validate` and `bf eval`.

1. Add a reviewed [sensor](sensors.md) for information the whole team may retain.
1. Test it locally, then set its `refresh` if regular updates help.
1. Run `bf watch` on that laptop, or a [native schedule](schedule.md#generate-a-native-schedule) kept outside the brain's Git history.
1. Inspect the collected files before sharing them.

New brains ignore `memories/` in Git. To share only a reviewed `github-issues` source, replace the `/memories/` line in `.gitignore` with:

```gitignore
/memories/*
!/memories/github-issues/
```

Other sources and `memories/.pending/` stay ignored, like `logs/`. Collect, check and stage only that source:

```bash
bf collect github-issues
bf validate
git add .gitignore memories/github-issues
git diff --cached
```

Commit after review. Git history keeps committed evidence. A sleeping laptop does not collect, so set freshness expectations accordingly. `bf status` reports this machine's run history: a teammate's clone shows `never` for sources it did not collect, while holding their records.

## Contribute without overwriting each other

- **Notes:** make focused edits; keep one current decision and next step in the owning note.
- **Actions:** give each session its own folder with a 12-character suffix, as in `actions/2026-09-27_review-3f9a1c2e5b7d/`, so teammates picking the same topic on the same day never collide. The `bf-action` [new-action helper](agents.md#resume-an-action) adds one with `--unique`.
- **Records:** different ids live in different files. Competing revisions of one id need review.
- **Sources:** use different source names for different permission scopes. A snapshot replaces its whole source, so never let a partial view replace a shared one.
- **Conflicts:** keep both sides, reconcile them from evidence, then validate and evaluate. Never resolve evidence by last writer wins.
- **Local files:** keep `.bf/`, `logs/` and recovery journals out of Git. Local locks do not coordinate teammates' editors or Git.

Try the [two-contributor example](https://github.com/fmind/brain-framework/tree/main/examples/team), and follow the [conflict guide](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-maintain/references/conflicts.md) when a merge conflicts.

## Connect agents and personal brains

Launch a terminal agent in the team brain, or configure [MCP](mcp.md) with its absolute path. A shared brain should not reference a personal one: its `brains:` references are readable too. To include team notes in your personal searches, add a [direct reference](configuration.md#related-brains) from the personal brain to the team clone; collection still acts on the selected brain only.

Every clone keeps `name: team-brain`, so a link such as `[Product-page decision](bf://team-brain/projects/new-website.md#decision)` works wherever the clone lives. The target brain must be selected or referenced; a link never grants access. To move knowledge from a personal brain, the `bf-use` skill's [sharing guide](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/references/share.md) prepares a copy holding only what the team may read. Validation checks structure, not privacy.

## Evaluate a pilot

Test whether shared context helps before expanding it. Start with one project, three to five volunteers and four weeks:

1. **Choose.** Ask each participant which recent question needed several tools, then pick five recurring ones, such as “What blocks this launch?”. Name the tool that owns each fact and the person who maintains each note and sensor.
1. **Measure a baseline.** Before the introduction, time comparable questions with the usual tools and note correctness, sources consulted and upkeep.
1. **Use it.** In week 1, follow the [four-tool walkthrough](context-hub.md), then answer a real question from a fresh session. In weeks 2 and 3, use BF during ordinary work without reminders.
1. **Compare fairly.** On matched questions, compare the usual tools, the same curated files read directly, and BF retrieval, so organizing the files and BF's retrieval are judged separately. Count failures and abandoned attempts.
1. **Decide.** In week 4, compare the time to a correct, cited answer and the upkeep cost. Continue, change or stop.

Agree on decision rules before starting. For example: four of five people reach a cited answer without help, three return without reminders, and someone other than the author can retrieve and update the notes. Keep a short weekly note instead of telemetry, and share only aggregates, with permission. The fictional demo proves the mechanism, not a saving.
