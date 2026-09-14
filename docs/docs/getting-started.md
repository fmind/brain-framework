# Getting started

Use `uv run fkf` from the unreleased v7 checkout, or install its locally built wheel. Publication is a separate step.

```bash
uv run fkf init ~/knowledge
uv run fkf build --base ~/knowledge
uv run fkf context "project decisions" --base ~/knowledge --budget 850
uv run fkf read wiki/welcome.md --base ~/knowledge
```

Write useful decisions, constraints and next actions in Markdown under projects/ and wiki/. Add a source only when it answers a recurring question. Review its adapter and configuration before running an explicit collection.

## Agent workflows

The repository’s `skills/` directory contains reusable `fkf-use` and `fkf-learn` packages. Copy the selected directory into your base’s `.agents/skills/` or the host’s native skill catalog. Use the base-local instructions to select its installed executable. See the [base layout](base.md) and [MCP setup](mcp.md).
