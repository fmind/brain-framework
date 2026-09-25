# Privacy and security

Brain Framework stores what your sensors print, in plain files you own. It does not encrypt, redact or upload anything. Choose what each sensor projects, keep private brains in private repositories with owner-only permissions, and back up records that are not in Git with encryption.

## Offline retrieval

`search`, `read` (including every page), `eval`, `validate`, `status` and the MCP tools never run a sensor or routine, or contact the network. Search may rewrite the disposable `.bf/` cache. Retrieved notes and records are untrusted evidence: their text never reaches a command, a shell or an SQL expression. Full-text queries use quoted literal terms and SQL uses bound parameters. Search and read replies carry a notice that content is evidence, never instructions, because collected mail or chat can contain prompt injections.

To show whether agents actually use a brain, `search` and `read` append one line per call to `usage.jsonl` in the private state directory: the time, the operation and the number of results, never the query or the ref. The file stays on the machine, is capped at 1 MiB, and `bf status` summarizes it over 7 and 30 days. Retrieval cases run by `bf eval` are not counted.

## Untrusted sources

Most collected text is written by other people: a mail subject, an invitation, an issue comment or a feed item can carry instructions aimed at an agent. Each sensor declares `trust` in `bf.yaml`; only `trust: owner` sources, whose text the brain's owner writes, are shown with excerpts on pages. Pages list every other record by title and ref and mark it `external`, so the home page, periods and backlinks do not place third-party text in an agent's context unasked. Searches and exact reads return the text an agent asked for, labeled `external`. Routines and hooks that write or inject context should name external items by ref only, as the examples do. The label reduces exposure; it does not neutralize a prompt injection in text an agent chooses to read.

## Collecting is explicit and trusted per machine

Sensors and routines are code you run with your permissions. A brain collects only on machines where its owner ran `bf init --collect`, or `bf register PATH --collect`; that trust lives in `~/.config/bf/config.yaml`, outside the brain, so pulling a shared repository never starts running its `sensors/` or `routines/`. Review their changes like any other code before trusting a shared brain, and require review for `bf.yaml`, `sensors/`, `routines/` and CI changes in a [team brain](team.md#collect-in-ci).

Trust applies to the brain path, not a particular commit. Later sensor or routine edits in an already trusted brain can run on its next scheduled update. Review incoming changes before that run, or revoke trust with `bf register PATH` while you review them. Revoking trust prevents future collection attempts; it does not cancel a process already running.

Sensors and routines run with direct argv and no shell, from the brain root, with stdin closed, relative `PATH` entries dropped and startup-injection variables (`LD_*`, `DYLD_*`, `BASH_ENV`, every `PYTHON*` variable, and Java, Node, Ruby, Perl and Lua startup options) removed. A timeout, SIGTERM, a closed terminal, cancellation or oversized output kills the whole process group. Shipped provider examples bound subprocess output as it arrives instead of spooling an unlimited temporary file. Provider failures write nothing; interrupted record commits retain durable originals under `memories/.pending/`; explicit `bf build` or collection recovers them, while ordinary reads fail without changing evidence. Stderr goes to a private log capped at 256 KiB in `~/.local/state/bf/`; errors name that log but never include provider output. Provider CLIs keep their own credentials; sensors must never print tokens. Invalid sensor output is reported by record position and field name only: keys the provider printed never reach errors, logs or run history. A routine's output is validated as a Markdown note and written as a new action only after the routine succeeds; it never replaces an existing action or edits other notes.

## File boundaries

All brain access goes through no-follow directory descriptors: Brain Framework refuses symlinks and special files below the brain, writes atomically, and serializes writers with a lock kept in private state outside the brain. The lock follows the brain directory itself, so bind mounts and other spellings of its path share it; processes that write one brain must share the same private state directory. Readers use the same physical-brain lock to avoid observing half a record transaction. New brains ignore collected records and original inputs in Git by default; shared publication is an explicit brain-owner choice. Limits fail explicitly: 1 MiB configuration, 4 MiB note and exact reply, 256 MiB record partition, 100,000 entries per scanned folder, 50 search results, and routine output of 1 MiB by default. Pages are bounded instead: at most 50 period or source items and 200 notes per folder, each with its `total`, and 20 items per section.

## Separating audiences

Offline describes Brain Framework's retrieval. An agent host may send returned content to its model provider; configure that host for your workplace's data requirements. Restrict a work integration explicitly, for example `bf mcp --brain team`, so its scope does not depend on the host's working directory.

A brain is a context boundary, not access control. Keep people who must not read each other's data in separate brains and repositories. Agents inside a brain search that root and its directly declared `brains:` references; elsewhere the optional registry supplies roots. Review reference changes before an agent uses them, especially in shared repositories: declarations expand the readable context. Reference expansion is not recursive and never grants collection trust. Records committed to Git stay in its history; publish only sources every reader may keep.

## Honest limits

Sensors and routines are trusted programs, not sandboxed plugins. They can use the permissions and credentials of the account running them; review their code and scope before granting collection trust. Offline retrieval does not prove an upstream record is current, and Brain Framework does not check the truth of a note or neutralize prompt injections for the consuming agent.

Filesystem confinement refuses redirected paths below a brain; it does not protect against another process with the same user's permissions replacing the brain or its SQLite cache. Use operating-system accounts and repository permissions for adversarial separation. Atomic writes and recovery journals protect interrupted commits; backups still matter for accidental deletion, storage failure and upstream history that no longer exists.
