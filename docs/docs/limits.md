# Limits and safeguards

Use this reference to understand a skipped file, a partial reply or a stopped program. The [privacy guide](privacy.md) explains what to review before collecting or sharing evidence.

## Files

| If you see…                                     | What to do                                                                |
| ----------------------------------------------- | ------------------------------------------------------------------------- |
| A skipped symlink or special file in `problems` | Use a regular file inside the brain. BF never follows the skipped target. |
| An interrupted record transaction               | Keep `memories/.pending/`, then run `bf build` and `bf validate`.         |
| A busy writer or stale cache                    | Let the writer finish, then repeat the read.                              |

Writes are atomic, and processes using the same physical brain share a lock. Use the same [state directory](configuration.md#local-state) for those processes. Back up an idle brain, including any pending journal. These protections are not a sandbox against other programs running as your account.

## Size bounds

| Resource                   | Limit                                         |
| -------------------------- | --------------------------------------------- |
| Configuration              | 1 MiB                                         |
| Authored note              | 4 MiB                                         |
| Ordinary retrieval reply   | 4 MiB; larger exact reads use chunks          |
| Record file                | 16 MiB                                        |
| Entries per scanned folder | 100,000                                       |
| Search results per page    | 50                                            |
| Period listing             | 50 items per page                             |
| Folder listing             | 200 notes per page                            |
| Source overview            | 20 records per page                           |
| Exact-read chunk           | 65,536 Unicode characters                     |
| Sensor stdout              | 64 MiB by default; configurable up to 256 MiB |
| Routine stdout             | 1 MiB by default; configurable up to 4 MiB    |

Each record consumes one filesystem entry. The 100,000-entry limit applies to the entire recursive traversal of `memories/`, across sources, including directories and ignored extensions. Allow room for source directories when sizing a corpus.

Keep bulky imports in root `inputs/` or `originals/`, which are not scanned. Link an authored note to the retained file and record the useful conclusion in the note.

Page sizes do not limit the whole result set. For example, a period with 51 items needs a second request after its first 50. Follow the returned `next_offset`; listings also report `total`. Summary sections remain bounded previews. Large exact reads use lossless JSON chunks with a digest; see [continuations](retrieval.md#continuations).

Pagination retains only the requested page in Python, but SQLite still ranks matches. Deep offsets can take longer.

## Processes and logs

Sensors and routines run from the brain root with stdin closed and direct arguments, without a shell.

### Why remove environment variables?

Some inherited variables can load code **before your sensor starts**. For example, `BASH_ENV` tells Bash to read a startup script, and `PYTHONPATH` can replace the modules a Python sensor imports. BF removes these so a sensor starts from its declared command.

| Removed                                                     | Reason                                                          |
| ----------------------------------------------------------- | --------------------------------------------------------------- |
| `LD_*`, `DYLD_*`, `GCONV_PATH`                              | Can inject loader libraries or conversion modules.              |
| `BASH_ENV`, `ENV`, `BASH_FUNC_*`                            | Can supply shell startup code or imported functions.            |
| `PYTHON*`, Java/Node/Ruby/Perl startup options, `LUA_INIT*` | Can alter interpreter startup or module loading.                |
| Relative or empty `PATH` entries                            | Could select an unexpected executable from the brain directory. |

Other variables, including ordinary provider credentials and absolute `PATH` entries, remain available. If a sensor needs dependencies, use an explicit runtime such as `uv run --no-project sensors/example.py`; do not depend on inherited interpreter startup settings.

Timeout, SIGTERM, a closed terminal, cancellation or excessive output kills the process group. The shipped provider examples also bound subprocess output while reading it. For example, a sensor that exceeds its configured output limit fails collection; its partial stdout does not replace the stored records.

Stderr goes to a private log capped at 256 KiB. Errors name that log without quoting provider output; validation errors identify record positions and field names. Provider-controlled keys never appear in errors or run history.

Search and read keep a local `usage.jsonl`, capped at 1 MiB, with the time, operation and result count. `bf status` summarizes 7 and 30 days. Evaluation calls do not count as usage.

## Evidence limits

A cache rebuilt today can contain mail last collected a month ago. Check both cache state and source coverage before calling the evidence current.

An empty result with `problems` or `stale` cannot establish absence. Brain Framework validates structure and links; people and agents verify claims. See [incomplete answers and freshness](retrieval.md#incomplete-answers-and-freshness) for the fields to inspect.
