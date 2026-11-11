---
icon: lucide/users
description: Set up a private team brain, share reviewed evidence, coordinate collection and evaluate a pilot.
---

# Team setup

A team brain is a **private Git repository** with one local clone per teammate. Git handles sharing and access control; personal mail, chats and machine history stay in separate personal brains.

## Create and join

Create an empty **private** repository (no README or `.gitignore`), initialize it with a stable shared `--name`, add initial notes and [retrieval cases](checks.md#retrieval-cases), and push:

```bash
git clone git@github.com:example-org/team-brain.git ~/team-brain
bf init ~/team-brain --name team-brain
cd ~/team-brain
bf validate && bf eval
git add AGENTS.md bf.yaml .gitignore projects concepts actions evals
git commit -m "feat: create the team brain"
git push
```

Teammates clone `~/team-brain`, run `bf validate && bf eval`, and optionally `bf register ~/team-brain` (or reference it under `brains:` in their personal `bf.yaml` so personal searches include team knowledge while collection stays isolated). Set explicit `stale_after` dates on shared notes because Git checkouts update local file mtimes.

### Review before you merge

Running `bf update`, `bf watch` or an agent inside a shared clone executes pulled configurations and scripts with your permissions ([running code](privacy.md#running-code)). Fetch and inspect executable, config and instruction changes before merging:

```bash
git fetch
reviewed=$(git rev-parse '@{u}')
git diff --name-status "HEAD...$reviewed" -- . ':!projects/*.md' ':!concepts/*.md' ':!actions/*.md' ':!memories/*.json'
git diff --name-status "HEAD...$reviewed" -- '*AGENTS*.md' '*CLAUDE*.md' '*SKILL.md' '*.claude/*' '*.agents/*'
git merge --ff-only "$reviewed"
```

Review every file in both lists (`bf.yaml`, `sensors/`, `routines/`, `prompts/`, `channels/`, `.env`, `pyproject.toml`/`uv.lock`, `.mcp.json`, `.claude/` and instruction/skill files) with `git diff "HEAD...$reviewed" -- FILE` before `git merge --ff-only "$reviewed"` (or `git rebase "$reviewed"`). Install hooks and skills only from reviewed copies outside the shared clone — never via `--link` inside a multi-writer repo.

## Collect on a laptop

Keep provider credentials and scheduled collection on one designated laptop per shared source, and restrict CI to offline `bf validate` and `bf eval`.

To share a reviewed source such as `github-issues` in Git while keeping other sources and `memories/.pending/` ignored, replace `/memories/` in `.gitignore`:

```gitignore
/memories/*
!/memories/github-issues/
```

```bash
bf collect github-issues
bf validate
git add .gitignore memories/github-issues
git diff --cached
```

Check `bf status --check` on the collecting laptop: run history is local, so teammates' clones show `never` for scheduled sources they do not collect themselves.

## Collaborate cleanly

- **Avoid collisions**: keep one current decision per owning project note, create shared actions with `bf action new SLUG --unique` so folders never collide, and use separate sensor names for distinct permission scopes.
- **Reconcile conflicts from evidence**: preserve competing claims until verified ([two-contributor example](https://github.com/fmind/brain-framework/tree/main/examples/team), [conflict guide](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-maintain/references/conflicts.md)).
- **Curate personal-to-team transfers**: strip private passages and internal refs before copying notes to a team repo ([evidence and sharing guide](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/references/evidence.md)).
- **Cloud agent VMs**: give cloud sessions (such as [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web#limitations)) only team brains whose entire history the vendor may process — never upload a personal brain or store provider credentials there. Install BF in the VM [setup script](https://code.claude.com/docs/en/cloud-environments#setup-scripts):

```bash
UV_TOOL_BIN_DIR=/usr/local/bin uv tool install --python 3.14 brain-framework==X.Y.Z
bf skills ~/.claude/skills
```

## Evaluate a pilot

Run a bounded four-week pilot on one active project with three to five teammates before expanding:

1. **Baseline**: pick five recurring cross-tool questions (such as “What blocks this launch?”) and time how long teammates take to answer them with citations today.
2. **Operate**: run the [four-tool walkthrough](context-hub.md) in week 1, then use the team brain during normal work in weeks 2–3 without reminders.
3. **Compare**: measure time to a cited, accurate answer and weekly upkeep cost across existing tools, raw files and BF retrieval, and agree up front on keep/stop criteria (for example, at least four of five teammates reach cited answers independently and non-authors update notes cleanly).
