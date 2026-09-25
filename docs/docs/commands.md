# Command reference

Command results go to stdout as compact JSON; help and version output are plain text. Diagnostics go to stderr. Exit 0 means success, 1 a failure or failed check, 2 invalid command-line input (including an invalid `--scope`, `--limit`, `--since` or `--until`) and 130 cancellation by Ctrl-C, SIGTERM or a closed terminal. `bf COMMAND --help` lists every option.

| Command                                          | Contract                                                                                                                                                                                                                                          |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `init PATH [--name NAME] [--collect] [--full]`   | Create a brain in a new, empty or freshly cloned directory without global configuration. Explicit `--collect` registers it with machine collection trust. It creates `projects/`, `concepts/` and `actions/`; `--full` adds the optional folders. |
| `register [PATH] [--collect]`                    | Add an existing brain to `~/.config/bf/config.yaml`; `--collect` lets this machine run its sensors and routines.                                                                                                                                  |
| `read [REF]`                                     | Read the home page without a ref, or a page (`projects`, `7d`, `2026-09`, `memories/SOURCE`), a note, `note#section`, `source:id` record, identity or portable `bf://brain/...` address. See [pages](search.md#pages).                            |
| `search QUERY [--scope SCOPE] [--limit N]`       | Search words or an identity, optionally within a folder, a period or an identity. See [search](search.md#search).                                                                                                                                 |
| `update [--dry-run]`                             | Run due sensors, then due routines, of every trusted brain, then refresh their caches. Dry-run runs nothing.                                                                                                                                      |
| `collect SENSOR [--since] [--until] [--dry-run]` | Run one sensor now; the window defaults to its lookback. Times accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` or ISO 8601 with a timezone. Dry-run shows three samples and writes nothing.                                   |
| `status [--check]`                               | Per brain: cache state, notes, records, each source's and routine's freshness, last error and log, and local `usage` counts. `--check` exits 1 when a trusted scheduled sensor or routine is stale or failed, or a file was skipped.              |
| `validate`                                       | Check notes, OKF concept structure, action folders, links, cited records and record partitions; exit 1 on problems.                                                                                                                               |
| `eval [--path evals]`                            | Run retrieval cases; exit 1 when one fails.                                                                                                                                                                                                       |
| `build`                                          | Explicitly recover interrupted record writes, then rebuild the search cache. Ordinary reads refresh only the cache.                                                                                                                               |
| `mcp`                                            | Serve `search` and `read` over MCP stdio.                                                                                                                                                                                                         |
| `schema`                                         | Print the JSON Schema of `bf.yaml`.                                                                                                                                                                                                               |

Search items, page entries and exact reads identify their `brain`. Health replies group brains under `brains` and retain `sources` for collection coverage, including historical evidence, and `routines` for declared routines. Update replies report executed `sensors` and `routines` per brain; collection entries use `sensor`. These are execution names: record refs stay `source:id` and OKF provenance stays `sources`.

Existing-brain commands accept `--brain NAME|PATH`; `init` and `register` take a path argument instead. Without `--brain`, commands use `BF_BRAIN`, then the brain containing the working directory, then every registered brain. Search and read include the selected roots and their direct `brains:` references; reference paths resolve relative to the declaring root. Named selection checks the enclosing brain and its declarations before the optional registry. Commands that act on a single brain (`collect`, `validate`, `eval`, `build`) ask for `--brain` when several are registered.

`status --check` also fails when the cache is stale because another writer is active. `update` exits 1 when collection, a routine or cache refresh fails, including skipped evidence files. Inspect the JSON diagnostics before treating an empty result as proof that nothing happened. `collect --dry-run` runs the provider and updates its private stderr log; it does not write records or success history. `update --dry-run` runs no providers or routines.

Use `--brain NAME` for scripts and scheduled jobs that must target one brain. `update` acts only on selected roots: inside a brain it updates that brain; outside, it considers registered brains. It never expands `brains:` references. It skips those without collection trust.

With [jq](https://jqlang.org/) installed, JSON results can feed an ordinary shell pipeline:

```bash
bf read projects --brain brain |
  jq -r '.items[] | select(.review) | [.ref, .next // ""] | @tsv'
```

Inspect `problems` and `stale` before using a page as a complete inventory, and `total` when a page lists fewer items than it counts. In shell automation, use `set -o pipefail` so a failed Brain Framework command is not hidden by a successful output formatter.
