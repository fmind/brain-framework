---
name: bf-setup
description: Install Brain Framework (bf), create a brain, connect an agent, install the skills, choose sources worth connecting and import documents or notes. Use for "set up my brain", "install bf", "which sources should I connect", "import this vault or folder", or when bf or a brain is missing.
license: MIT
compatibility: Requires Brain Framework 20 (the bf command) on Linux or macOS.
---

# bf-setup

Start from one project and a few questions the user keeps reconstructing across tools: a useful first result answers one of them from the brain with its ref, and a notes-only brain is a valid outcome. Reuse the directory, audience, sources and authorization the user already gave; ask only for what is missing.

## Install and create the brain

1. Check `bf --version` against this skill's `compatibility`. Install a missing `bf` only with the user's consent: `uv tool install --python 3.14 brain-framework`, then `uv tool update-shell` when `bf` is not on PATH. Upgrade an older one only with consent, through the `bf-maintain` skill; for a newer one, update the skills (below).
1. Inspect an existing brain's `bf.yaml`, `AGENTS.md` and notes before changing anything, and keep its name and customizations. Check it with the installed `bf`, never its pinned runtime: when `bf status --brain PATH` shows a `runtime.pinned` whose major version differs from `runtime.installed`, report it and ask.
1. For a new personal brain, run `bf init ~/brain` unless the user chose another path. It refuses a folder with content and returns the brain name `bf://` addresses use, derived from the directory; pass `--name NAME` when a letter has no plain form, such as `ø`. `bf register PATH` optionally lets search and read find the brain by name; rerun it after a move.
1. Save one project note from what the user said, following the `bf-use` skill: `type: project`, `status: draft`, the decision and its reason under `## Decision {#decision}`, and a next task. Never invent a decision.
1. Verify inside the brain directory (or with `--brain PATH` everywhere):

   ```bash
   bf read
   bf search "words from the decision's reason"
   bf read 'projects/PROJECT.md#decision'
   bf validate
   bf eval
   ```

   Expect the saved reason at its ref, `"valid":true` and `"score":"3/3"` for the starter cases, which prove nothing about the user's questions: add those as [retrieval cases](https://fmind.github.io/brain-framework/docs/checks/), plus one query that must stay empty.

## Connect the agent

1. A terminal agent can use the CLI directly: "Use `bf search` to find why we chose X, then `bf read` the ref and cite it." Hosts, including Claude Code 2.1.277+, load the brain's `AGENTS.md`; add no `CLAUDE.md` here or in a parent folder, where it would replace `AGENTS.md`, except one holding only `@AGENTS.md` for an older host.
1. Install the skills where the host discovers them, such as `~/.agents/skills` or `~/.claude/skills`, then start a fresh host session:

   ```bash
   bf skills ~/.agents/skills
   ```

   A folder reported `modified` (the user edited it), `unmanaged` (BF did not install it) or `newer` (a newer `bf` installed it) stays unchanged and the command exits 1. Pass `--force` only when the user accepts replacing the first two; for `newer`, upgrade this `bf` instead ([statuses](https://fmind.github.io/brain-framework/docs/agents/#install-the-skills)).
1. For a host that prefers tools, `bf mcp --brain PATH` serves only `search` and `read`; configure absolute paths ([MCP guide](https://fmind.github.io/brain-framework/docs/mcp/)). It needs the optional MCP SDK: with the user's consent, run `uv tool install --python 3.14 'brain-framework[mcp]'` when `bf mcp` reports `needs the mcp extra`.
1. Optional [hooks](https://github.com/fmind/brain-framework/tree/main/examples/hooks) add the repository's project at session start and matching refs to each prompt.

Before connecting a cloud agent, explain that its provider receives the notes and records it reads, although retrieval is offline, and that a related brain widens what it can read.

From a fresh host session, ask a real question and check in the tool history that the agent searched the intended brain, read the source and cited its ref: installation proves configuration only. When the session cannot be observed, report verification as pending with the question to try.

## Discover, connect and import sources

Skip discovery when the user already named the sources. Otherwise follow [scoped discovery](references/scan.md) and its [methods](references/discovery.md): agree on an inspection scope, inspect bounded evidence and return a ranked recommendation, without authenticating, collecting or scheduling. Hand each selected source to the `bf-maintain` skill with its question, scope, fields, exclusions, needed freshness and a retrieval case.

To bring in an identified document, website, repository, export, note vault or other brain, follow [imports](references/import.md).

## Finish with evidence

Report the brain location, the questions answered with their refs, check results and the host access you observed. Separate proposed, configured, collected and scheduled sources, and name access or evidence gaps. Keep setup decisions in the owning project, never raw discovery inventories. For a team pilot, follow the [pilot guide](https://fmind.github.io/brain-framework/docs/team/#evaluate-a-pilot).

## References and helpers

`SKILL_DIR` is the folder holding this `SKILL.md`. Run a helper as `python3 "$SKILL_DIR/scripts/NAME.py"` (Python 3.11+); when `python3` is older, as on macOS Command Line Tools, use `"$(uv python find --system --no-config --no-project 3.14)"` in its place, never `uv run`. For a contract this skill does not give, read the [documentation](https://fmind.github.io/brain-framework/llms.txt) as Markdown; `bf COMMAND --help` wins where they differ.

- [references/scan.md](references/scan.md): agree on a scope, recommend sources, hand them off.
- [references/discovery.md](references/discovery.md): bounded methods for tools, bookmarks and folders.
- [references/import.md](references/import.md): turn a selected source into OKF notes or retained evidence.
- [scripts/inventory.py](scripts/inventory.py): check named tools without running them, or count a bookmark export's hosts.
