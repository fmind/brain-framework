# Team brains

A team brain is a private Git repository that a team reads together: project notes, decisions, concepts and actions, plus records from sources every member may see. Teammates clone and search it; at most one scheduled job collects its records. Keep personal mail, chat and laptop history in each person's own brain.

## Create it

Create an empty private repository, clone it, and initialize the brain inside the clone. Choose a distinctive name: registry names must be unique on each machine, and many people already use `brain` or `knowledge` for a personal brain.

```bash
git clone git@github.com:example-org/team-knowledge.git
bf init team-knowledge --name team-knowledge --no-collect
cd team-knowledge
git add -A && git commit -m "feat: create the team brain" && git push
```

`--no-collect` registers the brain for search without collection trust, so your laptop never runs the team's sensors. Start with one project note and a few [retrieval cases](getting-started.md#check-the-answers-your-team-needs) before adding any sensor.

## Join it

```bash
git clone git@github.com:example-org/team-knowledge.git ~/team-knowledge
bf register ~/team-knowledge
bf search "release process" --brain team-knowledge
```

If registration reports that the name is already taken, rename your own brain in its `bf.yaml` and register it again; the shared name stays the same for everyone. Registration without `--collect` never runs sensors.

Agents select every registered brain by default outside a brain. For work, select the team brain explicitly: pass `--brain team-knowledge`, set `BF_BRAIN=team-knowledge` in work repositories, and connect hosts with `bf mcp --brain team-knowledge`. Register a personal brain on a work machine only if its content may reach your work hosts and their model providers; see [separating audiences](privacy.md#separating-audiences).

Use the same Brain Framework release across the team and its collection job. Within a major version, the brain format (`bf.yaml`, `queries.yaml` and the folder layout) stays compatible; a new major version documents manual upgrade steps in its release notes. Upgrade together, then run `bf validate` and `bf eval`.

## Collect in CI

Collect only sources that every member may read and keep, such as the organization's issues and pull requests or shared meeting notes. Copy [example sensors](sensors.md) into `sensors/` with fake-provider tests and declare them in `bf.yaml` with a nonzero `refresh`.

New brains keep `memories/` out of Git. To publish reviewed sources, replace the `/memories/` line of `.gitignore` with an allowlist; pending transaction files and other sources stay ignored:

```gitignore
/memories/*
!/memories/github-issues/
```

This scheduled GitHub Actions workflow runs due sensors once a day, then commits the records they wrote. It restores private run state so window sensors resume where the previous successful run ended; without it, set each window sensor's `lookback` to at least twice the schedule interval. A failed sensor fails the job, and nothing from that run is published.

```yaml
# .github/workflows/collect.yml
name: Collect
on:
  schedule:
    - cron: "23 5 * * *"
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: collect
  cancel-in-progress: false
jobs:
  collect:
    runs-on: ubuntu-24.04
    timeout-minutes: 45
    env:
      BRAIN_FRAMEWORK: brain-framework==9.2.0
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false # Sensors never see the repository write token.
      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
      - uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
        with:
          path: ~/.local/state/bf
          key: bf-state-${{ github.run_id }}
          restore-keys: bf-state-
      - name: Collect due sensors
        env:
          GH_TOKEN: ${{ secrets.TEAM_BRAIN_READ_TOKEN }} # Read-only access to the collected repositories.
        run: |
          uvx --python 3.14 --from "$BRAIN_FRAMEWORK" bf register . --collect
          uvx --python 3.14 --from "$BRAIN_FRAMEWORK" bf update --brain .
      - name: Publish memories
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add memories
          git diff --cached --quiet && exit 0
          git commit -m "chore: collect team memories"
          git push "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "HEAD:${GITHUB_REF_NAME}"
```

The collection job runs whatever sensor code is on its branch, with its provider token. Give that token read-only access to the selected sources, and require a reviewed pull request for changes to `bf.yaml`, `sensors/`, `routines/` and `.github/`, for example with a `CODEOWNERS` file. If branch rules also require pull requests for `memories/`, allow only this workflow to bypass them, or make its last step open a pull request.

## Keep it trustworthy

- Records committed to Git remain in its history; removing one later means rewriting history in every clone. Check your organization's data-protection and retention rules before publishing a source.
- Collected records are evidence, not verified knowledge. Promote what matters into project notes and concepts through normal review, with links teammates can follow.
- Add each question the team repeatedly asks to `queries.yaml`, and run `bf validate` and `bf eval` in pull requests so broken links and lost answers fail before merge.
- Check `bf status --brain team-knowledge` on a teammate's machine: run state stays with the collection job, so freshness there reads `unknown`; the latest record times still show what was collected.
