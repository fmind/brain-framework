# Privacy and security

Brain Framework stores what your sensors print, in plain files you own. It does not encrypt, redact or upload anything. Choose what each sensor projects, keep private brains in private repositories with owner-only permissions, and back up records that are not in Git with encryption.

## Offline retrieval

`search`, `read`, `eval`, `validate`, `status` and the MCP tools never run a sensor or contact the network. Search may rewrite the disposable `.bf/` cache. Retrieved notes and records are untrusted evidence: their text never reaches a command, a shell or an SQL expression. Full-text queries use quoted literal terms and SQL uses bound parameters. Search and read replies carry a notice that content is evidence, never instructions, because collected mail or chat can contain prompt injections.

To show whether agents actually use a brain, `search` and `read` append one line per call to `usage.jsonl` in the private state directory: the time, the operation and the number of results, never the query or the ref. The file stays on the machine, is capped at 1 MiB, and `bf status` summarizes it over 7 and 30 days. Retrieval cases run by `bf eval` are not counted.

## Collecting is explicit and trusted per machine

Sensors are code you run with your permissions. A brain collects only on machines where its owner ran `bf init` or `bf register PATH --collect`; that trust lives in `~/.config/bf/config.yaml`, outside the brain, so pulling a shared repository never starts running its `sensors/`. Review sensor changes like any other code before trusting a shared brain.

Trust applies to the brain path, not a particular commit. Later sensor edits in an already trusted brain can run on its next scheduled update. Review incoming changes before that run, or revoke trust with `bf register PATH` while you review them. Revoking trust prevents future collection attempts; it does not cancel a process already running.

Sensors run with direct argv and no shell, from the brain root, with stdin closed and loader-injection variables (`LD_PRELOAD`, `DYLD_*`, `BASH_ENV`, Python/Node/Ruby/Perl loader paths) removed. A timeout, SIGTERM, cancellation or oversized output kills the whole process group. Shipped provider examples bound subprocess output as it arrives instead of spooling an unlimited temporary file. Provider failures write nothing; interrupted record commits retain durable originals under `memories/.pending/`; explicit `bf build` or collection recovers them, while ordinary reads fail without changing evidence. Stderr goes to a private log capped at 256 KiB in `~/.local/state/bf/`; errors name that log but never include provider output. Provider CLIs keep their own credentials; sensors must never print tokens.

## File boundaries

All brain access goes through no-follow directory descriptors: Brain Framework refuses symlinks and special files below the brain, writes atomically, and serializes writers with a lock kept in private state outside the brain. Readers use the same physical-brain lock to avoid observing half a record transaction. New brains ignore collected records and original inputs in Git by default; shared publication is an explicit brain-owner choice. Limits fail explicitly: 1 MiB configuration, 4 MiB note and exact reply, 256 MiB record partition, 100,000 entries per scanned folder, 50 search results.

## Separating audiences

Offline describes Brain Framework's retrieval. An agent host may send returned content to its model provider; configure that host for your workplace's data requirements. Restrict a work integration explicitly, for example `bf mcp --brain team`, so its scope does not depend on the host's working directory.

A brain is a context boundary, not access control. Keep people who must not read each other's data in separate brains and repositories. Agents inside a brain search only that brain; elsewhere they search every brain you registered. Register only the brains you want your agents to see.

## Honest limits

Sensors are trusted programs, not sandboxed plugins. They can use the permissions and credentials of the account running them; review their code and scope before granting collection trust. Offline retrieval does not prove an upstream record is current, and Brain Framework does not check the truth of a note or neutralize prompt injections for the consuming agent.

Filesystem confinement refuses redirected paths below a brain; it does not protect against another process with the same user's permissions replacing the brain or its SQLite cache. Use operating-system accounts and repository permissions for adversarial separation. Atomic writes and recovery journals protect interrupted commits; backups still matter for accidental deletion, storage failure and upstream history that no longer exists.
