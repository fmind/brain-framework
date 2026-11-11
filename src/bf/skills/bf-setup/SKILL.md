---
name: bf-setup
description: Install Brain Framework (bf), create a brain, connect an agent, install the skills, choose sources worth connecting and import documents or notes. Use for "set up my brain", "install bf", "which sources should I connect", "import this vault or folder", or when bf or a brain is missing.
license: MIT
compatibility: Requires Brain Framework 22 (the bf command) on Linux or macOS.
---

# bf-setup

Start from one real project and the recurring questions the user reconstructs across tools: a notes-only brain answering one question with its ref is already complete. Ask only for missing scope or consent.

## Install and create the brain

1. Check `bf --version` against `compatibility`. With user consent, install missing `bf` via `uv tool install --python 3.14 brain-framework` (`uv tool update-shell` if needed), upgrade older versions via `bf-maintain`, or update skills below.
1. Inspect an existing brain's `bf.yaml`, `AGENTS.md` and notes with the installed `bf` (never its pinned runtime; ask if `bf status --brain PATH` shows a `runtime.pinned` major mismatch).
1. Create a new brain with `bf init ~/brain` (`--name NAME` when directory letters lack ASCII forms; `bf register PATH` for optional name lookup).
1. Save one real `projects/*.md` note (`type: project`, `status: draft`, `## Decision {#decision}` and one `- [ ]` next task — never invented) and verify:

```bash
bf read
bf search "words from the decision's reason"
bf read 'projects/PROJECT.md#decision'
bf validate
bf eval
```

Add the user's real questions (plus one negative case) to `evals/` ([checks](https://fmind.github.io/brain-framework/docs/checks/)).

## Connect the agent

1. Terminal hosts load the brain's `AGENTS.md` directly (avoid adding a competing `CLAUDE.md` unless an older host needs `@AGENTS.md`). Install skills globally or inside the brain, then start a fresh session:

   ```bash
   bf skills ~/.agents/skills
   bf skills skills --link # inside a brain: installs ./skills and links host skill folders
   ```

   `modified` (edited), `unmanaged` (foreign or symlink) and `newer` (installed by a newer `bf`) leave folders untouched and exit 1: use `--force` only with consent on the first two, and upgrade `bf` for `newer` ([skills](https://fmind.github.io/brain-framework/docs/agents/#install-the-skills)).
1. For MCP hosts, configure `bf mcp --brain PATH` with absolute paths (`uv tool install --python 3.14 'brain-framework[mcp]'` with consent when needed; [MCP guide](https://fmind.github.io/brain-framework/docs/mcp/)). Optional [hooks](https://github.com/fmind/brain-framework/tree/main/examples/hooks) surface project context at session start.
1. Note before connecting cloud hosts that retrieved passages reach the model provider, and verify in a fresh session that the host searched, read and cited the intended brain.

## Discover, import and hand off

- When sources are unchosen, run [scoped discovery](references/scan.md) ([methods](references/discovery.md)) without authenticating, collecting or scheduling, then hand approved sources to `bf-maintain`.
- To import documents, exports, vaults or another brain into OKF notes, follow [import.md](references/import.md). For team rollouts, see [evaluate a pilot](https://fmind.github.io/brain-framework/docs/team/#evaluate-a-pilot).
- Report brain path, verified refs, checks, observed host access and separate proposed/configured/collected/scheduled states.

## References and helpers

Run helpers literally as `python3 SKILL_DIR/scripts/NAME.py` (Python 3.11+, or `"$(uv python find --system --no-config --no-project 3.14)"`, never uv's run command). Contracts live in [llms.txt](https://fmind.github.io/brain-framework/llms.txt) and `bf COMMAND --help`.

- [references/scan.md](references/scan.md): agree on discovery scope, rank candidates and hand off.
- [references/discovery.md](references/discovery.md): bounded local checks for tools, bookmarks and folders.
- [references/import.md](references/import.md): convert external material or vaults into OKF notes.
- [scripts/inventory.py](scripts/inventory.py): check `PATH` tools without running them or count bookmark hosts.
