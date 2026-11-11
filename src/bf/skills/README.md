# Brain Framework skills

Agent procedures for **gather → normalize → organize and connect → act → learn**, shipped inside `brain-framework` so every installation carries matching skills ([Agent Skills specification](https://agentskills.io/specification)).

## Choose a skill

| Skill                               | Use it for                                                                          | Example request                                                      |
| ----------------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| [bf-use](bf-use/SKILL.md)           | Answering from evidence, active note updates, reviews, consolidation and sharing    | "Why did we choose a single product page? Read and cite the source." |
| [bf-action](bf-action/SKILL.md)     | Starting, resuming, handing off and settling a tracked work session (`ACTION.md`)   | "/bf-action website-review"                                          |
| [bf-setup](bf-setup/SKILL.md)       | Installing `bf`, creating a brain, connecting a host, source discovery and imports  | "Set up `~/brain` and verify you can answer our project decision."   |
| [bf-maintain](bf-maintain/SKILL.md) | Sensors, routines, assistants, gates, channels, hooks, refresh, recovery, conflicts | "Why is `local-documents` overdue? Diagnose it before fixing."       |

Install all four so each skill can hand off cleanly:

```bash
bf skills ~/.agents/skills
bf skills skills --link # inside a brain: installs ./skills and links .agents/skills + .claude/skills
bf skills ~/.agents/skills --check
```

`.bf-skill.json` tracks installed file digests so later `bf skills` runs update untouched copies and prune removed files. Three statuses leave a folder untouched and exit 1:

| Status      | Meaning                                                      | Recovery                                                     |
| ----------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `modified`  | An installed file was edited or deleted (`edited`).          | Keep needed edits, then pass `--force`.                      |
| `unmanaged` | BF did not install the folder, or it is a symlink.           | Move it aside, or pass `--force` (`--force` keeps symlinks). |
| `newer`     | A newer `bf` (such as a pinned runtime) installed this copy. | Upgrade this `bf` instead of downgrading with `--force`.     |

Start a fresh host session after installing or updating, then verify with a real question (such as citing `projects/new-website.md#decision` from [Getting started](https://fmind.github.io/brain-framework/docs/getting-started/)). Brain-specific procedures belong in the brain's own `skills/` folder ([workflow skills](https://fmind.github.io/brain-framework/docs/agents/#add-your-own-workflow-skills)).

## Runtime and helper boundaries

- Each skill holds a short `SKILL.md` router plus on-demand `references/`, `scripts/` and `templates/`.
- Helpers are standalone Python 3.11+ stdlib scripts invoked by literal path (`python3 SKILL_DIR/scripts/NAME.py`, or `"$(uv python find --system --no-config --no-project 3.14)"` when `python3` is older — never `uv run`, which adopts a directory `.venv`). They never use the network and call the installed `bf` first on `PATH`.
- Read-only commands and note edits always use the installed `bf`, never a brain's pinned runtime (`pyproject.toml` / `uv.lock`). Only `bf-maintain` executes through a pin, after user creation or review of `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version`, and never with an unreviewed `.venv/` ([pinned runtime](https://fmind.github.io/brain-framework/docs/upgrades/#pin-a-brains-runtime)).
- The [example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain) exercises retrieval, review, retention and sharing on synthetic data.
