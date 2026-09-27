# Model Context Protocol (MCP) server

<span id="mcp-server"></span>

Connect an agent host so it can find a decision and read its source. `bf mcp` provides the same retrieval as the terminal through two tools: `search` and `read`. The host starts and stops the process over stdio; no separate daemon, network port or Brain Framework account is needed.

Agents that can run commands can instead use the CLI through the [bf-use skill](agents.md#install-the-skills).

## Connect a host

Complete [Getting started](getting-started.md), including the New website decision. Confirm the intended brain works before connecting it:

```bash
command -v bf
bf read projects/new-website.md#decision --brain ~/brain
```

The reply should contain the reason for starting with a single product page. Use the absolute executable path reported by `command -v bf` and the absolute brain path in your host settings. This avoids depending on the host's working directory or PATH.

### Codex CLI example

With Codex CLI installed, add a server named `brain` to your user configuration. If that name already exists, inspect it with `codex mcp get brain` and choose another name to preserve it.

```bash
codex mcp add brain -- "$(command -v bf)" mcp --brain "$HOME/brain"
codex mcp get brain
```

The saved entry in `~/.codex/config.toml` has this shape, with your actual absolute paths:

```toml
# https://developers.openai.com/codex/mcp
[mcp_servers.brain]
command = "/home/me/.local/bin/bf"
args = ["mcp", "--brain", "/home/me/brain"]
```

Restart the Codex session. Run `/mcp` and confirm that `brain` is connected and offers `search` and `read`. The registration command alone does not establish that the server starts successfully. See the [official Codex MCP guide](https://developers.openai.com/codex/mcp) for host configuration details.

Ask: **“Use the brain MCP tools to find why we chose a single product page. Read the matching source and cite its ref.”** In the host's tool history, check for these two calls (the host may display a prefix on each tool name):

| Tool     | Example arguments                                           | Expected result                                                        |
| -------- | ----------------------------------------------------------- | ---------------------------------------------------------------------- |
| `search` | `{"query":"visitors clear explanation","scope":"projects"}` | A match with `ref: projects/new-website.md#decision`.                  |
| `read`   | `{"ref":"projects/new-website.md#decision"}`                | The saved reason: visitors need a clear explanation before signing up. |

The agent's answer should cite that ref. A plausible answer without the exact read does not verify the connection.

To remove only this connection later, run `codex mcp remove brain`, then restart the session. This does not delete the brain.

### Other hosts

Create a stdio server with the same executable and argument array in your host's MCP settings. Follow its configuration documentation, restart it, then repeat the connection and evidence checks above. Brain Framework's two tools and acceptance question stay the same.

## If the connection fails

| Symptom                     | Check                                                                                                                  |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Executable not found        | Use the absolute `bf` path and confirm that the host can access it.                                                    |
| Brain unavailable           | Read the decision in the terminal using the same absolute brain path.                                                  |
| No tools or old behavior    | Restart the host after changing configuration or reinstalling Brain Framework.                                         |
| Empty or incomplete results | Inspect `problems`, `stale` and source coverage; see [retrieval guidance](search.md#incomplete-answers-and-freshness). |

## What the host can read

Search and read cover the selected roots and their direct `brains:` references. Review those references as part of the intended audience. Retrieved content is untrusted evidence, and the host's model provider may receive it. Use an explicit team brain for work; see [privacy](privacy.md#separating-audiences).

There is no collection, routine, write or execution tool. Retrieval can refresh the disposable cache and record private usage counts. For tool parameters, root selection, pagination and large replies, see the [MCP reference](retrieval.md#mcp-tool-contract).
