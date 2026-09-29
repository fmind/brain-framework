# Brain Framework skills

Agent procedures for **gather → normalize → organize and connect → act → learn**, shipped inside the `brain-framework` package so that every installation carries the skills of its own version. They follow the [Agent Skills specification](https://agentskills.io/specification); their names are not `bf` subcommands.

## Choose a skill

| Skill                               | Use it for                                                                              | Example request                                                         |
| ----------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [bf-use](bf-use/SKILL.md)           | Finding and citing evidence, saving knowledge, tracking and reviewing work sessions     | “Why did we choose a single product page? Read and cite the source.”    |
| [bf-setup](bf-setup/SKILL.md)       | Installing BF, creating a brain, connecting an agent, discovering and importing sources | “Set up `~/brain` and check that you can find our project decision.”    |
| [bf-maintain](bf-maintain/SKILL.md) | Sensors, routines and hooks, collection, refresh, recovery and conflicts                | “Why is the local-documents source overdue? Diagnose it before fixing.” |

Install all three: each one names its neighbors when a task crosses into their scope. `bf-use` covers everyday work, including "remember this" and "resume the website review"; `bf-setup` and `bf-maintain` hand selected sources to each other without collecting, scheduling or authenticating on their own.

## Install and update

Install the skills of the installed `bf` into the directory your agent host discovers, such as `~/.agents/skills` or `~/.claude/skills`:

```bash
bf skills ~/.agents/skills
```

The reply lists each skill as `installed`, `updated` or `current`. A manifest (`.bf-skill.json`) in each folder records the digests of the installed files, so a later `bf skills` updates only files nobody edited: an edited skill is reported as `modified`, and a folder BF did not install (or a link) as `unmanaged`, and both stay as they are unless you pass `--force`. Files a newer version no longer ships are removed only when unedited.

After upgrading `bf`, check for drift without writing anything:

```bash
bf skills ~/.agents/skills --check
```

It exits 1 unless every skill is `current`. Start a fresh host session after installing or updating so the host rediscovers the skills, then ask a question the brain can answer, such as “Use bf-use with `~/brain`: why did we choose a single product page? Read the source and cite its ref.” Expect the ref `projects/new-website.md#decision` from the [getting-started decision](https://fmind.github.io/brain-framework/docs/getting-started/). Installation proves configuration; only an observed session proves the host uses the skill and reaches the intended brain.

## What each skill contains

- `bf-use`: retrieval and answering in `SKILL.md`; references for complete reads and graph context, writing knowledge back, links, evidence retention, reviews, consolidation, sharing, actions, working context, handoffs and decisions; the helpers `guarded-write.py`, `evidence.py`, `new-action.py` and `check-handoff.py`; project, concept and action templates.
- `bf-setup`: installation, brain creation, agent access and verification in `SKILL.md`; references for scoped discovery, discovery methods and imports; the `inventory.py` helper.
- `bf-maintain`: diagnosis and core commands in `SKILL.md`; references for integrations, operations and conflicts.

Helpers are standalone Python 3.11+ scripts using only the standard library, run with the agent's `python3` by their path inside the skill folder. They never contact a network; the ones that read the brain call the offline `bf read`.

## Structure and maintenance

Each folder holds a short `SKILL.md` router (when to use the skill, its core loop, its boundaries and one line per reference or helper) plus `references/`, `scripts/` and `templates/` loaded only when a task needs them. `metadata.version` equals the package version, and `compatibility` names the Brain Framework major version the procedures assume. Links to the documentation site describe the current release; `bf --version` and `bf COMMAND --help` are authoritative for an installation.

To adapt a skill, edit the installed copy: `bf skills` then reports it as `modified` and never overwrites it. To return to the packaged version, rerun `bf skills DIR --force` after keeping the edits you need. Brain-specific procedures belong in the brain itself, for example under its own `skills/` folder, rather than in these copies.

Tests in the repository check that every command, option, link and file a skill mentions exists, that each `SKILL.md` references all of its files, and that helpers run on Python 3.11 with the standard library. The [example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain) exercises decision review, evidence retention and sharing with fictional data.
