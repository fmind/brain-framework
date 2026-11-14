# Retrieval, pages, search, export, and MCP

CLI (`bf search`, `bf read`, `bf export`) and local stdio MCP (`bf mcp`) share one strictly offline SQLite retrieval service (`.bf/cache.sqlite3`). Retrieval never contacts the network or executes configured jobs.

## Session workflow

1. **Orient first**: read Home (`bf read`) and review queues (`bf read review`) before fetching external tools.
2. **Search narrowly**: run focused keyword, phrase, prefix, or identity queries with `--scope`.
3. **Read to verify**: inspect exact `#section` or `SOURCE:ID` refs and trace `--rel` neighbors before stating claims.
4. **Check completeness flags**: inspect `problems`, `stale`, `sources`, and `next_offset` on every reply. An incomplete or empty result never proves absence.

## Built-in overview pages (`bf read`)

| Ref for `bf read`                                                        | Returns                                                                                                                                                   |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| _(omitted)_ or `home`                                                    | Home note (`concepts/index.md`), prioritized `projects`, up to 10 `draft` `actions`, `changed`, 24h `activity`, 7d `upcoming`, `attention`, `pages`.      |
| `review`                                                                 | Paginated non-deprecated review items (`due`, `newer_evidence`, `future_modified`, `unknown_modified`), 20 `draft` `actions` (`blocked_by`), `attention`. |
| `tasks`                                                                  | Open `- [ ]` tasks across non-deprecated OKF notes (50/page) and `summary` counts.                                                                        |
| `projects`, `concepts`, `actions`, or subfolder                          | Folder listing with open task counts (`ACTION.md` per action folder).                                                                                     |
| `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD`, `YYYY-MM`, `A..B` | Period items (local midnight DST-aware), upstream `changed` records (up to 20), and `sources` summary counts.                                             |
| `memories`, `memories/SOURCE`, `memories/SOURCE/PERIOD\|undated\|FILE`   | Source inventory, coverage health, undated records, or SHA-256 filename -> `SOURCE:ID` mapping.                                                           |
| `tags`, `tags/LABEL`                                                     | Tag directory and tag members (200/page).                                                                                                                 |
| `actions/YYYY-MM-DD_SLUG`                                                | The action's `ACTION.md`, folder `files` (up to 200), linked `projects` (20), and `backlinks`.                                                            |

- `stale_after` or 14 days without edits marks projects `"review": true`; `newer_evidence` compares record `time` / `attributes.updated` against note `modified` (`attributes.observed` never triggers review, and undated records fall back to `observed` only for period placement).
- `priority: low` sources summarize under `sources` without flooding period item lists; `deprecated` notes stay out of Home, `review`, and `tasks`.

## Full-text and identity search (`bf search`)

```bash
bf search "oauth token rotation*" --scope projects
bf search '"rate limit"' --scope "7d memories/github"
bf search "github:example/repo#42"
```

- **Query syntax**: up to 4,096 chars and 32 distinct terms, `"quoted phrases"`, `word*` prefixes (>= 3 letters, expanding up to 64 stems), case/Latin-accent/compatibility folding with English stemming, English/French unquoted function-word filtering (uppercase acronyms stay), and CJK unspaced matching. Inspect `unmatched` and `suggestions`.
- **Ranking**: BM25 weighted toward titles, headings, filename stems, aliases, and tags, +20% multi-term coverage bonus, OKF notes boosted, `deprecated` last, `priority: low` and `"generated": true` assistant outputs at half weight, newest first on ties. Duplicate note sections group under `sections` (up to 3) and duplicate URLs under `also` (up to 5).
- **Scopes (`--scope`)**: optional period + space + folder/note (`projects`, `projects/api.md`), `memories/<source>[/<period|undated|file>]`, explicit identity (`repo:...`), or full tag URI (`bf://brain/tags/LABEL`).
- **Source coverage**: `sources` reports returned record sources plus any `failed`, `overdue`, `never`, `silent`, or `degraded` sources (`sources_omitted` flags omitted summary entries on broad queries).

## Targeted reads, relations, and pagination

```bash
bf read projects/new-website.md#decision
bf read projects/new-website.md --rel depends-on
bf read memories/mail/ab12...json --offset 32768
```

- Pass `--rel RELATION` on whole notes, action folders, records, or identities to paginate incoming links (including `broader` narrower relations, 50/page).
- Replies cut softly near 32 KiB with `next_offset` (`0`..`2^53-1`). Page 1 of large multi-section notes returns `4 KiB` plus up to 200 `outline` sections.
- Cache refreshes automatically on read (including a 5s `memories/SOURCE` settle window; 2s lock wait / 3s record wait before `"stale": true`). Run `bf build` after hand-editing JSON files inside `memories/`.

## Export graph claims and items

```bash
bf export --kind edges
bf export --kind identities
bf export --kind items --scope memories/mail --observed-since 7d
```

Streams compact JSON Lines from the validated cache (`--kind edges` default, `identities`, or full `items` for external vector/embedding pipelines without adding embeddings into `bf` core). Exits `1` after streaming remaining rows if any unreadable/changed file was skipped.

## Local MCP server (`bf mcp`)

Requires `'brain-framework[mcp]'`. `bf mcp [--brain NAME|PATH]` exposes two strictly offline read-only stdio tools with draft 2020-12 output schemas:

- `search(query, scope="", limit=10, offset=0)`
- `read(ref="", rel="", offset=0)`

Brain selection resolves at startup and on every call (`--brain`, `BF_BRAIN`, enclosing brain, registry `default`), with brain/registry/home prefixes redacted in errors. Configure hosts (Claude Code, Codex, Claude Desktop, Cursor, VS Code, Zed) to launch `bf mcp --brain /path/to/brain`.
