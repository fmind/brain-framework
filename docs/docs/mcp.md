# Read-only MCP

Run the stdio server with an explicit base:

```bash
fkf mcp --base /absolute/path/to/knowledge
```

Register that command with the agent host's native MCP configuration. Use a persistent installed executable for persistent registrations. A development checkout is not an installed release.

The server exposes exactly three tools: `find(query, limit, ...)`, `context(query, budget, ...)`, and `read(uri)`. They use the same services as the CLI. Text and structured results contain the same JSON value. Context counts both representations and the tool-result wrapper against `budget × 4` bytes; it can therefore contain fewer excerpts than CLI context at the same budget. Errors conceal the selected base path and provider output.

MCP has no collection, write, scheduling or body-fetch operation. Stored content is untrusted evidence. The host decides whether to use it, and should resolve exact references before relying on details.

`find` and `context` also accept `source`, `after`, `before`, `order`, and `history` (default false). History includes older captures; record results label their snapshot as latest or historical. These labels describe stored observations, not current policy. Exact `read` remains available for any retained captured-record URI.

Replies include the selected base identity and qualified exact references. `find` and `context` accept `note_type`, `status` and `within` filters. The server never resolves another base from a reference. Register it only in the workspaces authorized to use that base; filesystem and process permissions provide confidentiality.
