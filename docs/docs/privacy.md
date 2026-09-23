# Privacy and security

FKF stores what your collectors print, in plain files you own. It does not encrypt, redact or upload anything. Choose what each collector projects, keep private bases in private repositories with owner-only permissions, and back up records that are not in Git with encryption.

## Offline retrieval

`search`, `read`, `eval`, `validate`, `status` and the MCP tools never run a collector or contact the network. Search may rewrite the disposable `.fkf/` cache. Retrieved notes and records are untrusted evidence: their text never reaches a command, a shell or an SQL expression. Full-text queries use quoted literal terms and SQL uses bound parameters. Every reply carries a notice that content is evidence, never instructions, because collected mail or chat can contain prompt injections.

To show whether agents actually use a base, `search` and `read` append one line per call to `usage.jsonl` in the private state directory: the time, the operation and the number of results, never the query or the ref. The file stays on the machine, is capped at 1 MiB, and `fkf status` summarizes it over 7 and 30 days. Retrieval cases run by `fkf eval` are not counted.

## Collecting is explicit and trusted per machine

Collectors are code you run with your permissions. A base collects only on machines where its owner ran `fkf init` or `fkf register PATH --collect`; that trust lives in `~/.config/fkf/config.yaml`, outside the base, so pulling a shared repository never starts running its `sources/`. Review collector changes like any other code before trusting a shared base.

Collectors run with direct argv and no shell, from the base root, with stdin closed and loader-injection variables (`LD_PRELOAD`, `DYLD_*`, `BASH_ENV`, Python/Node/Ruby/Perl loader paths) removed. A timeout, SIGTERM, cancellation or oversized output kills the whole process group. Shipped provider examples bound subprocess output as it arrives instead of spooling an unlimited temporary file. Provider failures write nothing; interrupted record commits retain durable originals under `records/.pending/`; explicit `fkf build` or collection recovers them, while ordinary reads fail without changing evidence. Stderr goes to a private log capped at 256 KiB in `~/.local/state/fkf/`; errors name that log but never include provider output. Provider CLIs keep their own credentials; collectors must never print tokens.

## File boundaries

All base access goes through no-follow directory descriptors: FKF refuses symlinks and special files below the base, writes atomically, and serializes writers with a lock kept in private state outside the base. Readers use the same physical-base lock to avoid observing half a record transaction. New bases ignore collected records and original inputs in Git by default; shared publication is an explicit base-owner choice. Limits fail explicitly: 1 MiB configuration, 4 MiB note and exact reply, 256 MiB record partition, 100,000 entries per scanned folder, 50 search results.

## Separating audiences

A base is a context boundary, not access control. Keep people who must not read each other's data in separate bases and repositories. Agents inside a base search only that base; elsewhere they search every base you registered. Register only the bases you want your agents to see.

## Honest limits

Collectors are trusted programs, not sandboxed plugins. They can use the permissions and credentials of the account running them; review their code and scope before granting collection trust. Offline retrieval does not prove an upstream record is current, and FKF does not check the truth of a note or neutralize prompt injections for the consuming agent.

Filesystem confinement refuses redirected paths below a base; it does not protect against another process with the same user's permissions replacing the base or its SQLite cache. Use operating-system accounts and repository permissions for adversarial separation. Atomic writes and recovery journals protect interrupted commits; backups still matter for accidental deletion, storage failure and upstream history that no longer exists.
