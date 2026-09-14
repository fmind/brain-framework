# Command reference

All successful data output is compact JSON on stdout; diagnostics are on stderr. `--version` prints the source version. Exit 0 means success, 1 an operational/check failure, 2 invalid command/model input, and 130 cancellation. Use `fkf COMMAND --help` for exact arguments.

| Command                                                                                                  | Contract                                                                                                                                           |
| -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `init PATH --name NAME`                                                                                  | Create a new or empty base; never overwrite an existing populated directory.                                                                       |
| `schema`                                                                                                 | Print the generated configuration JSON Schema.                                                                                                     |
| `collect SOURCE START END [--preview]`                                                                   | Run one enabled source over an explicit timezone-aware window. Preview runs the real command, returns at most three records and writes no capture. |
| `build [--check]`                                                                                        | Publish a derived SQLite generation, or check its state without writing.                                                                           |
| `validate`                                                                                               | Validate every published note and captured record offline.                                                                                         |
| `status`                                                                                                 | Report index state and, per configured or retired source, capture count, record count and newest capture time; no provider probes.                 |
| `find QUERY [--limit N] [--source NAME] [--after TIME] [--before TIME] [--order recent] [--history]`     | Return at most 100 deterministic local matches.                                                                                                    |
| `context QUERY [--budget N] [--source NAME] [--after TIME] [--before TIME] [--order recent] [--history]` | Return a pack bounded to four bytes per budget unit; default 850, allowed 128–16384.                                                               |
| `read URI`                                                                                               | Read an exact note, note heading, captured record, explicit alias or collection file.                                                              |
| `eval [--path queries.yaml]`                                                                             | Run owner-authored context and exact-read acceptance cases.                                                                                        |
| `mcp --base PATH`                                                                                        | Run the three-tool read-only stdio server.                                                                                                         |

Search and context also accept `--type TYPE`, `--status STATUS`, and `--within CONTAINER_ID`. Use `find '*' --within ID` for bounded hierarchy browsing.

Base commands accept `--base PATH`. There are no implicit collection, body-fetch, scheduling, or installation side effects in retrieval.
