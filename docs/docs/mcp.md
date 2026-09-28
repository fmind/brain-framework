---
description: Connect an agent through local MCP search and read tools, then verify access to a saved decision.
---

# Connect an agent with MCP

[MCP (Model Context Protocol)](https://modelcontextprotocol.io/) lets an agent host call BF's **`search` and `read`** tools. The host launches `bf mcp` as a local stdio process; no network port or BF account is needed.

A terminal agent can also use the CLI directly. See [Agent workflows](agents.md) for skills, instructions, hooks and write-back procedures.

## Before connecting

Complete [Getting started](getting-started.md), then check the same decision from your terminal:

```bash
cd ~/brain
command -v bf
bf read projects/new-website.md#decision
```

Use the absolute executable path and brain path in host settings. **MCP is one place where `--brain` is useful:** hosts may start the server from another directory.

## Claude Code

If a server named `brain` exists, inspect it first with `claude mcp get brain`; choose another name to preserve it.

```bash
claude mcp add --transport stdio --scope user brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
claude mcp get brain
```

Restart Claude Code, run `/mcp` and confirm that `brain` connects. See [Claude Code MCP setup](https://code.claude.com/docs/en/mcp) for scopes and troubleshooting. Remove this connection with `claude mcp remove brain --scope user` when no longer needed.

## Codex

If a server named `brain` exists, inspect it first with `codex mcp get brain`; choose another name to preserve it.

```bash
codex mcp add brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
codex mcp get brain
```

Equivalent entry in `~/.codex/config.toml`, using your absolute paths:

```toml
# https://developers.openai.com/codex/mcp
[mcp_servers.brain]
command = "/home/me/.local/bin/bf"
args = ["mcp", "--brain", "/home/me/brain"]
```

Restart Codex and run `/mcp`. See [Codex MCP setup](https://developers.openai.com/codex/mcp) for host options. Remove this connection with `codex mcp remove brain` when no longer needed.

## Other agent hosts

Use the host's **local stdio** configuration with these values; replace the paths:

| Field       | Value                                  |
| ----------- | -------------------------------------- |
| Server name | `brain`                                |
| Executable  | `/home/me/.local/bin/bf`               |
| Arguments   | `["mcp", "--brain", "/home/me/brain"]` |
| Tools       | `search`, `read`                       |

Configuration formats differ, so follow the owning documentation:

| Host                  | Official setup                                                                                                              |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Copilot in VS Code    | [Add and manage MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers); use a local stdio server. |
| Cursor                | [MCP configuration](https://cursor.com/docs/mcp); use the executable and arguments above.                                   |
| OpenCode              | [Local MCP servers](https://opencode.ai/docs/mcp-servers/); put the executable and arguments in its command array.          |
| Another CLI or editor | Use its stdio MCP support, or the [CLI workflow](agents.md) if it can run terminal commands.                                |

A host that accepts only remote MCP URLs cannot start `bf mcp` directly. BF provides stdio only.

## Verify the connection

Ask: **“Use the brain MCP tools to find why we chose a single product page. Read the matching source and cite its ref.”**

Check the host's tool history:

| Tool     | Arguments                                                   | Expected result                                                        |
| -------- | ----------------------------------------------------------- | ---------------------------------------------------------------------- |
| `search` | `{"query":"visitors clear explanation","scope":"projects"}` | `projects/new-website.md#decision` among its matches.                  |
| `read`   | `{"ref":"projects/new-website.md#decision"}`                | The saved reason: visitors need a clear explanation before signing up. |

The answer should cite the ref. A configured entry alone does not prove the server runs, and an answer without the exact read does not verify retrieval.

## If the connection fails

| Symptom                     | Check                                                                                          |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| Executable not found        | Use the absolute `bf` path and confirm the host can access it.                                 |
| Brain unavailable           | Read the decision in the terminal with that same absolute brain path.                          |
| No tools or old behavior    | Restart after configuration changes or a BF update.                                            |
| Empty or incomplete results | Inspect `problems`, `stale` and [source coverage](search.md#incomplete-answers-and-freshness). |
| Cache write access error    | Make the brain's `.bf/` directory writable for the host's account, or serve a writable copy.   |

## What the host can read

- Selected roots and their direct `brains:` references; review the intended audience.
- No collection, routine, write or execution tools are exposed by BF's MCP server.
- Retrieval can refresh the disposable cache in the brain's `.bf/` directory, which must be writable by the host's account, and write private local usage counts.
- The host may have other tools and may send evidence to a cloud model. See [privacy](privacy.md#your-agent-has-its-own-privacy-rules).

To read from one brain when a ref exists in several, pass its `bf://NAME/...` address, exactly as in the CLI. For parameters, pagination and large replies, use the [MCP tool contract](retrieval.md#mcp-tool-contract).
