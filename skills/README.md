# FKF skills

Markdown packages that teach agents to use FKF. They are distributed from this repository, separately from the Python package.

| Skill                                 | Use                                                                         |
| ------------------------------------- | --------------------------------------------------------------------------- |
| [fkf-use](fkf-use/SKILL.md)           | Search and read notes and records from any repository.                      |
| [fkf-learn](fkf-learn/SKILL.md)       | Keep project notes, wiki concepts and tasks current after work.             |
| [fkf-maintain](fkf-maintain/SKILL.md) | Fix failing collectors, schedule updates, backfill and add retrieval cases. |

Install `fkf-use` where every agent host finds it, usually the user-wide `~/.agents/skills/fkf-use/`, so agents can reach your registered bases from any repository. Install `fkf-learn` and `fkf-maintain` the same way, or copy them into a base's `skills/` when only that base should use them. Review updates before replacing an installed copy.

For development of FKF itself, use the repository-local [fkf-contribute](../.agents/skills/fkf-contribute/SKILL.md) skill.
