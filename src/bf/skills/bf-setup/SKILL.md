---
name: bf-setup
description: Set up Brain Framework (the bf command) and a brain, connect an agent to it, discover which sources are worth connecting and import selected documents or notes. Use when the user says "set up my brain", "install bf", "create a brain", "connect my agent", "install the skills", "which sources should I connect", "connect a source", "scan my tools or bookmarks", "import this handbook, vault or folder", or when bf is missing, older than 16 or has no brain yet.
license: MIT
compatibility: Requires Brain Framework 16 (the bf command) on Linux or macOS.
metadata:
  version: "16.0.1"
---

# bf-setup

Start from one project and a few questions the user keeps reconstructing across tools. A useful first result is one of those questions answered from the brain with its ref; a notes-only brain is a valid outcome. Reuse the directory, audience, sources and authorization the user already gave, and ask only for what is missing.

## Install and create the brain

1. Check `bf --version`. When it is missing or older than 16, install or upgrade only with the user's consent: `uv tool install --python 3.14 brain-framework`, or `uv tool upgrade brain-framework` after stopping watchers and reading the changelog's upgrade steps; run `uv tool update-shell` when `bf` is not on PATH. When the brain pins its own runtime with a `pyproject.toml` and `uv.lock`, check `uv run --project PATH --locked bf --version` instead.
1. Inspect an existing brain's `bf.yaml`, `AGENTS.md` and notes before changing anything; keep its name, customizations and unrelated work. For a new personal brain, default to `~/brain` unless the user chose a path, and run `bf init ~/brain`. It refuses a folder with content, derives the brain name from the directory (or `--name NAME`) and returns it as `brain`: use that name in `bf://` addresses. `bf register PATH` is optional: it lets search and read select the brain by name from anywhere and never runs its programs.
1. Save one project note grounded in what the user said: `type: project`, `status: draft`, the decision under `## Decision {#decision}` with its reason, and a next task. Follow the `bf-use` skill's guide to writing knowledge back; never invent a decision to fill the example.
1. Verify inside the brain directory (or with `--brain PATH` everywhere):

   ```bash
   bf read
   bf search "words from the decision's reason"
   bf read 'projects/PROJECT.md#decision'
   bf validate
   bf eval
   ```

   Expect the saved reason at its ref, `"valid":true` and `"score":"3/3"` for the starter retrieval cases. Add the user's real questions as cases with expected refs and answer fragments, plus one query that must stay empty, to a suite under `evals/` starting with `version: 7` ([retrieval cases](https://fmind.github.io/brain-framework/docs/checks/)). Starter checks alone do not prove the user's questions are answered.

## Connect the agent

1. A terminal agent can use the CLI directly: first try an explicit request such as "Use `bf search` to find why we chose X, then `bf read` the ref and cite it."
1. Install the skills into the directory the host discovers, such as `~/.agents/skills` or `~/.claude/skills`:

   ```bash
   bf skills ~/.agents/skills
   ```

   It installs or updates `bf-use`, `bf-setup` and `bf-maintain` from the installed package and never overwrites a skill the user edited: an edited folder is reported as `modified` and left alone unless the user asks for `--force`. `bf skills DIR --check` reports drift after an upgrade without writing. Start a fresh host session so it discovers them.
1. For a host that prefers tools, follow the [MCP guide](https://fmind.github.io/brain-framework/docs/mcp/): `bf mcp --brain PATH` serves only `search` and `read`.
1. Optional [hooks](https://github.com/fmind/brain-framework/tree/main/examples/hooks) add the repository's project at session start and matching note refs to each prompt in Claude Code or Codex; register them only when the user wants one search per prompt.

Explain the data boundary before connecting a cloud agent: its provider receives the notes and records it reads, even though retrieval itself is offline. Adding a related brain widens what retrieval can return.

From a fresh host session, ask a real question, check in the tool history that the agent searched the intended brain, read the source and cited its ref. Installing a skill or registering a server proves configuration only; when the session cannot be observed, report verification as pending with the exact question to try.

## Discover and connect sources

Skip discovery when the user already named the sources. Otherwise follow [scoped discovery](references/scan.md): agree on an inspection scope first, inspect bounded evidence with [discovery methods](references/discovery.md) and `scripts/inventory.py`, and return a ranked recommendation. Discovery never authenticates, collects or schedules.

For each selected source, record the question it answers, its account, folder or repository scope, the fields to keep, exclusions, the freshness it needs and a retrieval case. Hand the implementation to the `bf-maintain` skill, which owns sensors, fake-provider tests, live runs and refresh; prepare disabled, tested configuration before asking for missing execution authority.

## Import selected material

To make an identified document, website, repository, export, note vault or other brain useful, follow [imports](references/import.md). An import is an agent procedure, not a core feature: OKF stays the only note format, so imported Markdown becomes OKF projects or concepts, and changing material usually becomes an overview with canonical links rather than a copy.

## Finish with evidence

Report the brain location, the questions answered with their refs, validation and evaluation results and the host access you observed. Separate proposed, configured, collected and scheduled sources, and name unresolved access or evidence gaps. Keep reviewed setup decisions in the owning project, never raw discovery inventories. For a team pilot, the [pilot guide](https://fmind.github.io/brain-framework/docs/team/#evaluate-a-pilot) records comparison conditions, effort and a next review; leave unmeasured outcomes unknown.

## References and helpers

Helper paths are relative to this skill's folder; run them with `python3` (3.11 or later).

- [references/scan.md](references/scan.md): agree on an inspection scope, recommend sources and hand them off.
- [references/discovery.md](references/discovery.md): bounded methods for tools, applications, bookmarks and project folders.
- [references/import.md](references/import.md): turn a selected source into OKF notes, links or retained evidence.
- [scripts/inventory.py](scripts/inventory.py): check named tools without running them, or summarize a bookmark export as host counts.
