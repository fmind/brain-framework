# Brain Framework skills

Agent procedures for **gather → normalize → organize and connect → act → learn**, shipped inside the `brain-framework` package so that every installation carries the skills of its own version. They follow the [Agent Skills specification](https://agentskills.io/specification); their names are not `bf` subcommands.

## Choose a skill

| Skill                               | Use it for                                                                              | Example request                                                         |
| ----------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [bf-use](bf-use/SKILL.md)           | Finding and citing evidence, saving knowledge and reviewing projects and decisions      | “Why did we choose a single product page? Read and cite the source.”    |
| [bf-action](bf-action/SKILL.md)     | Starting, resuming, handing off and closing a tracked work session (an action)          | “/bf-action website-review”                                             |
| [bf-setup](bf-setup/SKILL.md)       | Installing BF, creating a brain, connecting an agent, discovering and importing sources | “Set up `~/brain` and check that you can find our project decision.”    |
| [bf-maintain](bf-maintain/SKILL.md) | Sensors, routines and hooks, collection, refresh, recovery and conflicts                | “Why is the local-documents source overdue? Diagnose it before fixing.” |

Install all four: each one names its neighbors when a task crosses into their scope. `bf-use` covers everyday work, including "remember this"; `bf-action` starts a session when invoked with a topic, so no request wording is needed, and resumes or hands one off; `bf-setup` and `bf-maintain` hand selected sources to each other without collecting, scheduling or authenticating on their own.

## Install and update

Install the skills of the installed `bf` into the directory your agent host discovers, such as `~/.agents/skills` or `~/.claude/skills`:

```bash
bf skills ~/.agents/skills
```

The reply lists each skill as `installed`, `updated` or `current`. A manifest (`.bf-skill.json`) in each folder records the digests of the installed files, so a later `bf skills` updates a skill only while none of its files was edited: it then finishes an interrupted install and removes the files a newer version no longer ships. Three statuses leave a folder unchanged and make the command exit 1:

| Status      | Meaning                                                                                           | Recovery                                                                                           |
| ----------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `modified`  | You edited or deleted an installed file, listed under `edited`.                                   | Keep the edits you need, then pass `--force`.                                                      |
| `unmanaged` | BF did not install the folder, or it is a link.                                                   | Move it aside, or pass `--force` to install over it.                                               |
| `newer`     | A newer `bf`, such as a brain's pinned runtime sharing the directory, installed a different copy. | Upgrade this `bf`, or run `bf skills` with the newer one: `--force` would install this older copy. |

`--force` never replaces a linked folder: it stops before writing anything until you remove the link.

After upgrading `bf`, check for drift without writing anything:

```bash
bf skills ~/.agents/skills --check
```

It exits 1 unless every skill is `current`. Start a fresh host session after installing or updating so the host rediscovers the skills, then ask a question the brain can answer, such as “Use bf-use with `~/brain`: why did we choose a single product page? Read the source and cite its ref.” Expect the ref `projects/new-website.md#decision` from the [getting-started decision](https://fmind.github.io/brain-framework/docs/getting-started/). Installation proves configuration; only an observed session proves the host uses the skill and reaches the intended brain.

## What each skill contains

- `bf-use`: retrieval and answering in `SKILL.md`; references for complete reads and graph context, writing knowledge back, links, evidence retention, reviews, consolidation, sharing and decisions; the helpers `guarded-write.py` and `evidence.py`; project and concept templates.
- `bf-action`: starting an action from a topic in `SKILL.md`; references for starting, resuming and closing a session, its working-context budget and handoffs; the helpers `new-action.py` and `check-handoff.py`; the action template.
- `bf-setup`: installation, brain creation, agent access and verification in `SKILL.md`; references for scoped discovery, discovery methods and imports; the `inventory.py` helper.
- `bf-maintain`: diagnosis and core commands in `SKILL.md`; references for integrations, operations and conflicts.

Helpers are standalone Python 3.11+ scripts using only the standard library, run with the agent's `python3` by their path inside the skill folder. When that `python3` is older, agents run them with the Python 3.14 that `uv python find --system --no-config --no-project 3.14` names, never through `uv run`: even with `--no-project`, it adopts a `.venv` in the working directory or a parent, so a brain could supply the interpreter, code it runs at startup and the `bf` first on PATH. They never contact a network; the ones that read the brain call the offline `bf read` of the installed `bf`, the first on PATH.

Agents run every read-only command and helper with the installed `bf`, never through a brain's pinned runtime (`pyproject.toml` and `uv.lock`), which installs and runs code the brain supplies. Only `bf-maintain` runs a pin, for execution in a brain the user created or whose `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version` they reviewed, and never with a `.venv/` the brain supplied ([pin a brain's runtime](https://fmind.github.io/brain-framework/docs/upgrades/#pin-a-brains-runtime)).

## Structure and maintenance

Each folder holds a short `SKILL.md` router (when to use the skill, its core loop, its boundaries and one line per reference or helper) plus `references/`, `scripts/` and `templates/` loaded only when a task needs them. `compatibility` names the Brain Framework major version the procedures assume; the installed `.bf-skill.json` records the exact version, so a release changes a skill's files only when its content changes. The documentation site follows the latest code, which can be newer than an installation; `bf --version` and `bf COMMAND --help` are authoritative for it.

To adapt a skill, edit the installed copy: `bf skills` then reports it as `modified` and never overwrites it. To return to the packaged version, rerun `bf skills DIR --force` after keeping the edits you need. Brain-specific procedures belong in the brain itself, for example under its own `skills/` folder, rather than in these copies.

Tests in the repository check that every command, option, link and file a skill mentions exists, that each `SKILL.md` references all of its files, and that helpers run on Python 3.11 with the standard library. The [example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain) exercises decision review, evidence retention and sharing with fictional data.
