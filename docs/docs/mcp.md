---
icon: lucide/plug
description: Connect an agent through local MCP search and read tools, then verify access to a saved decision.
---

# Connect an agent with MCP

[MCP (Model Context Protocol)](https://modelcontextprotocol.io/) lets an agent host call BF's **`search` and `read`** tools. The host starts `bf mcp` as a local process over stdio; no network port or account is involved. A terminal agent can use the CLI directly instead; see [Agent workflows](agents.md).

## Before connecting

Complete [Getting started](getting-started.md), then check the same decision from your terminal:

```bash
cd ~/brain
command -v bf
bf read projects/new-website.md#decision
```

Give the host absolute paths to `bf` and to the brain: hosts may start the server from another folder, so `--brain` is useful here. The server checks its brain at startup and selects it again for each call, so registering or restoring a brain needs no restart; an empty `--brain` is invalid input.

## Claude Code

If a server named `brain` exists, inspect it with `claude mcp get brain` or choose another name.

```bash
claude mcp add --transport stdio --scope user brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
claude mcp get brain
```

Restart Claude Code, run `/mcp` and confirm that `brain` connects. See [Claude Code MCP setup](https://code.claude.com/docs/en/mcp). Remove it with `claude mcp remove brain --scope user`.

## Codex

If a server named `brain` exists, inspect it with `codex mcp get brain` or choose another name.

```bash
codex mcp add brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
codex mcp get brain
```

The equivalent entry in `~/.codex/config.toml`, with your absolute paths:

```toml
# https://developers.openai.com/codex/mcp
[mcp_servers.brain]
command = "/home/me/.local/bin/bf"
args = ["mcp", "--brain", "/home/me/brain"]
```

Restart Codex and run `/mcp`. See [Codex MCP setup](https://developers.openai.com/codex/mcp). Remove it with `codex mcp remove brain`.

## Other agent hosts

Use the host's **local stdio** configuration with these values, replacing the paths:

| Field       | Value                                  |
| ----------- | -------------------------------------- |
| Server name | `brain`                                |
| Executable  | `/home/me/.local/bin/bf`               |
| Arguments   | `["mcp", "--brain", "/home/me/brain"]` |
| Tools       | `search`, `read`                       |

| Host               | Official setup                                                                                                       |
| ------------------ | -------------------------------------------------------------------------------------------------------------------- |
| Copilot in VS Code | [Add and manage MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers), as a stdio server. |
| Cursor             | [MCP configuration](https://cursor.com/docs/mcp).                                                                    |
| OpenCode           | [Local MCP servers](https://opencode.ai/docs/mcp-servers/): the executable and arguments form its command array.     |

A host that accepts only remote MCP URLs cannot start `bf mcp`: BF serves stdio only.

## Verify the connection

Ask: **“Use the brain MCP tools to find why we chose a single product page. Read the matching source and cite its ref.”** Then check the host's tool history:

| Tool     | Arguments                                                   | Expected result                                                        |
| -------- | ----------------------------------------------------------- | ---------------------------------------------------------------------- |
| `search` | `{"query":"visitors clear explanation","scope":"projects"}` | `projects/new-website.md#decision` among its items.                    |
| `read`   | `{"ref":"projects/new-website.md#decision"}`                | The saved reason: visitors need a clear explanation before signing up. |

The answer should cite the ref. A configured entry does not prove the server runs, and an answer without the exact read does not prove retrieval.

## If the connection fails

| Symptom                     | Check                                                                                          |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| Executable not found        | Use the absolute `bf` path, and confirm the host's account can run it.                         |
| Brain unavailable           | Read the decision in your terminal with the same absolute brain path.                          |
| No tools, or old behavior   | Restart the host after changing its configuration or updating BF.                              |
| Empty or incomplete results | Inspect `problems`, `stale` and [source coverage](search.md#incomplete-answers-and-freshness). |
| Cache write access error    | Make the brain's `.bf/` folder writable for the host's account, or serve a writable copy.      |

## What the host can read

- The selected brain and its direct `brains:` references: review their audience first. Started outside any brain without `--brain` or `BF_BRAIN`, the server reads every registered brain, including one registered while it runs.
- Nothing else: BF's server exposes no collection, program, write or network tool. Retrieval only refreshes the disposable `.bf/` cache and private usage counts.
- The host may have other tools and may send what it reads to a cloud model; see [privacy](privacy.md#your-agent-has-its-own-privacy-rules).

Arguments match the CLI. To read from one brain when a ref exists in several, pass its `bf://NAME/...` address. To list every item linking to a note through one relation, pass it as `rel`, as `bf read REF --rel RELATION` does: `{"ref":"projects/new-website.md","rel":"cites"}`.

Hosts show the tools as **Search the brain** and **Read a brain page, note or record**, both annotated read-only.

At connection, the server sends instructions with the loop the generated `AGENTS.md` teaches: orient, search a few words, read each ref relied on, follow `next_offset` and inspect `problems` and `stale`. Whether a host passes them to its model depends on the host. The [MCP tool contract](retrieval.md#mcp-tool-contract) lists parameters, reply schemas and errors.
