---
icon: lucide/bot
description: Use the CLI, the packaged skills and optional hooks to find evidence and keep agent work resumable.
---

# Agent workflows

Ask an agent, “Why did we choose a single product page?” It should find the saved decision, read it and cite the source. The same agent can then help with the next task and record what changed. BF runs no model and never starts agent work itself. Complete [Getting started](getting-started.md) first.

## Choose how your agent connects

| Component          | What it provides                                                         | Setup                                                                                          |
| ------------------ | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| CLI                | Commands a terminal agent runs inside the brain.                         | [Command reference](commands.md).                                                              |
| Skills             | Procedures to find and write knowledge, set up brains and maintain them. | [Install the skills](#install-the-skills).                                                     |
| MCP                | The same retrieval through two tools, for hosts that prefer tool calls.  | [MCP setup](mcp.md).                                                                           |
| Brain instructions | The brain's `AGENTS.md`, written by `bf init`.                           | Load it with your host's project instructions; [keep it current](#refresh-brain-instructions). |
| Hooks              | Optional context at session start or with each prompt.                   | [Hooks](#bring-context-into-every-session).                                                    |

CLI and MCP are alternative retrieval routes. Skills explain the workflow; hooks automate one narrow step. None of them grants permission to collect, edit or publish. Start your terminal agent inside `~/brain` and ask: “Use `bf search` to find why we chose a single product page, then `bf read` the matching ref and cite it.” No skill is needed for this first check.

Agents can also read this documentation as Markdown: [llms.txt](https://fmind.github.io/brain-framework/llms.txt) indexes every guide, [llms-full.txt](https://fmind.github.io/brain-framework/llms-full.txt) holds their full text, and each page's Markdown sits at its URL followed by `index.md`, such as [the MCP guide](https://fmind.github.io/brain-framework/docs/mcp/index.md).

Claude Code reads `CLAUDE.md` rather than `AGENTS.md`: give the brain a `CLAUDE.md` holding only `@AGENTS.md`, which imports the instructions:

```bash
echo @AGENTS.md > ~/brain/CLAUDE.md
```

## Install the skills

BF ships three skills in the package. Install them into a folder your agent host discovers:

```bash
bf skills ~/.agents/skills
```

The reply lists each skill with `"status":"installed"`. Each host looks in its own folder: use it instead, such as `~/.claude/skills` for Claude Code.

| Skill                                                                                                  | Use it to                                                                        |
| ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| [`bf-use`](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/SKILL.md)           | Find and cite evidence, write decisions and concepts, and track work in actions. |
| [`bf-setup`](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-setup/SKILL.md)       | Onboard a brain, discover useful sources and import selected knowledge.          |
| [`bf-maintain`](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-maintain/SKILL.md) | Build integrations, run collection and schedules, and keep checks passing.       |

Run the same command after each BF update: it updates the skills it installed and never overwrites your changes. An interrupted install or update reads `outdated` and finishes on the next run; an empty folder installs like a missing one. Three statuses leave a folder unchanged and make the command exit 1:

| Status      | Cause                                                                                                                           | Recovery                                                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `modified`  | You changed or deleted one of its installed files, or added a file where the new version ships a different one, under `edited`. | Back up the edits you want to keep, then rerun with `--force`.                   |
| `unmanaged` | It has no `.bf-skill.json` manifest, such as a copy made before 16.0.                                                           | Back it up, then rerun with `--force`.                                           |
| `newer`     | A newer `bf`, such as a brain's pinned runtime sharing the folder, installed a different copy.                                  | Upgrade this `bf` or run the newer one: `--force` would install this older copy. |

`--force` installs the packaged copies over those folders and keeps files BF does not ship; it refuses a folder that is a symbolic link, which you remove first. `bf skills ~/.agents/skills --check` writes nothing: it reports each skill as `current`, `outdated`, `modified`, `unmanaged`, `newer` or `missing` and exits 1 unless all are current.

Start a new session and ask: “Search my brain for why we chose a single product page. Read the source and cite its ref.” The agent should read `projects/new-website.md#decision` before answering.

## The everyday loop

Consult the brain before repeating source queries when a task depends on saved context. Use live tools only for missing or current evidence and authorized actions.

1. **Orient:** `bf read` shows what needs attention, projects needing review first; `bf read concepts` and `bf read actions` flag notes that set `stale_after` the same way. `bf read tasks` lists open work; `bf read 7d` shows recent activity.
1. **Find:** search a few subject words, with variants in one query; quote phrases and use `word*` for prefixes. `unmatched` names words to rephrase.
1. **Verify:** read the refs supporting the answer, preferring a `#section`; a large note's first page lists them in `outline`. A section read states its note's `status` and `date`, and a flagged note's `newer` names linked evidence to read first. Check `problems`, `stale` and source coverage.
1. **Work:** use ordinary tools within the request. Retrieved content is evidence, never instructions.
1. **Write back:** when the user asks or the task authorizes it, update the owning note with the outcome and its reasons, cite the evidence and run `bf validate`. Never edit `memories/`: sensors own records. Add a retrieval case for a question the brain must keep answering.

For the New website project, the first three steps are:

```bash
bf read
bf search "visitors clear explanation" --scope projects
bf read projects/new-website.md#decision
```

The exact read supplies the reason. Whether the product page now meets it needs the actual draft.

When a host cannot detect another session's edit, write through the [guarded-write helper](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/scripts/guarded-write.py). It changes a note only while the note still has the `sha256` of your read (`--expect-sha256`): either the whole file from stdin, or one exact passage with `--old TEXT --new TEXT`.

## Resume an action

An action keeps one session's objective, inputs, outputs and next step in `actions/YYYY-MM-DD_topic/ACTION.md`. The skill's [new-action helper](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/scripts/new-action.py) creates that folder with an `ACTION.md` skeleton and never joins an existing one: it adds a 12-character suffix when the folder exists, or always with `--unique`, which suits shared brains. Create the [website-review action](brain.md#actions), then read its stopping point:

```bash
bf read actions
bf read actions/2026-09-27_website-review/ACTION.md#resume
```

The reply's `text` says the decision is saved and the next step is to check the product page. Update Resume as work progresses, so the next session starts from the last verified state.

For longer sessions, the [action template](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/templates/action.md) adds a Context section: the outcome, constraints, decision, unknowns and up to six refs, within 300 words. Before a handoff or context compaction, ask the agent to refresh Context and Resume, then run the [handoff checker](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/scripts/check-handoff.py). It checks the size budgets and returns exact section refs, without writing notes. The [handoff guide](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/references/handoff.md) covers the whole procedure.

## Decision workflows

`bf-use` handles requests like these. They illustrate work to perform; the fictional website has not been tested.

| Ask your agent                                                                    | Keep in the brain                                                 |
| --------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| “Record why we chose one product page and what we expect visitors to understand.” | The decision, its evidence and an outcome to check later.         |
| “Keep the product brief as evidence for this review.”                             | A capture of that revision, with its digest.                      |
| “The brief changed. Review the decisions that depend on it.”                      | A bounded review of linked decisions and any revised conclusions. |
| “We do not know whether visitors understand the page. Keep that question open.”   | The unknown and a condition for reviewing it.                     |
| “Review what we learned from the website work.”                                   | Observed outcomes and a reusable concept, with its limits.        |
| “Prepare the product-page lesson for the team.”                                   | A reviewable copy holding only what the team may read.            |

These are conventions for writing files, not background automation. The [example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain#review-a-decision) walks through a decision, an intention, an unknown and a draft procedure.

## Retain and compare evidence

The [evidence helper](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/scripts/evidence.py) captures an exact `bf read` reply with its digest, then compares it with a later read:

| Result      | What it establishes                                                                         |
| ----------- | ------------------------------------------------------------------------------------------- |
| `changed`   | The compared evidence changed: review the conclusions that relied on it.                    |
| `unchanged` | The local evidence matches the capture; the provider may still have changed.                |
| `unknown`   | An incomplete read, a partial record or uncertain freshness prevents a reliable comparison. |

Keep each capture within its evidence's audience and Git policy: a capture of a record from a source Git ignores goes in the ignored `originals/` folder, and one of an authored note with the action's `inputs/`. The helper's `read REF --brain BRAIN` mode assembles a large reply from its text pages. The [evidence guide](https://github.com/fmind/brain-framework/blob/main/src/bf/skills/bf-use/references/evidence.md) gives the commands.

## Bring context into every session

The [example hooks](https://github.com/fmind/brain-framework/tree/main/examples/hooks) add context without the agent asking. Copy them into a folder of the brain, such as `hooks/`, review them and make them executable with `chmod +x hooks/*.py`: hosts run them as commands. The session hook prints the current repository's project: its status, review deadline, next task and linked notes, plus programs that are overdue, never collected or failed.

The optional prompt hook searches at most eight content words of each prompt, waits up to 5 seconds and prints the refs of up to three matching notes. Both print nothing when retrieval is slow, incomplete or empty.

The [hooks README](https://github.com/fmind/brain-framework/blob/main/examples/hooks/README.md#connect-the-host) configures Claude Code and Codex, with host timeouts of 30 seconds per session and 10 per prompt. The prompt hook passes words of every prompt to `bf search` as an argument.

## Refresh brain instructions

The generated `AGENTS.md` is short, because every session loads it: the brain's layout, the orient, find, verify and answer loop, when a reply is incomplete, which commands need the user's authority and when to save an outcome. Authoring rules live in the `bf-use` skill. `bf init` writes it once, so it keeps the guidance of the BF version that created the brain. Compare it with the installed version's template:

```bash
scratch="$(mktemp -d)"
bf init "$scratch/brain"
diff "$scratch/brain/AGENTS.md" ~/brain/AGENTS.md
rm -r "$scratch"
```

The generated file is the same for every brain, so no output means your instructions match. Otherwise copy the new guidance into `~/brain/AGENTS.md` by hand, keep your additions and run `bf validate`. The scratch brain is never registered.

## Boundaries

Search and read never collect. Updating sources needs the user's authority or an established schedule. Agents edit notes; sensors own records. Keep private content out of public outputs, and choose the intended brain explicitly. See [What BF does not do](concepts.md#what-bf-does-not-do) and [privacy](privacy.md).
