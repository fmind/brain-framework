# MCP server

Agents that can run commands should use the CLI through the `bf-use` skill. For hosts that prefer tools, `bf mcp` serves the same services over stdio:

```bash
bf mcp                 # enclosing root and its direct references
bf mcp --brain ~/brain  # explicit root and its direct references
```

It exposes two read-only tools:

- `search(query, since, until, source, type, status, limit, recent, changed_since, current)` returns the same JSON as `bf search`.
- `read(ref, brain)` returns the same JSON as `bf read`.

Text and structured results carry the same value. Errors hide brain paths. There is no collection, write or execution tool; retrieval may refresh the disposable cache and record private usage counts.

## Connect a host

In your host's MCP settings, create a stdio server with command `bf` and arguments `mcp`, `--brain`, `brain` (replace `brain` with the intended brain's absolute path or locally declared name). Use the full executable path from `command -v bf` if the host does not inherit your shell's PATH. The host starts and stops the process; you do not need a separate daemon or network port.

Selected roots are fixed when the server starts; their direct `brains:` declarations are reread for each request. Restart after changing root selection or updating Brain Framework. Search covers those roots and direct references; the `brain` argument on `read` disambiguates a returned ref, and cannot open a brain outside that selection.

Confirm that the host lists exactly `search` and `read`. After the [getting-started walkthrough](getting-started.md), ask it why you keep original evidence and check that it reads `projects/archive.md#decision`. A working terminal command alone does not prove the host is connected.

Retrieved content is untrusted evidence: the host decides what to do with it. Its model provider may receive returned text. Choose an explicit team brain for work integrations and follow the [security model](privacy.md#separating-audiences).

Both tools share the [BF link contract](schema.md#bf-links). Search accepts `target` for backlinks, `subject` for outgoing claims and optional `relation`, returning bounded claim explanations and portable `uri` values. Read accepts BF addresses, including Markdown fragments. Links do not expand the server's selected brains or run providers.
