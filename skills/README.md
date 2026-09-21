# FKF skills

These reusable Markdown packages help agents use an installed FKF base. They are distributed from this repository separately from the Python wheel and source distribution.

| Skill                                 | Use                                                                        |
| ------------------------------------- | -------------------------------------------------------------------------- |
| [fkf-use](fkf-use/SKILL.md)           | Retrieve bounded context and resolve exact evidence references.            |
| [fkf-maintain](fkf-maintain/SKILL.md) | Refresh and maintain an authorized base without deleting durable evidence. |
| [fkf-learn](fkf-learn/SKILL.md)       | Maintain concise, sourced knowledge through authorized Markdown edits.     |

Copy required packages into the target base’s canonical `skills/` directory, then expose reviewed packages through project-local `.agents/skills/` using supported links or copies. Installing into a host-wide skill catalog is a separate choice. Keep the base’s executable selection in its own instructions. Existing skills with the same name should be reviewed before replacement. FKF does not install skills or change host registrations.

For a host that discovers project-local `.agents/skills/`, run this from the base after copying and reviewing `skills/fkf-use/` (and confirm the destination does not already exist):

```bash
mkdir -p .agents/skills
ln -s ../../skills/fkf-use .agents/skills/fkf-use
```

Use a copy if the host does not support links. Repeat only for the workflows you need; preserve each skill folder, its `SKILL.md`, and any referenced templates. Review updates before replacing an installed package. See each skill’s `name` and `description` for its routing scope.

For development of FKF itself, use the repository-local [fkf-contribute](../.agents/skills/fkf-contribute/SKILL.md) skill.
