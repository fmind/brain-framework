# Command reference

All successful data output is compact JSON on stdout; diagnostics are on stderr. `--version` prints the installed package version. Exit 0 means success, 1 an operational/check failure, 2 invalid command/model input, and 130 cancellation. Use `fkf COMMAND --help` for exact arguments.

| Command                                                                                                  | Contract                                                                                                                                           |
| -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `init PATH --name NAME`                                                                                  | Create a new or empty base; never overwrite an existing populated directory.                                                                       |
| `update [--dry-run]`                                                                                     | Collect due sources and rebuild a stale index; dry-run executes no commands and writes nothing.                                                    |
| `schema`                                                                                                 | Print the generated configuration JSON Schema.                                                                                                     |
| `collect SOURCE START END [--preview]`                                                                   | Run one enabled source over an explicit timezone-aware window. Preview runs the real command, returns at most three records and writes no capture. |
| `build [--check] [--if-stale]`                                                                           | Publish a derived SQLite generation, inspect its state, or skip a current index. The flags are mutually exclusive.                                 |
| `validate`                                                                                               | Validate authored wiki structure against OKF v0.2 and all note/capture models offline.                                                             |
| `status`                                                                                                 | Report index state and, per configured or retired source, capture count, record count and newest capture time; no provider probes.                 |
| `find QUERY [--limit N] [--source NAME] [--after TIME] [--before TIME] [--order recent] [--history]`     | Return at most 100 deterministic local matches.                                                                                                    |
| `context QUERY [--budget N] [--source NAME] [--after TIME] [--before TIME] [--order recent] [--history]` | Return a pack bounded to four bytes per budget unit; default 850, allowed 128–16384.                                                               |
| `read URI`                                                                                               | Read an exact note, note heading, captured record, explicit alias or collection file.                                                              |
| `eval [--path queries.yaml]`                                                                             | Run owner-authored context and exact-read acceptance cases.                                                                                        |
| `mcp --base PATH`                                                                                        | Run the three-tool read-only stdio server.                                                                                                         |

Search and context also accept `--type TYPE`, `--status STATUS`, and `--within CONTAINER_ID`. Use `find '*' --within ID` for bounded hierarchy browsing.

Options follow their command, for example `fkf context retention --base ~/knowledge`. Base commands accept `--base PATH`; selection uses that option, then nonempty `FKF_BASE`, then the nearest ancestor `fkf.yaml`. There are no implicit collection, body-fetch, scheduling, or installation side effects in retrieval.

`update [--dry-run]` collects due sources with positive `refresh` intervals and rebuilds only when stale. Dry-run has no execution or write side effects. It is separate from software upgrades and native schedule installation.

Indexed retrieval (`find`, `context`, `eval`, and alias/record-reference reads) requires a ready index. `status` diagnoses missing, stale or corrupt state without building. Direct Markdown and capture-file reads remain available for recovery.
