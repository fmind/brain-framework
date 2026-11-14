# Retrieval — orient, search, read sections, verify, and export

CLI (`bf search`, `bf read`, `bf export`) and MCP (`search`, `read`) share the same offline SQLite retrieval service (`.bf/cache.sqlite3`). Neither ever spawns subprocesses or accesses the network.

## Recommended session workflow

1. **Orient first**: check what changed or needs review before calling external provider tools.
2. **Search narrowly**: issue focused keyword or identity queries with optional scope and recency filters.
3. **Read to verify**: inspect exact `#section` pages, follow pagination cursors, and trace `?rel=` neighbors before stating conclusions.
4. **Check completeness flags**: inspect `problems`, `stale`, and `newer` on every reply. An incomplete or empty result never proves absence.

## Built-in overview pages (`bf read`)

Calling `bf read` without a file ref opens curated navigation pages:

- `bf read` or `bf read home`: home note (`concepts/index.md`) plus summary counts
- `bf read review`: due `reviewed` notes, notes with `newer_evidence` in `memories/`, open `draft` actions (`blocked_by`), and `attention` items
- `bf read tasks`: open and scheduled task view across notes and `ACTION.md` files
- `bf read 7d` (or any duration such as `24h`, `30d`): recently changed notes and collected records within that window

## Full-text and identity search (`bf search`)

Search matches FTS terms, quoted phrases, `word*` prefixes, or explicit `scheme:value` identities:

```bash
bf search "oauth token rotation*" --scope projects
bf search '"rate limit"' --scope memories/github
bf search "github:example/repo#42"
```

Useful habits when iterating on queries:

- Combine close variants in one query rather than running redundant searches.
- Inspect `unmatched` terms in JSON replies and respell or broaden prefixes (`tok*`) if words missed.
- Narrow noisy queries with `--scope projects`, `--scope concepts`, `--scope memories/<source>`, or recency windows (`--scope 7d` or `--scope "7d repo:github.com/example/app"`).

## Targeted reads, sections, and graph edges

Always read the ref you intend to cite:

```bash
bf read projects/billing.md#decision
bf read projects/billing.md --offset 40
bf read projects/billing.md --rel depends-on
```

- Pass `#section` anchors to retrieve only the relevant slice without overflowing context.
- Follow `next_offset` with `--offset` whenever text is paginated; preserve section digests when comparing revisions.
- Use `--rel RELATION` to inspect incoming and outgoing edges for one declared or used relation (for example `--rel depends-on`, `--rel about`, or `--rel mentions`).
- Check `stale: true`, `problems`, and `newer` in replies; run `bf build` if the cache needs rebuilding.

## Export records for external tools

Feed external vector indexes or analysis pipelines without adding embeddings into `bf` core:

```bash
bf export --kind items
```

Consult `bf export --help` for filters and output formats.
