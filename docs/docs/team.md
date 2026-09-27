# Team brains

A team brain is a private Git repository containing knowledge every teammate may read and keep. One person records a decision; another pulls the repository and finds its reason with `bf search`. Use at most one scheduled collection job, and keep personal mail, chat and laptop history in each person's own brain.

## Create it

Create an empty private repository, clone it, and initialize the brain inside the clone. Choose a distinctive name: brain names must be unique within a search context, and many people already use `brain` for a personal brain.

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
bf init ~/team-brain --name team-brain
cd ~/team-brain
```

Replace `example-org` with your organization. Add one project note and a few [retrieval cases](checks.md#retrieval-cases) before adding sensors. For a fictional trial, save the [New website decision](getting-started.md#save-a-decision) in this brain, then check and share it:

```bash
bf validate
bf eval
git add -A
git commit -m "feat: create the team brain"
git push
```

Validation should return `"valid":true`; the starter evaluation returns `"score":"3/3"` until you add your own cases. Creating or reading a brain runs no sensors or routines. Run updates only on the designated collecting machine.

## Join it

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
bf search "visitors clear explanation" --brain ~/team-brain
bf read projects/new-website.md#decision --brain ~/team-brain
```

If the team saved the sample above, the read returns the same product-page decision. After teammates publish changes, run `git -C ~/team-brain pull` to retrieve them; Brain Framework then refreshes its search cache as needed.

To include team notes when searching your personal brain, add a [direct reference](configuration.md#related-brains) in `~/brain/bf.yaml`. Keep the team name stable across clones; it is the namespace in shared links.

For work, select only the intended root with `--brain ~/team-brain`, or set `BF_BRAIN` to its absolute path. Connect an agent host with `bf mcp --brain ~/team-brain`. Direct references are also readable, so review them as part of the work host's [audience](privacy.md#separating-audiences).

Use the same Brain Framework release across the team and its collection job. Within a major version, the brain format (`bf.yaml`, `evals/retrieval.yaml` and the folder layout) stays compatible; a new major version documents manual upgrade steps in its release notes. Upgrade together, then run `bf validate` and `bf eval`.

## Collect in CI

Collect only sources that every member may read and keep, such as the organization's issues and pull requests or shared meeting notes. Copy [example sensors](sensors.md) into `sensors/` with fake-provider tests and declare them in `bf.yaml` with a nonzero `refresh`.

New brains keep `memories/` out of Git. To publish reviewed sources, replace the `/memories/` line of `.gitignore` with an allowlist; pending transaction files and other sources stay ignored:

```gitignore
/memories/*
!/memories/github-issues/
```

This GitHub Actions template checks for due work once a day, then commits collected records. Before enabling it:

1. Replace `VERSION` with your team's Brain Framework release and `github-issues` with your reviewed source name.
1. Configure `TEAM_BRAIN_READ_TOKEN` with read-only access to the selected sources.
1. Review every configured sensor and routine: `bf update` can run both.

The cache retains private run state so window sensors can resume after their previous success. Without retained state, set each window sensor's `lookback` to at least twice the schedule interval. A failed sensor or routine fails the job, so its publish step does not run.

```yaml
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
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
      BRAIN_FRAMEWORK: "brain-framework==VERSION"
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

`bf update` also runs the brain's due [routines](routines.md) after its sensors. Their actions stay in the job's checkout unless the commit step also adds `actions/`; publish them only when every member should review them.

The collection job runs whatever sensor and routine code is on its branch, with its provider token. Give that token read-only access to the selected sources, and require a reviewed pull request for changes to `bf.yaml`, `sensors/`, `routines/` and `.github/`, for example with a `CODEOWNERS` file. If branch rules also require pull requests for `memories/`, allow only this workflow to bypass them, or make its last step open a pull request.

## Keep it useful

- Records committed to Git remain in its history; removing one later means rewriting history in every clone. Check your organization's data-protection and retention rules before publishing a source.
- Collected records are evidence, not verified knowledge. Promote what matters into project notes and concepts through normal review, with links teammates can follow.
- Add each question the team repeatedly asks to `evals/retrieval.yaml`, and run `bf validate` and `bf eval` in pull requests so broken links and lost answers fail before merge.
- Check `bf status --brain ~/team-brain` on a teammate's machine: run state stays with the collection job. A clone does not establish collection health: an enabled scheduled source reports `never` before local success. The latest record times still show the saved evidence.

## Share from a personal brain

For example, you may want to share a product-page lesson while keeping personal meeting notes private. Prepare a copy containing only the lesson and evidence the team can access; keep private source mappings outside it.

Use the [`bf-learn` sharing guide](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/references/share.md) to prepare and review that copy. Validate and evaluate it in a disposable destination without personal-brain references, then inspect its text, metadata and unresolved links for private information. `bf validate` checks structure and links; it cannot establish privacy.

When refreshing shared knowledge later, compare the previous shared version, your new copy and the team's current edits. Preserve the team's changes instead of replacing the note wholesale. See [decision workflows](agents.md#decision-workflows).

## Shared links

Use the shared brain name in portable references:

```markdown
[Product-page decision](bf://team-brain/projects/new-website.md#decision)
```

This link identifies the same note whether the clone lives in `~/team-brain` or `~/brains/work`. Every clone must retain `name: team-brain` in `bf.yaml`.

Declare relationship meanings before authoring typed links. Share only reviewed identities and evidence: a link into a personal brain does not make that evidence available to teammates. Validation reports foreign destinations under `unresolved`; read them explicitly within the permitted selection when needed. See [BF links](schema.md#bf-links).
