# Brain Framework skills

Markdown packages that teach agents to use Brain Framework. They are distributed from this repository, separately from the Python package.

| Skill                               | Use                                                                                   |
| ----------------------------------- | ------------------------------------------------------------------------------------- |
| [bf-use](bf-use/SKILL.md)           | Read pages, search and read notes and records from any repository.                    |
| [bf-learn](bf-learn/SKILL.md)       | Keep notes current, retain decision evidence and prepare knowledge for another brain. |
| [bf-action](bf-action/SKILL.md)     | Start, resume or close one action (one session of work) when the user asks for it.    |
| [bf-maintain](bf-maintain/SKILL.md) | Fix failing sensors and routines, schedule updates, backfill and add retrieval cases. |

## Install

Start with `bf-use`; add `bf-learn` to maintain notes after work, `bf-action` to resume work by name and `bf-maintain` for brain operations. From a reviewed checkout of the release you use, copy each complete folder, including templates, into a skill directory your host discovers. For a host that reads `~/.agents/skills/`, a first installation is:

```bash
mkdir -p ~/.agents/skills
cp -R skills/bf-use ~/.agents/skills/bf-use
```

Run this from the Brain Framework checkout only when that destination does not exist. For an existing installation, compare the folders and merge changes deliberately; preserve local customizations. Copy complete folders, including `references/` and `scripts/`. The Python package and `bf init` do not install skills.

Host discovery paths differ. A brain's `skills/` can hold versioned workflow packages, but placing files there alone does not make a host load them. Use the host's configured discovery directory or a host-supported link, then start a new session and confirm `bf-use` is available.

Ask the agent to search the intended brain, read one returned ref and cite it. This verifies the route from the host to your knowledge. Use `--brain PATH` for a work root; its direct `bf.yaml` brain references are also in scope; default selection otherwise depends on `BF_BRAIN`, the current directory and your registered brains. See the [agent walkthrough](../docs/docs/getting-started.md#give-agents-access) and [MCP alternative](../docs/docs/mcp.md).

For development of Brain Framework itself, use the repository-local [bf-contribute](../.agents/skills/bf-contribute/SKILL.md) skill.

## Decision workflows

`bf-action` adds a small working context, decisions with expected outcomes, conditional intentions and explicit unknowns ([context](bf-action/references/context.md), [decisions](bf-action/references/decisions.md)). `bf-learn` adds selected evidence captures, belief revision and bounded dependency review ([evidence](bf-learn/references/evidence.md)), procedures learned from outcomes ([consolidation](bf-learn/references/consolidate.md)) and reviewed transfer to another brain ([sharing](bf-learn/references/share.md)). Agents load a guide only when the task needs it; the default working packet is at most 300 words, six refs and 4 KiB, and dependency review stops after two hops or ten distinct dependents.

`bf-learn/scripts/evidence.py` captures an exact `bf read` reply or compares a retained capture with a new read. It uses the standard library only, reads stdin, prints JSON, runs no model and never opens a brain or runs a provider. Captures stay with the action; they are not general record versioning. See [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) and the [example brain](../examples/brain/README.md#review-a-decision), which demonstrates these workflows with fictional evidence.
