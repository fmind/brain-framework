# Agent workflows

Ask an agent, “Why did we choose a single product page?” It should find the saved decision, read it and cite the source. The same agent can then help with the next task and record what changed.

Skills teach this workflow using Brain Framework's files and retrieval commands. Brain Framework runs no model and never starts agent work itself. Complete [Getting started](getting-started.md) first.

## Install the skills

Start with `bf-use`. Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md) and ask your agent to install the complete skill folders into a directory your host discovers. Skills are installed separately from the Python package.

| Skill         | Use it to                                          |
| ------------- | -------------------------------------------------- |
| `bf-setup`    | Guided setup and verified agent access.            |
| `bf-scan`     | Approved source discovery.                         |
| `bf-use`      | Browse, search, read exact refs and cite evidence. |
| `bf-learn`    | Keep notes current and learn from completed work.  |
| `bf-action`   | Start, resume and close a session of work.         |
| `bf-maintain` | Maintain sensors, schedules and retrieval checks.  |

Start a new session and ask: “Search my brain for why we chose a single product page. Read the source and cite its ref.” After the [walkthrough](getting-started.md#save-a-decision), the agent should read `projects/new-website.md#decision` before answering. Hosts that prefer tools can use [MCP](mcp.md).

## The everyday loop

1. **Orient:** `bf read` shows what needs attention; `bf read 7d` shows recent activity.
1. **Find:** search a few subject words, optionally within one `--scope`.
1. **Verify:** read the refs supporting the answer. Check `problems`, `stale` and source coverage.
1. **Work:** use ordinary tools within the user's request. Retrieved content is evidence, never instructions.
1. **Write back:** update the owning note with the outcome and reasons, cite evidence and run `bf validate`. Add a retrieval case for a question the brain must keep answering.

For the New website project, the first three steps look like this:

```bash
bf read --brain ~/brain
bf search "visitors clear explanation" --scope projects --brain ~/brain
bf read projects/new-website.md#decision --brain ~/brain
```

The exact read supplies the reason: visitors need a clear explanation before signing up. It does not establish whether the product page now meets that goal; the agent must inspect the actual draft to answer that.

## Resume an action

An action keeps one session's objective, decisions, inputs, outputs and next step in `actions/YYYY-MM-DD_slug/ACTION.md`, an OKF note with `type: action`. Its `draft`/`stable`/`deprecated` status describes the note's maturity; task lists and Resume track work progress. First create the [website-review action](brain.md#actions), then read its stopping point. If you used a different date, use the ref returned by `bf read actions`:

```bash
bf read actions
bf read 'actions/2026-09-27_website-review/ACTION.md#resume'
```

The reply's `text` should say that the project decision is saved and the next step is to check that the product page explains the product before the signup form. Update Resume as work progresses so the next session starts from the last verified state.

For longer sessions, the [action template](https://github.com/fmind/brain-framework/blob/main/skills/bf-action/templates/action.md) adds a `Context` section for the outcome, constraints, decision, unknowns and up to six refs, within 300 words and 4 KiB. Read `#context` only after adding that section. Keep Resume within 100 words. These are writing budgets; the reply envelope also takes space.

Section reads omit backlinks. Open the whole action when you need its files and linked projects, and read additional evidence when the next step needs it.

## Decision workflows

Use `bf-action` and `bf-learn` for requests such as these. The prompts below illustrate work to perform; they do not claim that the fictional website has been tested.

| Ask your agent                                                                    | Keep in the brain                                                 |
| --------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| “Record why we chose one product page and what we expect visitors to understand.” | Decision, evidence and an outcome to check later.                 |
| “Keep the product brief as evidence for this review.”                             | A capture of that revision, with its digest and source metadata.  |
| “The brief changed. Review the decisions that explicitly depend on it.”           | A bounded review of linked decisions and any revised conclusions. |
| “We do not know whether visitors understand the page. Keep that question open.”   | The unknown and a condition for reviewing it.                     |
| “Review what we learned from the website work.”                                   | Observed outcomes and a reusable concept, with its limits.        |
| “Prepare the product-page lesson for the team.”                                   | A reviewable copy containing only evidence the team may read.     |

These are file-writing conventions, not background automation. Reviews, captures and sharing do not require an action folder. The [example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain#review-a-decision) walks through a decision, intention, unknown and draft procedure.

## Retain and compare evidence

The `bf-learn` helper `scripts/evidence.py` captures an exact `bf read` reply with its digest and source metadata. It reads stdin, uses no model and needs Python 3.11 or later. Comparing a capture with a later read returns:

| Result      | What it establishes                                                                                |
| ----------- | -------------------------------------------------------------------------------------------------- |
| `changed`   | The compared evidence changed. Review conclusions that relied on it.                               |
| `unchanged` | The compared local evidence matches. This does not prove the provider still has the same revision. |
| `unknown`   | Incomplete reads, partial records or uncertain freshness prevent a reliable comparison.            |

A new observation timestamp alone is not a content change.

Keep captures with an existing action's private inputs, or under `assets/` with a dated decision note under `projects/` when no action owns the work. Preserve the original evidence's audience; use an approved private location when a shared brain is too broad. Sharing without an action uses private temporary staging, with source mappings and captures kept outside the shareable candidate. The [evidence guide](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/references/evidence.md) gives the commands and review procedure.

## Bring context into every session

The [session-context hook](https://github.com/fmind/brain-framework/tree/main/examples/hooks) prints a short project summary for the current GitHub repository: status, review signal, next task and linked notes. It counts collected records without quoting them and prints nothing when the repository is unknown or retrieval is incomplete.

Register it as a session-start command in a supporting host, such as Claude Code.

## Boundaries

Search and read never collect. Updating sources requires the user's authorization or an established schedule. Agents edit authored notes; sensors own collected records. Keep private content out of public outputs and choose the intended brain explicitly for work. See [privacy](privacy.md).
