# FKF skills

Markdown packages that teach agents to use FKF. They are distributed from this repository, separately from the Python package.

| Skill                                 | Use                                                                         |
| ------------------------------------- | --------------------------------------------------------------------------- |
| [fkf-use](fkf-use/SKILL.md)           | Search and read notes and records from any repository.                      |
| [fkf-learn](fkf-learn/SKILL.md)       | Keep project notes, wiki concepts and tasks current after work.             |
| [fkf-maintain](fkf-maintain/SKILL.md) | Fix failing collectors, schedule updates, backfill and add retrieval cases. |

## Install

Start with `fkf-use`; add `fkf-learn` to maintain notes after work and `fkf-maintain` for base operations. From a reviewed checkout of the release you use, copy each complete folder, including templates, into a skill directory your host discovers. For a host that reads `~/.agents/skills/`, a first installation is:

```bash
mkdir -p ~/.agents/skills
cp -R skills/fkf-use ~/.agents/skills/fkf-use
```

Run this from the FKF checkout only when that destination does not exist. For an existing installation, compare the folders and merge changes deliberately; preserve local customizations. The Python package and `fkf init` do not install skills.

Host discovery paths differ. A base's `skills/` can hold versioned workflow packages, but placing files there alone does not make a host load them. Use the host's configured discovery directory or a host-supported link, then start a new session and confirm `fkf-use` is available.

Ask the agent to search the intended base, read one returned ref and cite it. This verifies the route from the host to your knowledge. Use `--base NAME` for a work context; default selection otherwise depends on `FKF_BASE`, the current directory and your registered bases. See the [agent walkthrough](../docs/docs/getting-started.md#give-agents-access) and [MCP alternative](../docs/docs/mcp.md).

For development of FKF itself, use the repository-local [fkf-contribute](../.agents/skills/fkf-contribute/SKILL.md) skill.
