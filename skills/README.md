# FKF skills

These reusable Markdown packages help agents use an installed FKF base. They are distributed from this repository separately from the Python wheel and source distribution.

| Skill                           | Use                                                                    |
| ------------------------------- | ---------------------------------------------------------------------- |
| [fkf-use](fkf-use/SKILL.md)     | Retrieve bounded context and resolve exact evidence references.        |
| [fkf-learn](fkf-learn/SKILL.md) | Maintain concise, sourced knowledge through authorized Markdown edits. |

Copy each required directory into the target base’s `.agents/skills/` or your host’s native skill directory. Keep the base’s executable selection in its own instructions. Existing skills with the same name should be reviewed before replacement. FKF does not install skills or change host registrations.

For development of FKF itself, use the repository-local [fkf-contribute](../.agents/skills/fkf-contribute/SKILL.md) skill.
