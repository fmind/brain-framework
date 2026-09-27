# Team setup

A team brain is a **private Git repository**, with a separate local clone for each teammate. BF needs no team server or hosted account. Git shares files; your repository permissions control who can read them.

## Agree on the basics

| Decision         | Recommended starting point                                                            |
| ---------------- | ------------------------------------------------------------------------------------- |
| Audience         | Only information every repository member may read and retain.                         |
| Brain name       | One stable name, such as `team-brain`, shared across clones.                          |
| Knowledge owner  | Name a maintainer for each project's decisions and next tasks.                        |
| Collection owner | One designated laptop per shared source, with reviewed scripts and local credentials. |
| Contributions    | Focused changes reviewed through your usual Git process.                              |
| Runtime          | The same BF release on all clones and collecting laptops.                             |

Keep personal mail, chat and laptop history in personal brains. A private repository does not remove your organization's rules for retention or sharing.

## Create it

Create an empty **private** repository in your organization, then clone and initialize it. Replace `example-org` with your organization:

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
bf init ~/team-brain --name team-brain
cd ~/team-brain
```

Save your first project decision using [Getting started](getting-started.md#save-a-decision). Add the questions your team needs to [retrieval cases](checks.md#retrieval-cases), then review and share the files:

```bash
bf validate
bf eval
git status --short
git add AGENTS.md bf.yaml .gitignore projects concepts evals
git diff --cached
git commit -m "feat: create the team brain"
git push
```

Validation should return `"valid":true`. Starter evaluation returns `"score":"3/3"`; extend it with team questions before relying on that score.

## Join it

Install BF, then:

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
cd ~/team-brain
bf read
bf read projects
bf validate
bf eval
```

If the team saved the website example, `bf search "visitors clear explanation"` finds its decision. Use `git pull` to receive changes; BF refreshes its search cache automatically.

Review `bf.yaml`, `sensors/` and `routines/` before running collection. Reading a shared brain does not execute its programs.

## Collect on a laptop

Keep provider credentials and scheduled collection on the designated owner's laptop. Use CI for offline `bf validate` and `bf eval`, with no provider credentials or collection jobs.

1. Add a reviewed [sensor](sensors.md) for information the whole team may retain.
1. Test it locally, then set its `refresh` if regular updates are useful.
1. Use `bf watch` on that laptop, or an optional [native schedule](schedule.md).
1. Inspect the collected files before sharing them through Git.

New brains ignore `memories/`. To share only a reviewed `github-issues` source, replace the `/memories/` line in `.gitignore` with:

```gitignore
/memories/*
!/memories/github-issues/
```

Other sources and `memories/.pending/` remain ignored. Collect and validate from the brain directory, then inspect and commit only the intended source:

```bash
bf collect github-issues
bf validate
bf eval
git add .gitignore memories/github-issues
git diff --cached
```

Commit and push after review, using your team's normal process. Git history retains committed evidence. A sleeping or offline laptop does not collect; choose refresh expectations accordingly.

`bf status` reports **local** run history. A teammate's clone can have useful saved records while showing `never` for local collection. Check collection health on the designated laptop and record dates on other clones.

## Contribute without overwriting each other

- **Project and concept notes:** make focused edits; keep one current decision and next step in the owning note.
- **Actions:** give independent sessions unique folders; resume existing work by its ref. The [action workflow](agents.md#resume-an-action) owns this convention.
- **Records:** different ids have separate files. Competing revisions of the same id still need review.
- **Sources:** use different source names for different permission scopes. A snapshot represents its whole catalog; a partial view must not replace a shared one.
- **Conflicts:** preserve both sides, reconcile using evidence, then validate and evaluate the merged result. Never use automatic last-writer-wins for evidence.
- **Local state:** keep caches, logs and recovery journals out of Git. Use separate clones; local BF locks do not coordinate teammates' editors or Git operations.

Try the [two-contributor example](https://github.com/fmind/brain-framework/tree/main/examples/team) or use the [conflict resolution guide](https://github.com/fmind/brain-framework/blob/main/skills/bf-maintain/references/conflicts.md) when needed.

## Connect an agent

Launch a terminal agent in the team brain, or configure [MCP](mcp.md) with its absolute path. Review direct `brains:` references: they are also readable. Do not reference a personal brain from a shared work configuration.

To include team notes in your own personal searches, add a [direct reference](configuration.md#related-brains) from the personal brain to the team clone. Collection still acts only on the selected root.

## Shared links

```markdown
[Product-page decision](bf://team-brain/projects/new-website.md#decision)
```

Every clone keeps `name: team-brain`, so the link works at different filesystem locations. The target brain must already be selected or directly referenced; a link never grants access. See [link syntax](link-reference.md).

## Share from a personal brain

Use the [`bf-learn` sharing guide](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/references/share.md) to prepare a copy containing only knowledge and evidence the team may access. Validate it without personal-brain references, then review its text, metadata and links for private information. Validation checks structure, not privacy.

When updating the shared copy, preserve teammates' edits. For an optional adoption experiment after setup, see the advanced [team pilot](pilot.md).
