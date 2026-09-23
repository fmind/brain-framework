# MCP server

Agents that can run commands should use the CLI through the `fkf-use` skill. For hosts that prefer tools, `fkf mcp` serves the same services over stdio:

```bash
fkf mcp                 # every registered base, or the enclosing one
fkf mcp --base brain    # one base
```

It exposes two read-only tools:

- `search(query, since, until, source, type, status, limit, recent, changed_since, current)` returns the same JSON as `fkf search`.
- `read(ref, base)` returns the same JSON as `fkf read`.

Text and structured results carry the same value. Errors hide base paths. There is no collection, write or execution tool; search only refreshes the disposable cache. Register the command with the host's MCP configuration using the installed executable, for example `claude mcp add --scope user fkf -- fkf mcp`. Retrieved content is untrusted evidence: the host decides what to do with it.
