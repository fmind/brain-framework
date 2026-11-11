---
icon: lucide/plug
description: Connect an agent through local MCP search and read tools, then verify access to a saved decision.
---

# Connect an agent with MCP

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) exposes BF's read-only **`search` and `read`** tools over local stdio (`bf mcp`) with no network port or hosted account. Terminal agents can also call the CLI directly ([Agent workflows](agents.md)).

## Before connecting

Complete [Getting started](getting-started.md), install the optional `mcp` extra and verify terminal retrieval with absolute paths:

```bash
uv tool install --python 3.14 'brain-framework[mcp]'
uv tool list --show-extras
command -v bf
bf read projects/new-website.md#decision --brain "$HOME/brain"
```

Pass absolute paths for both `bf` and `--brain` so hosts launched outside `~/brain` select the intended brain (`bf mcp` re-validates selection on each call).

## Host configuration

### Claude Code

```bash
claude mcp add --transport stdio --scope user brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
claude mcp get brain
```

Restart Claude Code and check `/mcp` ([Claude Code MCP setup](https://code.claude.com/docs/en/mcp); remove with `claude mcp remove brain --scope user`).

### Codex

```bash
codex mcp add brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
codex mcp get brain
```

Equivalent `~/.codex/config.toml` ([Codex MCP setup](https://developers.openai.com/codex/mcp); remove with `codex mcp remove brain`):

```toml
[mcp_servers.brain]
command = "/home/me/.local/bin/bf"
args = ["mcp", "--brain", "/home/me/brain"]
```

### goose

Add under `extensions:` in `~/.config/goose/config.yaml` (or via `goose configure`; [Using extensions](https://goose-docs.ai/docs/getting-started/using-extensions)):

```yaml
# https://goose-docs.ai/docs/getting-started/using-extensions#config-entry
extensions:
  brain:
    name: brain
    type: stdio
    cmd: /home/me/.local/bin/bf
    args: [mcp, --brain, /home/me/brain]
    enabled: true
    timeout: 300
```

### Other stdio hosts

Configure a local stdio server named `brain` running `/home/me/.local/bin/bf` with `["mcp", "--brain", "/home/me/brain"]`:

- **VS Code Copilot**: [Add and manage MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
- **Cursor**: [MCP configuration](https://cursor.com/docs/mcp)
- **OpenCode**: [Local MCP servers](https://opencode.ai/docs/mcp-servers/)

Ask your agent: **“Use the brain MCP tools to find why we chose a single product page. Read the matching source and cite its ref.”** Confirm in tool history that `search` (`{"query":"visitors clear explanation","scope":"projects"}`) and `read` (`{"ref":"projects/new-website.md#decision"}`) ran.

## If the connection fails

| Symptom                      | Fix                                                                              |
| ---------------------------- | -------------------------------------------------------------------------------- |
| Executable not found         | Pass the absolute `bf` path from `command -v bf`.                                |
| `bf mcp needs the mcp extra` | Run `uv tool install --python 3.14 'brain-framework[mcp]'` and restart the host. |
| Brain unavailable            | Test `bf read --brain /abs/path/to/brain` in a terminal.                         |
| Empty or incomplete results  | Inspect `problems`, `stale` and [source coverage](retrieval.md#source-coverage). |
| Cache write access error     | Ensure `.bf/` is writable by the host's user account.                            |

## What the host can read

- **Selected brain and direct `brains:` references** (or all registered brains when started outside any brain without `--brain` / `BF_BRAIN`).
- **Read-only access**: `search` and `read` refresh only the disposable `.bf/` cache and private usage counts, and accept `bf://NAME/...` addresses plus `rel` (`{"ref":"projects/new-website.md","rel":"cites"}`; [MCP tool contract](retrieval.md#mcp-tool-contract)). Review your host's model data policy ([privacy](privacy.md#your-agent-has-its-own-privacy-rules)).

## Why local stdio and two tools

- **No remote endpoint**: keeps private brains on the local machine without network listeners or accounts.
- **No MCP resources or prompts**: `read` pages (`review`, `projects`, `7d`) already browse the brain, and [Agent Skills](agents.md#install-the-skills) carry portable workflows.
- **No execution or write tools**: collection, routines and assistants run only from explicit CLI commands or user services, never from an MCP tool call.
