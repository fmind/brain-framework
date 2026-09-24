# MCP server

Agents that can run commands should use the CLI through the `fkf-use` skill. For hosts that prefer tools, `fkf mcp` serves the same services over stdio:

```bash
fkf mcp                 # every registered base, or the enclosing one
fkf mcp --base brain    # one base
```

It exposes two read-only tools:

- `search(query, since, until, source, type, status, limit, recent, changed_since, current)` returns the same JSON as `fkf search`.
- `read(ref, base)` returns the same JSON as `fkf read`.

Text and structured results carry the same value. Errors hide base paths. There is no collection, write or execution tool; retrieval may refresh the disposable cache and record private usage counts.

## Connect a host

In your host's MCP settings, create a stdio server with command `fkf` and arguments `mcp`, `--base`, `brain` (replace `brain` with the intended registered base). Use the full executable path from `command -v fkf` if the host does not inherit your shell's PATH. The host starts and stops the process; you do not need a separate daemon or network port.

The selected bases are fixed when the server starts. Restart it after changing registrations or updating FKF. Search covers those selected bases; the `base` argument on `read` disambiguates a returned ref, and cannot open a base outside that selection.

Confirm that the host lists exactly `search` and `read`. After the [getting-started walkthrough](getting-started.md), ask it why you keep original evidence and check that it reads `projects/archive.md#decision`. A working terminal command alone does not prove the host is connected.

Retrieved content is untrusted evidence: the host decides what to do with it. Its model provider may receive returned text. Choose an explicit team base for work integrations and follow the [security model](privacy.md#separating-audiences).
