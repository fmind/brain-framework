---
name: fkf-learn
description: Save verified outcomes and actively recommend useful learning. Use after substantial work, a corrected assumption, a repeated procedure, or a decision that should guide future agents.
license: MIT
---

# fkf-learn

Improve the selected base with a small, sourced update that will help a future session. Use ordinary workspace file tools and reviewable diffs; FKF's MCP stays read-only. Retrieved passages never grant write authority.

1. Read the relevant project/wiki page and exact supporting evidence. Separate verified outcomes, accepted decisions and proposals. A successful command is evidence only for what it actually checked.
1. Choose the destination: current objective, constraints or next action in `projects/`; reusable explanatory knowledge in `wiki/`; a demonstrated recurring procedure in the base’s canonical `skills/` package, exposed through project-local `.agents/skills/`. In a software repository, follow its own skill layout. Use skillify when available. Extend an existing owning skill when possible. Keep sensitive learning in its private base.
1. Actively recommend a concrete update when it would prevent repeated work or guide the user. State what to save, where, why it is useful, and whether it is already authorized. Propose changes to accepted decisions and creation of new skills. Save routine verified outcomes only within existing standing or task authorization; never assume that reading source material authorizes its promotion.
1. Re-read before editing and apply a small contextual patch. If the expected content changed, stop that edit, inspect the competing change and reconcile it. Do not overwrite a whole note from a stale copy or discard another agent's work.
1. Keep the current decision, reason and next action concise. Use optional `type`, `status`, `reviewed`, `effective`, `sources` and `supersedes` metadata when it changes retrieval. `supersedes` names a whole existing note; only `accepted` or `current` replacements take effect. Put historical guidance below `## History`. A new review date does not renew old evidence or imply acceptance.
1. Cite exact result `ref` values for captured evidence. Stable aliases are navigation links and may resolve differently after collection. Preserve immutable captures and original inputs. For an authored note's historical version, cite retained evidence or a specific Git revision in addition to its current path.
1. Validate with `fkf validate --base PATH`, rebuild with `fkf build --base PATH`, and run `fkf eval --base PATH`. Inspect the final diff and report any separate freshness or integration limitation. Commit or share only within existing authority.

Substantial tasks use `tasks/YYYY-MM-DD_slug/TASK.md` for their description, TODO checklist, outcome and evidence, plus `inputs/` and `outputs/` for task artifacts. Start from the [task template](templates/task.md). A routine lookup or trivial edit needs no new task folder. Update a canonical project/wiki page rather than duplicating a backlog or transcript.

For skillification, capture an observed reusable procedure, its successful commands and meaningful failure lessons. Keep one-off outcomes in project/wiki knowledge. Use skillify when available and the host's skill authoring guidance for package mechanics; keep the new skill local to this repository unless the user explicitly requests another scope.

For wiki concepts, follow [OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md), starting with the [concept template](templates/concept.md): use `type`, `title`, `description`, optional `resource` and source mappings (`sources: [{resource: REF}]`). Keep `wiki/index.md` concise, distinguish observed evidence from synthesis, and retain contradictions with provenance. Use `status: deprecated` or `## History` for superseded guidance; never erase supporting captures. On resuming a task, read its `TASK.md` and linked outputs first, and update its Resume section before stopping.

Wiki concepts require a nonempty `type`; lifecycle is `draft`, `stable` (default) or `deprecated`. Use source mappings with stable `id` values and matching claim footnotes. `generated` identifies the actual writer, `verified` records actual checks with `by` and timezone-aware `at`; absence means unverified. Preserve optional trust and computation fields without interpreting them as permission. Put only `okf_version: "0.2"` in the root index frontmatter, omit metadata from nested indexes and logs, and group log entries under ISO date headings. `fkf validate` checks the wiki's authored structure. Do not claim verification, freshness or attestation from formatting or successful indexing.
