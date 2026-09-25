# MCP server

Agents that can run commands should use the CLI through the `bf-use` skill. For hosts that prefer tools, `bf mcp` serves the same services over stdio:

```bash
bf mcp                 # enclosing root and its direct references
bf mcp --brain ~/brain  # explicit root and its direct references
```

It exposes two read-only tools:

- `read(ref, brain)` returns the same JSON as `bf read`: the home page when `ref` is empty, another [page](search.md#pages), a note, a section, a record or an identity with its backlinks.
- `search(query, scope, limit)` returns the same JSON as `bf search`.

Text and structured results carry the same value. Errors hide brain paths. There is no collection, routine, write or execution tool; retrieval may refresh the disposable cache and record private usage counts.

## Connect a host

In your host's MCP settings, create a stdio server with command `bf` and arguments `mcp`, `--brain`, `/home/me/knowledge` (the intended brain's absolute path; a registered name also works, but the host's working directory decides which enclosing brain a bare name can match). Use the full executable path from `command -v bf` if the host does not inherit your shell's PATH. The host starts and stops the process; you do not need a separate daemon or network port.

Selected roots are fixed when the server starts; their direct `brains:` declarations are reread for each request. Restart after changing root selection or updating Brain Framework. Search and read cover those roots and direct references; the `brain` argument on `read` disambiguates a returned ref, and cannot open a brain outside that selection.

Confirm that the host lists exactly `search` and `read`. After the [getting-started walkthrough](getting-started.md), ask it why you keep original evidence and check that it reads `projects/archive.md#decision`. A working terminal command alone does not prove the host is connected.

Retrieved content is untrusted evidence: the host decides what to do with it. Its model provider may receive returned text. Choose an explicit team brain for work integrations and follow the [security model](privacy.md#separating-audiences).

Both tools share the [BF link contract](schema.md#bf-links). Read accepts BF addresses, including Markdown fragments, and returns backlinks grouped by relationship with bounded claim explanations and portable `uri` values; search accepts an identity `scope`. Links do not expand the server's selected brains or run providers.
